"""
Data quality scorecard — runs after every silver load, writes results to gold.dq_scorecard.

I split rules into MUST_PASS and HIGH/MEDIUM/LOW because not every quality issue
should stop the pipeline. A slightly stale AECB report (DQ-AECB-03) shouldn't block
10K other decisions — but a negative fraud score (DQ-FRAUD-01) absolutely should,
because the whole scoring model breaks if the input is out of range.

MUST_PASS failures halt the pipeline and page the team.
HIGH failures send an alert but let the pipeline continue — decisions made during
the degraded period get a lower dq_score so underwriters know to scrutinise them.

The thresholds (95%, 99% etc.) were set conservatively at launch. Plan to review
them after 30 days once we have a baseline of what "normal" looks like.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import boto3
import psycopg2

logger = logging.getLogger(__name__)


@dataclass
class DQRule:
    rule_id:        str
    rule_name:      str
    source:         str
    layer:          str                     # bronze | silver | gold
    severity:       str                     # MUST_PASS | HIGH | MEDIUM | LOW
    threshold:      float                   # minimum acceptable pass_rate (0-1)
    query:          str                     # SQL returning (total, passing, sample_failures jsonb)
    halt_on_fail:   bool = False            # if True, raise exception on MUST_PASS failure


@dataclass
class DQResult:
    rule:               DQRule
    total_records:      int
    passing_records:    int
    failing_records:    int
    pass_rate:          float
    status:             str                 # PASS | FAIL
    sample_failures:    list = field(default_factory=list)
    evaluated_at:       datetime = field(default_factory=lambda: datetime.now(timezone.utc))


RULES: list[DQRule] = [
    DQRule(
        rule_id="DQ-AECB-01",
        rule_name="Emirates ID format: 15-18 numeric characters",
        source="aecb_parsed", layer="silver", severity="MUST_PASS",
        threshold=1.00, halt_on_fail=True,
        query="""
            SELECT
                COUNT(*)                                                          AS total,
                SUM(CASE WHEN emirates_id ~ '^\\d{15,18}$' THEN 1 ELSE 0 END)   AS passing,
                (SELECT jsonb_agg(id) FROM (
                    SELECT id FROM silver.aecb_parsed
                    WHERE emirates_id !~ '^\\d{15,18}$' LIMIT 10
                ) s)                                                              AS sample_failures
            FROM silver.aecb_parsed
            WHERE parsed_at >= NOW() - INTERVAL '1 day'
        """,
    ),
    DQRule(
        rule_id="DQ-AECB-02",
        rule_name="Credit score within valid AECB range (300-900)",
        source="aecb_parsed", layer="silver", severity="MUST_PASS",
        threshold=1.00, halt_on_fail=True,
        query="""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN credit_score BETWEEN 300 AND 900 THEN 1 ELSE 0 END) AS passing,
                NULL::jsonb AS sample_failures
            FROM silver.aecb_parsed
            WHERE parsed_at >= NOW() - INTERVAL '1 day'
        """,
    ),
    DQRule(
        rule_id="DQ-AECB-03",
        rule_name="AECB report freshness within 90 days",
        source="aecb_parsed", layer="silver", severity="HIGH",
        threshold=0.95,
        query="""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN report_date >= CURRENT_DATE - 90 THEN 1 ELSE 0 END) AS passing,
                NULL::jsonb AS sample_failures
            FROM silver.aecb_parsed
            WHERE parsed_at >= NOW() - INTERVAL '1 day'
        """,
    ),
    DQRule(
        rule_id="DQ-FRAUD-01",
        rule_name="Fraud score in range [0, 1]",
        source="fraud_scores", layer="silver", severity="MUST_PASS",
        threshold=1.00, halt_on_fail=True,
        query="""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN fraud_score BETWEEN 0 AND 1 THEN 1 ELSE 0 END) AS passing,
                NULL::jsonb AS sample_failures
            FROM silver.fraud_scores
            WHERE parsed_at >= NOW() - INTERVAL '1 day'
        """,
    ),
    DQRule(
        rule_id="DQ-FRAUD-02",
        rule_name="Fraud score freshness within 24 hours",
        source="fraud_scores", layer="silver", severity="HIGH",
        threshold=0.99,
        query="""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN scored_at >= NOW() - INTERVAL '24 hours' THEN 1 ELSE 0 END) AS passing,
                NULL::jsonb AS sample_failures
            FROM silver.fraud_scores
            WHERE parsed_at >= NOW() - INTERVAL '1 day'
        """,
    ),
    DQRule(
        rule_id="DQ-AML-01",
        rule_name="AML overall_risk_level is not null",
        source="aml_results", layer="silver", severity="MUST_PASS",
        threshold=1.00, halt_on_fail=True,
        query="""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN overall_risk_level IS NOT NULL THEN 1 ELSE 0 END) AS passing,
                NULL::jsonb AS sample_failures
            FROM silver.aml_results
            WHERE parsed_at >= NOW() - INTERVAL '1 day'
        """,
    ),
    DQRule(
        rule_id="DQ-CUST-01",
        rule_name="Customer monthly income is positive",
        source="customer_profiles", layer="silver", severity="HIGH",
        threshold=0.98,
        query="""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN monthly_income_aed > 0 THEN 1 ELSE 0 END) AS passing,
                (SELECT jsonb_agg(id) FROM (
                    SELECT id FROM silver.customer_profiles
                    WHERE monthly_income_aed IS NULL OR monthly_income_aed <= 0 LIMIT 10
                ) s) AS sample_failures
            FROM silver.customer_profiles
            WHERE parsed_at >= NOW() - INTERVAL '1 day'
        """,
    ),
    DQRule(
        rule_id="DQ-GOLD-01",
        rule_name="All 4 source IDs present for approved decisions",
        source="credit_decision_inputs", layer="gold", severity="MUST_PASS",
        threshold=1.00, halt_on_fail=True,
        query="""
            SELECT
                COUNT(*) AS total,
                SUM(CASE
                    WHEN silver_aecb_id IS NOT NULL AND silver_fraud_id IS NOT NULL
                     AND silver_aml_id IS NOT NULL AND silver_profile_id IS NOT NULL
                    THEN 1 ELSE 0 END) AS passing,
                NULL::jsonb AS sample_failures
            FROM gold.credit_decision_inputs
            WHERE decision_outcome = 'APPROVED'
              AND snapshot_created_at >= NOW() - INTERVAL '1 day'
        """,
    ),
]


class DQScorecardEngine:
    def __init__(self, db_conn, sns_topic_arn: str | None = None):
        self._conn = db_conn
        self._sns_topic_arn = sns_topic_arn
        self._run_id = str(uuid.uuid4())

    def run(self, rules: list[DQRule] | None = None) -> list[DQResult]:
        rules = rules or RULES
        results: list[DQResult] = []
        halt_triggered = False

        for rule in rules:
            result = self._evaluate(rule)
            results.append(result)
            self._persist(result)

            if result.status == "FAIL":
                logger.warning("DQ rule %s FAILED (pass_rate=%.4f, threshold=%.4f)",
                               rule.rule_id, result.pass_rate, rule.threshold)
                if rule.severity in ("MUST_PASS", "HIGH"):
                    self._alert(result)
                if rule.halt_on_fail and rule.severity == "MUST_PASS":
                    halt_triggered = True
            else:
                logger.info("DQ rule %s PASSED (%.4f)", rule.rule_id, result.pass_rate)

        if halt_triggered:
            raise RuntimeError(
                f"DQ run {self._run_id}: one or more MUST_PASS rules failed — pipeline halted"
            )

        return results

    def _evaluate(self, rule: DQRule) -> DQResult:
        with self._conn.cursor() as cur:
            cur.execute(rule.query)
            row = cur.fetchone()
        total = int(row[0] or 0)
        passing = int(row[1] or 0)
        sample_failures = row[2] or []
        failing = total - passing
        pass_rate = round(passing / total, 4) if total > 0 else 1.0
        status = "PASS" if pass_rate >= rule.threshold else "FAIL"
        return DQResult(
            rule=rule,
            total_records=total,
            passing_records=passing,
            failing_records=failing,
            pass_rate=pass_rate,
            status=status,
            sample_failures=sample_failures,
        )

    def _persist(self, result: DQResult):
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO gold.dq_scorecard
                    (run_id, layer, source, rule_id, rule_name, severity,
                     total_records, passing_records, failing_records, pass_rate, threshold, sample_failures)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    self._run_id,
                    result.rule.layer,
                    result.rule.source,
                    result.rule.rule_id,
                    result.rule.rule_name,
                    result.rule.severity,
                    result.total_records,
                    result.passing_records,
                    result.failing_records,
                    result.pass_rate,
                    result.rule.threshold,
                    json.dumps(result.sample_failures) if result.sample_failures else None,
                ),
            )
        self._conn.commit()

    def _alert(self, result: DQResult):
        if not self._sns_topic_arn:
            return
        sns = boto3.client("sns")
        message = {
            "run_id": self._run_id,
            "rule_id": result.rule.rule_id,
            "rule_name": result.rule.rule_name,
            "severity": result.rule.severity,
            "source": result.rule.source,
            "pass_rate": result.pass_rate,
            "threshold": result.rule.threshold,
            "failing_records": result.failing_records,
            "evaluated_at": result.evaluated_at.isoformat(),
        }
        sns.publish(
            TopicArn=self._sns_topic_arn,
            Subject=f"[DQ ALERT] {result.rule.severity}: {result.rule.rule_id} FAILED",
            Message=json.dumps(message, indent=2),
        )
