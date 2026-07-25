"""
comments.py — Lit (et repond a) les commentaires de ta derniere video.

Pre-requis : avoir refait l'auth avec le nouveau scope force-ssl :
    python upload.py --auth
(puis remettre a jour le secret GitHub YT_TOKEN avec le nouveau token.json)

Usage :
  python comments.py                 # affiche les commentaires de la derniere video
  python comments.py --reply         # repond a UN commentaire (via Gemini), pertinent
  python comments.py VIDEO_ID        # cible une video precise
  python comments.py VIDEO_ID --reply
"""

import os
import random
import sys
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
TOKEN = Path("token.json")


def _load_env():
    env = Path(__file__).with_name(".env")
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _yt():
    if os.getenv("YT_TOKEN") and not TOKEN.exists():
        TOKEN.write_text(os.environ["YT_TOKEN"])
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    return build("youtube", "v3", credentials=creds)


def latest_video_id(yt) -> str:
    """Recupere l'ID de la derniere video mise en ligne sur la chaine."""
    ch = yt.channels().list(part="contentDetails", mine=True).execute()
    uploads = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    pl = yt.playlistItems().list(part="contentDetails", playlistId=uploads,
                                 maxResults=1).execute()
    return pl["items"][0]["contentDetails"]["videoId"]


def list_comments(yt, video_id, limit=50):
    """Renvoie [{id, author, text, likes}] des commentaires de premier niveau."""
    out = []
    req = yt.commentThreads().list(part="snippet", videoId=video_id,
                                   maxResults=min(limit, 100), order="time",
                                   textFormat="plainText")
    while req and len(out) < limit:
        resp = req.execute()
        for it in resp.get("items", []):
            top = it["snippet"]["topLevelComment"]
            s = top["snippet"]
            out.append({"id": top["id"], "author": s["authorDisplayName"],
                        "text": s["textDisplay"], "likes": s["likeCount"]})
        req = yt.commentThreads().list_next(req, resp)
    return out


def gemini_reply(video_title, comment_text) -> str:
    """Genere une reponse courte, chaleureuse et PERTINENTE au commentaire."""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return "Merci pour ton commentaire ! 🙌"
    from google import genai
    client = genai.Client(api_key=key)
    prompt = (
        f"Tu es le createur d'une chaine YouTube Shorts. Video : \"{video_title}\".\n"
        f"Un spectateur a commente : \"{comment_text}\".\n"
        "Ecris UNE reponse en francais : courte (1 phrase), chaleureuse, naturelle, "
        "qui repond VRAIMENT au commentaire (pas generique), tutoiement, "
        "au max 1 emoji. Reponds uniquement par la phrase, rien d'autre."
    )
    r = client.models.generate_content(model="gemini-flash-latest", contents=prompt)
    return r.text.strip().strip('"')


def reply_to_comment(yt, parent_id, text):
    yt.comments().insert(
        part="snippet",
        body={"snippet": {"parentId": parent_id, "textOriginal": text}},
    ).execute()


def main():
    _load_env()
    args = [a for a in sys.argv[1:] if a != "--reply"]
    do_reply = "--reply" in sys.argv
    yt = _yt()

    video_id = args[0] if args else latest_video_id(yt)
    title_resp = yt.videos().list(part="snippet", id=video_id).execute()
    title = title_resp["items"][0]["snippet"]["title"] if title_resp["items"] else ""
    print(f"=== Video : {title} ({video_id}) ===")

    comments = list_comments(yt, video_id)
    if not comments:
        print("Aucun commentaire pour l'instant.")
        return
    for c in comments:
        print(f"  [{c['likes']}👍] {c['author']}: {c['text']}")

    if do_reply:
        # on evite de repondre a un commentaire auquel on a deja repondu serait mieux,
        # ici on prend simplement un commentaire au hasard parmi les plus recents
        target = random.choice(comments[:10])
        answer = gemini_reply(title, target["text"])
        print(f"\n-> Reponse a {target['author']} : {answer}")
        reply_to_comment(yt, target["id"], answer)
        print("Reponse postee.")


if __name__ == "__main__":
    main()
