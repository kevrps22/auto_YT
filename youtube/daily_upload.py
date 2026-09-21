"""Publie UNE video par jour sur YouTube. Concu pour tourner sur un Raspberry Pi.

Le Pi ne GENERE rien : il pioche dans une file de videos deja produites sur le PC.
Il n'a donc besoin ni de ffmpeg, ni de Whisper, ni de torch — seulement de requests
et du client Google. Aucun portage ARM risque.

    python3 daily_upload.py                       # publie la prochaine video
    python3 daily_upload.py --dry-run             # simule, n'envoie rien
    python3 daily_upload.py --lib /media/queue --delete-after --notify

Options :
    --lib DOSSIER    file d'attente (defaut : output/lib)
    --delete-after   supprime la video du support une fois publiee
    --notify         previent par Telegram (publication, stock bas, erreurs)
    --low N          seuil d'alerte de stock bas (defaut 7)
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sys
import unicodedata
from datetime import date, datetime
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent.parent   # racine du projet
LOG = HERE / "upload.log"


# ------------------------------------------------------------------ utilitaires
def load_env() -> None:
    """Lit le .env local. Volontairement autonome : aucune dependance sur le
    reste du projet, pour tourner tel quel sur l'Orange Pi."""
    f = HERE / ".env"
    if not f.exists():
        return
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M}  {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def notify(msg: str, on: bool) -> None:
    """Notification Telegram. Silencieuse si le bot n'est pas configure."""
    if not on:
        return
    tok, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": chat, "text": msg}, timeout=20)
    except Exception as e:
        log(f"notification impossible : {type(e).__name__}")


def norm(t: str) -> str:
    """Titre comparable : YouTube retouche la ponctuation ('physique : L'effet'
    devient 'physique L'effet'), ce qui ferait passer une video publiee pour
    inedite — et provoquerait un doublon."""
    t = unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", t.lower())


# ------------------------------------------------------------------ YouTube
def published() -> tuple[set[str], str | None]:
    """Titres deja en ligne + date de la derniere publication."""
    key = os.environ["YT_API_KEY"]
    handle = os.getenv("YT_CHANNEL", "@Attends_Quoi").lstrip("@")
    ch = requests.get("https://www.googleapis.com/youtube/v3/channels",
                      params={"part": "contentDetails", "forHandle": handle,
                              "key": key}, timeout=30).json()
    up = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]

    titles, last, token = set(), None, None
    while True:
        p = {"part": "snippet,contentDetails", "playlistId": up,
             "maxResults": 50, "key": key}
        if token:
            p["pageToken"] = token
        r = requests.get("https://www.googleapis.com/youtube/v3/playlistItems",
                         params=p, timeout=30).json()
        for it in r.get("items", []):
            titles.add(norm(it["snippet"]["title"]))
            d = it["contentDetails"].get("videoPublishedAt", "")[:10]
            if d and (last is None or d > last):
                last = d
        token = r.get("nextPageToken")
        if not token:
            break
    return titles, last


def queue(lib: Path, titles: set[str]) -> list[tuple[int, Path, dict]]:
    """Videos en file d'attente, la plus virale d'abord."""
    out = []
    for d in sorted(glob.glob(str(lib / "*"))):
        f = Path(d) / "meta.json"
        if not f.exists():
            continue
        m = json.loads(f.read_text(encoding="utf-8"))
        if norm(m.get("title", "")) in titles:
            continue
        out.append((m.get("virality") or 0, Path(d), m))
    out.sort(key=lambda x: -x[0])
    return out


# ------------------------------------------------------------------ principal
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lib", default=str(HERE / "output" / "lib"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--delete-after", action="store_true")
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--low", type=int, default=7)
    a = ap.parse_args()

    load_env()
    lib = Path(a.lib)
    sys.path.insert(0, str(HERE))

    if not (HERE / "token.json").exists() or not (HERE / "client_secret.json").exists():
        log("ARRET : OAuth non configure. Lance une fois : python upload.py --auth")
        notify("⚠️ Attends Quoi : OAuth non configure, rien n'est publie.", a.notify)
        return 2

    titles, last = published()
    if last == str(date.today()):
        log(f"deja publie aujourd'hui ({last}) -> rien a faire")
        return 0

    q = queue(lib, titles)
    if not q:
        log(f"STOCK VIDE dans {lib} — plus rien a publier.")
        notify("🔴 Attends Quoi : STOCK VIDE, la chaine s'arrete. "
               "Genere de nouvelles videos et recharge la carte.", a.notify)
        return 3

    _, folder, meta = q[0]
    reste = len(q) - 1
    log(f"selection [{meta.get('virality')}/100] {meta.get('title')} ({reste} restantes)")

    if a.dry_run:
        log("--dry-run : rien n'a ete envoye")
        return 0

    try:
        import upload
        upload.upload(str(folder))
        log("PUBLIE OK")
    except SystemExit as e:
        # upload.load_creds() s'arrete par sys.exit quand l'autorisation est morte.
        # SystemExit n'herite PAS de Exception : sans ce bloc, le script mourait
        # SANS alerte -- c'est exactement ce qui a fait taire la chaine 24 jours.
        log(f"ARRET AUTH : {e}")
        notify(f"🔴 Attends Quoi : publication bloquee, autorisation a refaire.\n{e}",
               a.notify)
        return 2
    except Exception as e:
        log(f"ECHEC upload : {type(e).__name__} {e}")
        notify(f"⚠️ Attends Quoi : echec de publication\n{type(e).__name__} : {e}", a.notify)
        return 1

    if a.delete_after:
        try:
            shutil.rmtree(folder)
            log(f"supprimee du support : {folder.name}")
        except Exception as e:
            log(f"suppression impossible : {e}")

    # --- alerte de stock : le seul moment ou une action humaine est requise
    if reste == 0:
        notify("🔴 Attends Quoi : c'etait la DERNIERE video. Stock vide.", a.notify)
    elif reste <= a.low:
        notify(f"🟠 Attends Quoi : plus que {reste} videos "
               f"({reste} jours). Pense a refaire le plein.", a.notify)
    else:
        notify(f"✅ Attends Quoi : « {meta.get('title')} » publiee. "
               f"{reste} videos en stock.", a.notify)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
