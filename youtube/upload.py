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
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# youtube.upload      : publier une video
# yt-analytics.readonly : LIRE nos propres courbes de retention (indispensable pour
#                         savoir a quelle seconde les gens decrochent)
# youtube.readonly      : lister nos videos et leurs identifiants
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/yt-analytics.readonly",
          "https://www.googleapis.com/auth/youtube.readonly"]
# a la racine du projet, pas dans youtube/ : ils servent aussi a analytics.py
RACINE = Path(__file__).resolve().parent.parent
TOKEN = RACINE / "token.json"
CLIENT = RACINE / "client_secret.json"


def _write_from_env():
    """Sur GitHub Actions : reconstruit les fichiers depuis les secrets."""
    if os.getenv("YT_CLIENT_SECRET") and not CLIENT.exists():
        CLIENT.write_text(os.environ["YT_CLIENT_SECRET"])
    if os.getenv("YT_TOKEN") and not TOKEN.exists():
        TOKEN.write_text(os.environ["YT_TOKEN"])


def auth_flow():
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT), SCOPES)
    # prompt=consent force Google a redonner un refresh_token meme si on a deja
    # autorise l'app ; sans ca une re-autorisation peut renvoyer un token sans refresh.
    creds = flow.run_local_server(port=0, prompt="consent")
    TOKEN.write_text(creds.to_json())
    print("token.json cree.")


def load_creds():
    """Charge le jeton et le rafraichit. Message clair si l'autorisation est morte
    (mode Test = refresh_token revoque tous les 7 jours) ou si les permissions
    ont change depuis la derniere autorisation."""
    _write_from_env()
    if not TOKEN.exists():
        sys.exit("token.json absent -> lance : python upload.py --auth")
    data = json.loads(TOKEN.read_text())
    manquantes = set(SCOPES) - set(data.get("scopes", []))
    if manquantes:
        sys.exit("Permissions manquantes dans token.json :\n  "
                 + "\n  ".join(sorted(manquantes))
                 + "\n-> relance : python upload.py --auth")
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN.write_text(creds.to_json())
        except RefreshError:
            sys.exit("Autorisation expiree (invalid_grant).\n"
                     "Cause habituelle : l'app OAuth est restee en mode Test, le\n"
                     "refresh_token y meurt au bout de 7 jours. Passe l'app en\n"
                     "PRODUCTION dans Google Cloud Console, puis :\n"
                     "  python upload.py --auth")
    return creds


def upload(base="output"):
    """Uploade base/short.mp4 avec base/meta.json (base = 'output' ou un dossier galerie)."""
    yt = build("youtube", "v3", credentials=load_creds())

    base = Path(base)
    meta = json.loads((base / "meta.json").read_text(encoding="utf-8"))
    # le fichier porte le titre de la video (ancien format : short.mp4)
    video = base / meta.get("file", "short.mp4")
    if not video.exists():
        mp4s = sorted(base.glob("*.mp4"))
        if not mp4s:
            sys.exit(f"Aucun .mp4 dans {base}")
        video = mp4s[0]
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
    media = MediaFileUpload(str(video), chunksize=-1, resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = req.execute()
    print(f"Publie : https://youtu.be/{resp['id']}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--auth" in sys.argv:
        auth_flow()
    else:
        upload(args[0] if args else "output")
