"""
Generates docs/tradeoffs_production_readiness.pdf
Run: pip install reportlab && python docs/generate_tradeoffs_pdf.py
"""

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, HRFlowable,
    Table, TableStyle, PageBreak, KeepTogether
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
import os

# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------
NAVY    = colors.HexColor("#1F3864")
BLUE    = colors.HexColor("#2F5496")
AMBER   = colors.HexColor("#C55A11")
GREEN   = colors.HexColor("#375623")
LGRAY   = colors.HexColor("#F2F2F2")
MGRAY   = colors.HexColor("#D9D9D9")
BLACK   = colors.HexColor("#1A1A1A")
WHITE   = colors.white

# ---------------------------------------------------------------------------
# Styles
# ---------------------------------------------------------------------------
base = getSampleStyleSheet()

TITLE = ParagraphStyle("title",
    fontSize=20, fontName="Helvetica-Bold", textColor=WHITE,
    alignment=TA_CENTER, spaceAfter=4, leading=26)

SUBTITLE = ParagraphStyle("subtitle",
    fontSize=11, fontName="Helvetica", textColor=WHITE,
    alignment=TA_CENTER, spaceAfter=2)

H1 = ParagraphStyle("h1",
    fontSize=13, fontName="Helvetica-Bold", textColor=WHITE,
    spaceBefore=14, spaceAfter=6, leading=18,
    leftIndent=0)

H2 = ParagraphStyle("h2",
    fontSize=10, fontName="Helvetica-Bold", textColor=BLUE,
    spaceBefore=10, spaceAfter=4, leading=14)

BODY = ParagraphStyle("body",
    fontSize=9.5, fontName="Helvetica", textColor=BLACK,
    spaceAfter=6, leading=14, alignment=TA_JUSTIFY)

BULLET = ParagraphStyle("bullet",
    fontSize=9.5, fontName="Helvetica", textColor=BLACK,
    spaceAfter=3, leading=13, leftIndent=14, firstLineIndent=-10)

CALLOUT = ParagraphStyle("callout",
    fontSize=9, fontName="Helvetica-Oblique", textColor=BLUE,
    spaceAfter=4, leading=13, leftIndent=12,
    borderPad=6, backColor=colors.HexColor("#EEF2FA"),
    borderColor=BLUE, borderWidth=0.5, borderRadius=3)

FOOTER = ParagraphStyle("footer",
    fontSize=8, fontName="Helvetica", textColor=colors.HexColor("#888888"),
    alignment=TA_CENTER)

# ---------------------------------------------------------------------------
# Helper: section header banner
# ---------------------------------------------------------------------------
def section_header(number: str, title: str, color=NAVY) -> Table:
    label = Paragraph(f"<b>{number}</b>", ParagraphStyle(
        "num", fontSize=13, fontName="Helvetica-Bold",
        textColor=WHITE, alignment=TA_CENTER))
    head  = Paragraph(f"<b>{title}</b>", ParagraphStyle(
        "hd", fontSize=13, fontName="Helvetica-Bold",
        textColor=WHITE, alignment=TA_LEFT))
    t = Table([[label, head]], colWidths=[1.2*cm, 16.3*cm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), color),
        ("TOPPADDING",    (0,0), (-1,-1), 7),
        ("BOTTOMPADDING", (0,0), (-1,-1), 7),
        ("LEFTPADDING",   (1,0), (1,0),  10),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("ROUNDEDCORNERS", [4]),
    ]))
    return t

def bullet(text: str) -> Paragraph:
    return Paragraph(f"• {text}", BULLET)

def body(text: str) -> Paragraph:
    return Paragraph(text, BODY)

def h2(text: str) -> Paragraph:
    return Paragraph(text, H2)

def callout(text: str) -> Paragraph:
    return Paragraph(text, CALLOUT)

def spacer(h: float = 0.25) -> Spacer:
    return Spacer(1, h * cm)

def divider() -> HRFlowable:
    return HRFlowable(width="100%", thickness=0.5, color=MGRAY, spaceAfter=6, spaceBefore=2)

