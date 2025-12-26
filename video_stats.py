import requests
import json

import os
from dotenv import load_dotenv

load_dotenv(dotenv_path= "./.env")

YOUR_API_KEY = os.getenv("API_KEY")
Channel="MrBeast"
max_results=50

def get_playlist_id():
    try:
        url=f"https://youtube.googleapis.com/youtube/v3/channels?part=contentDetails&forHandle={Channel}&key={YOUR_API_KEY}"

        response= requests.get(url)

        response.raise_for_status()

        data = response.json()

        #print(json.dumps(data,indent=4))

        channel_item = data['items'][0]

        channel_playlist_id = channel_item['contentDetails']['relatedPlaylists']['uploads']

        print(channel_playlist_id)
        return channel_playlist_id
    
    except requests.exceptions.RequestException as e:
        raise e



def get_video_ids(playlist_id):

    video_ids= []

    pageToken= None

    base_url= f"https://youtube.googleapis.com/youtube/v3/playlistItems?part=contentDetails&maxResults={max_results}&playlistId={playlist_id}&key={YOUR_API_KEY}"

    try:
        while True:
            url= base_url

            if pageToken:
                url +=f"&pageToken={pageToken}"
            
            response= requests.get(url)

            response.raise_for_status()

            data = response.json()

            for item in data.get('items',[]):
                video_id = item['contentDetails']['videoId']
                video_ids.append(video_id)
            
            pageToken = data.get('nextPageToken')

            if not pageToken:
                break
        
        return video_ids

    except requests.exceptions.RequestException as e:
        raise e

if __name__ == "__main__":
    playlist_id= get_playlist_id()
    get_video_ids(playlist_id)