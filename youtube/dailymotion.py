"""Republie sur Dailymotion un clip deja parti sur YouTube.

    python youtube/dailymotion.py <id> --clip 3             publie le clip 3
    python youtube/dailymotion.py <id> --clip 3 --simuler   n'envoie rien, montre tout

Appele par soir.py juste apres la publication YouTube. Un echec ici ne touche
jamais YouTube : on le note dans le log et la soiree continue.

Il faut dans le .env une cle API PRIVEE (espace partenaire Dailymotion) :
    DM_API_KEY=...  DM_API_SECRET=...  DM_CHANNEL=<nom de la chaine>
Sans ces trois variables, le module ne fait rien.

Journal : media/dailymotion.json (empeche de publier deux fois le meme clip).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import requests

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "clipper"))
sys.path.insert(0, str(RACINE / "youtube"))

import clipper as C          # noqa: E402
import publier as P          # noqa: E402

API = "https://partner.api.dailymotion.com"
JOURNAL = C.MEDIA / "dailymotion.json"


def actif() -> bool:
    return all(os.environ.get(v) for v in ("DM_API_KEY", "DM_API_SECRET", "DM_CHANNEL"))


def _journal() -> dict:
    return json.loads(JOURNAL.read_text(encoding="utf-8")) if JOURNAL.exists() else {}


def _jeton() -> str:
    r = requests.post(f"{API}/oauth/v1/token", timeout=30, data={
        "grant_type": "client_credentials", "scope": "manage_videos",
        "client_id": os.environ["DM_API_KEY"], "client_secret": os.environ["DM_API_SECRET"]})
    r.raise_for_status()
    return r.json()["access_token"]


def publier(vid: str, n: int, simuler: bool = False) -> str:
    idx = P.index(vid)
    clip = idx["clips"][n - 1]
    cle, jrn = f"{vid}_{n}", _journal()
    if cle in jrn:
        print(f"Dailymotion : deja publie le {jrn[cle]['date']}")
        return jrn[cle]["url"]
    auj = datetime.now().date().isoformat()
    du_jour = sum(1 for v in jrn.values() if v.get("date", "").startswith(auj))
    if du_jour >= P.par_jour():
        print(f"Dailymotion : deja {du_jour} publication(s) aujourd'hui, maximum {P.par_jour()}")
        return ""
    video = P.fichier(idx, clip)
    titre, desc = P.texte(idx, clip)
    titre = titre.replace("#Shorts", "").strip()[:255]
    print(f"Dailymotion : {video.name} -> {titre}")
    if simuler:
        print("--simuler : rien n'a ete envoye a Dailymotion.")
        return ""

    h = {"Authorization": f"Bearer {_jeton()}"}
    # 1) adresse d'envoi  2) envoi du fichier  3) creation de la video publique
    cible = requests.get(f"{API}/rest/file/upload", headers=h, timeout=30).json()["upload_url"]
    with open(video, "rb") as f:
        envoye = requests.post(cible, files={"file": f}, timeout=900).json()["url"]
    r = requests.post(f"{API}/rest/user/{os.environ['DM_CHANNEL']}/videos", headers=h,
                      timeout=60, data={
                          "url": envoye, "title": titre, "description": desc[:3000],
                          "channel": "news", "tags": "thinkerview,interview,politique",
                          "is_created_for_kids": "false", "published": "true"})
    r.raise_for_status()
    url = f"https://www.dailymotion.com/video/{r.json()['id']}"
    jrn[cle] = {"date": datetime.now().isoformat(timespec="minutes"), "url": url, "titre": titre}
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    JOURNAL.write_text(json.dumps(jrn, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Dailymotion : {url}")
    return url


def main() -> int:
    C.charger_env()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args or "--clip" not in sys.argv:
        print(__doc__)
        return 2
    if not actif():
        sys.exit("DM_API_KEY, DM_API_SECRET et DM_CHANNEL manquent dans le .env")
    publier(args[0], int(sys.argv[sys.argv.index("--clip") + 1]), "--simuler" in sys.argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