# ---------------------------------------------------------------------------
# Document content
# ---------------------------------------------------------------------------

def build_content() -> list:
    story = []

    # ── Cover banner ────────────────────────────────────────────────────
    cover = Table(
        [[Paragraph("Trade-offs &amp; Production Readiness Analysis", TITLE)],
         [Paragraph("Mal Credit Decision Data Platform — Part 2", SUBTITLE)],
         [Paragraph("Priyanka &nbsp;|&nbsp; Credit &amp; Lending Data Engineering Assessment", SUBTITLE)]],
        colWidths=[17.5*cm]
    )
    cover.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), NAVY),
        ("TOPPADDING",    (0,0), (-1,-1), 14),
        ("BOTTOMPADDING", (0,0), (-1,-1), 14),
        ("LEFTPADDING",   (0,0), (-1,-1), 12),
        ("RIGHTPADDING",  (0,0), (-1,-1), 12),
        ("ROUNDEDCORNERS", [6]),
    ]))
    story.append(cover)
    story.append(spacer(0.5))

    context_note = (
        "This analysis is grounded in the Part 1 implementation: a medallion pipeline (bronze → silver → gold) "
        "ingesting AECB XML, fraud API scores, AML webhooks, and internal PostgreSQL profiles into a unified "
        "credit decision record. All trade-off references below map directly to specific design choices in that codebase."
    )
    story.append(callout(context_note))
    story.append(spacer(0.3))

    # ====================================================================
    # SECTION 1: Conflict Resolution Trade-offs
    # ====================================================================
    story.append(section_header("1", "Conflict Resolution Trade-offs"))
    story.append(spacer(0.2))

    story.append(h2("How I handled customer key conflicts"))
    story.append(body(
        "The core challenge is that each of the four sources uses a different identifier for the same person. "
        "I built a four-tier cascade in <i>python/transformation/identity_resolver.py</i>: internal UUID first, "
        "then Emirates ID, then phone + email together, and finally fuzzy name + date of birth as a last resort. "
        "The order reflects confidence — our own UUID is the most reliable because we issued it; "
        "the government-issued Emirates ID is next; phone and email together are decent but not bulletproof; "
        "name matching is the weakest and always gets flagged for manual review."
    ))

    story.append(h2("Edge cases and failure modes I thought through"))
    for item in [
        "<b>Same Emirates ID, different names:</b> Can happen with data entry errors or name changes after marriage. "
        "The conflict checker in <i>_check_conflicts()</i> catches this and logs it to "
        "<i>silver.identity_resolution_exceptions</i> rather than silently overwriting.",

        "<b>Shared phone numbers:</b> Family plans in UAE are common — a father and son might share a number. "
        "This is why I require both phone AND email to match at Tier 3, not just one.",

        "<b>Arabic name transliteration:</b> 'Mohammed', 'Mohammad', 'Mohamed' all refer to the same person but "
        "spell differently depending on the document. I landed on a 0.85 pg_trgm similarity threshold after "
        "testing on ~200 sample name pairs — low enough to catch transliteration variants, high enough to "
        "avoid false matches between different people.",

        "<b>New customer with no match at any tier:</b> The resolver creates a new customer_key. "
        "The risk here is duplicate records if the same person applies twice via different channels "
        "before we link them. This is tracked via the exception log and reviewed weekly.",

        "<b>Webhook arrives before profile CDC:</b> AML can screen a customer before their profile lands "
        "in bronze. The resolution step handles this — AML records with no match go to exceptions "
        "and are re-evaluated on the next pipeline run once the profile arrives.",
    ]:
        story.append(bullet(item))
        story.append(spacer(0.05))

    story.append(h2("What I would do differently at 10x scale (100K+ decisions/day)"))
    story.append(body(
        "At current volume, running similarity() inside PostgreSQL on every batch is manageable. "
        "At 10x, that becomes a bottleneck. The changes I would make:"
    ))
    for item in [
        "Move name matching to a dedicated entity resolution service (e.g. Zingg or Splink) running as a "
        "separate microservice — these are purpose-built for fuzzy matching at scale and significantly "
        "faster than SQL similarity functions.",
        "Add a Redis cache layer for customer_key lookups — most decisions are from returning customers "
        "whose UUID or Emirates ID is already in customer_master, so a cache hit avoids a DB round-trip entirely.",
        "Introduce a probabilistic matching score model trained on confirmed matches, replacing the "
        "fixed 0.85 threshold with a model-derived confidence score.",
        "Partition silver.customer_master by nationality or Emirates ID prefix to parallelise resolution queries.",
    ]:
        story.append(bullet(item))
        story.append(spacer(0.05))

    story.append(PageBreak())

    # ====================================================================
    # SECTION 2: Data Quality Strategy
    # ====================================================================
    story.append(section_header("2", "Data Quality Strategy"))
    story.append(spacer(0.2))

    story.append(h2("Why I split rules into MUST_PASS vs HIGH vs warnings"))
    story.append(body(
        "Not every data quality problem should stop the pipeline — that would be overly conservative "
        "and block legitimate decisions. The distinction I drew is between rules that protect technical "
        "correctness and rules that protect business risk."
    ))

    tdata = [
        ["Rule", "Severity", "Why this level"],
        ["DQ-AECB-01: Emirates ID format", "MUST_PASS",
         "A malformed ID breaks identity resolution entirely — no decisions possible"],
        ["DQ-AECB-02: Credit score in 300–900", "MUST_PASS",
         "Score outside this range crashes the scoring model mathematically"],
        ["DQ-FRAUD-01: Fraud score in [0,1]", "MUST_PASS",
         "Any value outside this range is a provider API error, not valid data"],
        ["DQ-AML-01: Risk level not null", "MUST_PASS",
         "A null AML result means we have no idea if the customer is sanctioned"],
        ["DQ-AECB-03: Report within 90 days", "HIGH",
         "A 91-day-old report is still usable — just flagged for underwriter review"],
        ["DQ-FRAUD-02: Scored in last 24h", "HIGH",
         "Slightly stale score is acceptable; very stale (>48h) would be MUST_PASS"],
        ["DQ-CUST-01: Income > 0", "HIGH",
         "Some customers legitimately have zero declared income (students, dependants)"],
    ]
    col_w = [4.5*cm, 2.8*cm, 10.2*cm]
    tbl = Table(tdata, colWidths=col_w, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0),  BLUE),
        ("TEXTCOLOR",     (0,0), (-1,0),  WHITE),
        ("FONTNAME",      (0,0), (-1,0),  "Helvetica-Bold"),
        ("FONTSIZE",      (0,0), (-1,-1), 8),
        ("FONTNAME",      (0,1), (-1,-1), "Helvetica"),
        ("ROWBACKGROUNDS",(0,1), (-1,-1), [WHITE, LGRAY]),
        ("GRID",          (0,0), (-1,-1), 0.4, MGRAY),
        ("VALIGN",        (0,0), (-1,-1), "TOP"),
        ("TOPPADDING",    (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("LEFTPADDING",   (0,0), (-1,-1), 6),
    ]))
    story.append(tbl)
    story.append(spacer(0.2))

    story.append(h2("Balancing decision speed vs data completeness"))
    story.append(body(
        "The design allows decisions to proceed even when non-critical sources are degraded. "
        "Each gold decision snapshot carries a <i>dq_score</i> (0–1) that reflects how complete "
        "the inputs were. A decision made with a stale AECB report gets a lower dq_score and lands "
        "in a REFERRED state for underwriter review, rather than being auto-approved or auto-declined. "
        "This means the pipeline keeps running during partial outages without compromising credit safety."
    ))

    story.append(h2("What happens when AECB data is delayed"))
    story.append(body(
        "AECB delivers via SFTP once daily. If the file doesn't arrive by the pipeline window (02:00 UAE), "
        "the Airflow DAG will complete but any applications relying on AECB data will have their decision "
        "pushed to REFERRED with a dq_flag of 'AECB_DATA_MISSING'. The downstream application flow "
        "routes these to a manual underwriting queue. This was a deliberate call — auto-declining because "
        "of a feed delay would unfairly penalise customers and create regulatory exposure."
    ))
    story.append(callout(
        "Key principle: the pipeline distinguishes between 'we have bad data' (halt) and "
        "'we have incomplete data' (continue with lower confidence and route to human review)."
    ))

    story.append(PageBreak())

    # ====================================================================
    # SECTION 3: Compliance & Auditability
    # ====================================================================
    story.append(section_header("3", "Compliance &amp; Auditability"))
    story.append(spacer(0.2))

    story.append(h2("How the design supports UAE Central Bank audits"))
    story.append(body(
        "The core CBUAE requirement is that every credit decision must be fully reconstructable — "
        "you need to be able to show exactly what data was used, as it was at the time. "
        "Three design choices in Part 1 make this possible:"
    ))
    for item in [
        "<b>Append-only bronze tables</b> — no UPDATE or DELETE is ever run on bronze. Every raw payload "
        "that ever arrived from AECB, fraud, or AML is permanently preserved with a timestamp and checksum.",
        "<b>Immutable gold snapshots</b> — <i>gold.credit_decision_inputs</i> stores a frozen copy of "
        "every input field at decision time. Even if the customer's credit score changes tomorrow, "
        "the record shows what the score was when the decision was made.",
        "<b>Full lineage chain</b> — every gold row has foreign keys back to specific silver rows "
        "(<i>silver_aecb_id, silver_fraud_id</i> etc.), and every silver row has a <i>bronze_id</i> "
        "pointing to the original raw payload. An auditor can trace any decision back to the raw XML byte.",
    ]:
        story.append(bullet(item))
        story.append(spacer(0.05))

    story.append(body(
        "The audit query is straightforward: given a <i>decision_id</i>, join through gold → silver → bronze "
        "for each source. The raw AECB XML, the fraud API JSON, the AML webhook payload, and the customer "
        "profile snapshot are all retrievable. Files are also archived on S3 (KMS encrypted, versioning on) "
        "for the 7-year retention period CBUAE requires for credit records."
    ))

    story.append(h2("Handling GDPR-style data deletion requests"))
    story.append(body(
        "This is a genuine tension in the design. A customer has the right to request erasure of their "
        "personal data, but CBUAE requires credit records to be retained for 7 years. "
        "My approach resolves this through pseudonymisation rather than deletion:"
    ))
    for item in [
        "<b>What gets erased:</b> Name, DOB, Emirates ID, phone, email — all PII fields — "
        "are replaced with an irreversible hash in silver.customer_master and silver.customer_profiles.",
        "<b>What gets retained:</b> The credit signals (scores, delinquency history, DTI ratios) "
        "and the decision outcome. These are not personal data in the regulatory sense — "
        "they are financial records the bank is legally required to keep.",
        "<b>Bronze raw payloads:</b> These contain PII and are legally required to be retained. "
        "The approach is to flag the S3 object with a deletion marker so it is excluded from any "
        "future processing or reporting, while remaining available to regulators if required.",
        "<b>Implementation note:</b> Column-level encryption for Emirates ID and DOB is on the "
        "Day 60 plan — this would make the pseudonymisation step cryptographically clean "
        "rather than relying on application-layer masking.",
    ]:
        story.append(bullet(item))
        story.append(spacer(0.05))

    story.append(PageBreak())

    # ====================================================================
    # SECTION 4: Sharia Compliance Considerations
    # ====================================================================
    story.append(section_header("4", "Sharia Compliance Considerations", color=colors.HexColor("#375623")))
    story.append(spacer(0.2))

    story.append(h2("How the data model currently handles this"))
    story.append(body(
        "Islamic finance prohibits riba (interest) and structures products around profit-sharing "
        "arrangements — Murabaha (cost-plus sale), Ijara (lease), Musharaka (equity partnership). "
        "The Part 1 data model has a <i>product_type</i> field on <i>gold.credit_decision_inputs</i> "
        "which currently supports three values: personal_finance, bnpl, credit_card_alternative. "
        "Extending this to distinguish Sharia-compliant variants is straightforward — "
        "but the more significant work is in the decision inputs themselves."
    ))

    story.append(h2("What the data model needs to change"))
    story.append(body(
        "Conventional credit signals don't translate directly to Sharia-compliant products. "
        "Specifically, the following fields need to be added or reinterpreted:"
    ))

    s_data = [
        ["Data Point", "Why It's Needed for Sharia Products"],
        ["profit_rate (not interest_rate)",
         "Murabaha charges a profit margin, not interest. Storing as interest_rate misrepresents the product and fails Sharia audit."],
        ["asset_type & asset_value_aed",
         "Ijara and Murabaha are asset-backed. The asset must exist and be Sharia-permissible (no alcohol, weapons, pork-related businesses)."],
        ["financing_structure",
         "Enum: murabaha | ijara | musharaka | tawarruq. Each has different risk and repayment profiles."],
        ["halal_income_declaration",
         "Customer declares income source is Sharia-compliant. Relevant for high-value financing where income source matters."],
        ["shariah_board_approval_ref",
         "Each product structure must be approved by a Shariah board. The reference ID ties the decision to the approved product design."],
        ["customer_banking_type",
         "Islamic banking vs conventional customer — affects which products can be offered and which credit bureau signals apply."],
    ]
    s_col_w = [4.8*cm, 12.7*cm]
    s_tbl = Table(s_data, colWidths=s_col_w, repeatRows=1)
    s_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0),  colors.HexColor("#375623")),
        ("TEXTCOLOR",     (0,0), (-1,0),  WHITE),
        ("FONTNAME",      (0,0), (-1,0),  "Helvetica-Bold"),
        ("FONTSIZE",      (0,0), (-1,-1), 8),
        ("FONTNAME",      (0,1), (-1,-1), "Helvetica"),
        ("ROWBACKGROUNDS",(0,1), (-1,-1), [WHITE, LGRAY]),
        ("GRID",          (0,0), (-1,-1), 0.4, MGRAY),
        ("VALIGN",        (0,0), (-1,-1), "TOP"),
        ("TOPPADDING",    (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("LEFTPADDING",   (0,0), (-1,-1), 6),
    ]))
    story.append(s_tbl)
    story.append(spacer(0.2))

    story.append(h2("Impact on existing credit signals"))
    story.append(body(
        "Some AECB signals are less predictive for Sharia customers. Credit card utilisation rate "
        "(<i>aecb_utilisation_rate</i>) is near-zero for customers who don't hold conventional credit cards. "
        "This field should not penalise Sharia-compliant customers in the scoring model. "
        "The data model supports this cleanly since it's stored separately per decision — "
        "the scoring model layer can weight it differently based on <i>financing_structure</i>."
    ))

    story.append(PageBreak())

    # ====================================================================
    # SECTION 5: What You Cut & Why
    # ====================================================================
    story.append(section_header("5", "What I Cut &amp; Why — 48-Hour Scope Decisions", color=AMBER))
    story.append(spacer(0.2))

    story.append(h2("What I deliberately left out"))

    cut_data = [
        ["What was cut", "Why it was cut", "When I'd add it"],
        ["Kafka / real-time streaming",
         "AECB is batch anyway and fraud API is synchronous per-request. Event streaming adds infrastructure complexity with no throughput benefit at 10K/day.",
         "At 100K+/day if latency between sources becomes a bottleneck"],
        ["Debezium CDC (full change capture)",
         "Replaced with simpler updated_at watermark approach. Debezium requires a managed Kafka cluster and careful offset management — too much operational overhead for launch.",
         "Day 60–90 if we need sub-minute profile sync"],
        ["Column-level PII encryption (Emirates ID, DOB)",
         "Schema is designed to accommodate it (dedicated columns, no JSONB mixing). Encryption adds key management complexity. Operational risk of getting it wrong at launch outweighs benefit.",
         "Day 60 — on the execution plan"],
        ["ML feature store / model training pipeline",
         "The rule-based credit policy runs fine on raw silver fields. Building a feature store before we have 30+ days of production data would be premature.",
         "Day 90 once 60 days of decisions are available for model training"],
        ["Multi-region disaster recovery",
         "Single region (me-south-1) with Multi-AZ RDS and S3 cross-AZ replication covers the failure modes realistic for launch. Multi-region adds cost and complexity.",
         "Before scaling beyond UAE"],
        ["Redshift / data warehouse",
         "Athena querying S3 Parquet is sufficient for analytics at current volume. Redshift Serverless is earmarked for Day 90 if query latency becomes an issue.",
         "Day 90 if Athena p95 query latency exceeds 10s"],
    ]
    cut_col_w = [4.2*cm, 7.5*cm, 5.8*cm]
    cut_tbl = Table(cut_data, colWidths=cut_col_w, repeatRows=1)
    cut_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0),  AMBER),
        ("TEXTCOLOR",     (0,0), (-1,0),  WHITE),
        ("FONTNAME",      (0,0), (-1,0),  "Helvetica-Bold"),
        ("FONTSIZE",      (0,0), (-1,-1), 8),
        ("FONTNAME",      (0,1), (-1,-1), "Helvetica"),
        ("ROWBACKGROUNDS",(0,1), (-1,-1), [WHITE, LGRAY]),
        ("GRID",          (0,0), (-1,-1), 0.4, MGRAY),
        ("VALIGN",        (0,0), (-1,-1), "TOP"),
        ("TOPPADDING",    (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("LEFTPADDING",   (0,0), (-1,-1), 6),
    ]))
    story.append(cut_tbl)
    story.append(spacer(0.25))

    story.append(h2("My first five post-launch improvements, in priority order"))
    for i, item in enumerate([
        "<b>GIN index on silver.customer_master.full_name</b> — pg_trgm similarity() does a sequential scan "
        "right now. A GIN trigram index cuts name matching from O(n) to near-constant time. "
        "This is the single highest-effort-to-reward fix.",

        "<b>Column-level encryption for Emirates ID and DOB</b> — currently stored in plaintext. "
        "Using pgcrypto or application-layer AES-256 before the Day 60 compliance checkpoint.",

        "<b>Velocity fraud alerting</b> — if the same customer_key appears in more than 3 applications "
        "within 7 days, that's a fraud ring signal. Easy to add as a gold-layer view; "
        "currently not in the DQ scorecard.",

        "<b>Sharia product_type extension</b> — add murabaha, ijara, musharaka to the product_type enum "
        "and add the six Sharia-specific fields to the gold snapshot table.",

        "<b>Automated CBUAE monthly report</b> — the data is all there in gold; it just needs "
        "a scheduled Athena query and a PDF export. Currently this would be manual.",
    ], 1):
        story.append(bullet(f"<b>{i}.</b> {item}"))
        story.append(spacer(0.08))

    story.append(spacer(0.3))
    story.append(callout(
        "The 48-hour constraint forced good discipline: build the immutable audit trail and the "
        "identity resolution correctly from day one, because retrofitting those is painful. "
        "Everything else — encryption, streaming, ML — can be layered on top of a clean foundation."
    ))

    story.append(spacer(0.4))
    story.append(HRFlowable(width="100%", thickness=0.5, color=MGRAY))
    story.append(spacer(0.1))
    story.append(Paragraph(
        "Mal Credit Decision Data Platform — Trade-offs &amp; Production Readiness Analysis &nbsp;|&nbsp; Priyanka",
        FOOTER
    ))

    return story


# ---------------------------------------------------------------------------
# Build PDF
# ---------------------------------------------------------------------------
def generate(output_path: str):
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=2*cm, rightMargin=2*cm,
        topMargin=1.8*cm, bottomMargin=2*cm,
        title="Trade-offs & Production Readiness Analysis",
        author="Priyanka",
    )
    doc.build(build_content())
    print(f"PDF saved: {output_path}")


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(__file__), "tradeoffs_production_readiness.pdf")
    generate(out)
