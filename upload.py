"""
upload.py — Envoie output/short.mp4 sur YouTube via l'API officielle.

Auth (a faire UNE fois en local) :
  1. Google Cloud Console -> cree un projet -> active "YouTube Data API v3".
  2. Ecran de consentement OAuth (type "Desktop") -> telecharge client_secret.json ici.
  3. Lance :  python upload.py --auth
     -> ouvre le navigateur, tu autorises, ca cree token.json.
  4. Copie le CONTENU de token.json dans un secret GitHub "YT_TOKEN"
     (et client_secret.json dans "YT_CLIENT_SECRET") pour l'automatisation.

Ensuite, en local ou sur GitHub Actions :
  python upload.py            # lit output/short.mp4 + output/meta.json
"""

import json
import os
import sys
from pathlib import Path

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# force-ssl couvre l'upload ET la lecture/reponse aux commentaires
SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
TOKEN = Path("token.json")
CLIENT = Path("client_secret.json")


def _write_from_env():
    """Sur GitHub Actions : reconstruit les fichiers depuis les secrets."""
    if os.getenv("YT_CLIENT_SECRET") and not CLIENT.exists():
        CLIENT.write_text(os.environ["YT_CLIENT_SECRET"])
    if os.getenv("YT_TOKEN") and not TOKEN.exists():
        TOKEN.write_text(os.environ["YT_TOKEN"])


def auth_flow():
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN.write_text(creds.to_json())
    print("token.json cree. Copie son contenu dans le secret GitHub YT_TOKEN.")


def upload():
    _write_from_env()
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    yt = build("youtube", "v3", credentials=creds)

    meta = json.loads(Path("output/meta.json").read_text(encoding="utf-8"))
    body = {
        "snippet": {
            "title": meta["title"][:100],
            "description": meta["description"][:4900],
            "tags": meta.get("tags", []),
            "categoryId": "27",           # Education
        },
        "status": {"privacyStatus": os.getenv("YT_PRIVACY", "public"),
                   "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload("output/short.mp4", chunksize=-1, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = req.execute()
    print(f"Publie : https://youtu.be/{resp['id']}")


if __name__ == "__main__":
    if "--auth" in sys.argv:
        auth_flow()
    else:
        upload()
