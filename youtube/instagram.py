"""Republie en Reel Instagram un clip deja parti sur YouTube.

    python youtube/instagram.py <id> --clip 3             publie le clip 3
    python youtube/instagram.py <id> --clip 3 --simuler   n'envoie rien, montre tout
    python youtube/instagram.py --verifier                 teste le jeton et le compte

Appele par soir.py juste apres YouTube et Dailymotion. Un echec ici ne touche
jamais YouTube : on le note dans le journal et la soiree continue.

API officielle et gratuite d'Instagram (« Instagram API with Instagram Login »),
compte professionnel (Createur). Une appli Meta en mode developpement publie
sans validation sur les comptes qui lui sont rattaches : ici, le notre.

Il faut dans le .env : IG_TOKEN=<jeton d'acces Instagram>
Le jeton dure 60 jours : on le renouvelle chaque semaine et on garde le dernier
dans media/instagram_jeton.json (le .env n'est jamais reecrit).

Les liens ne sont pas cliquables dans les legendes Instagram : la chaine YouTube
est dans la bio du compte, la legende y renvoie.

Journal : media/instagram.json (empeche de publier deux fois le meme clip).
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "clipper"))
sys.path.insert(0, str(RACINE / "youtube"))

import clipper as C          # noqa: E402
import publier as P          # noqa: E402

API = "https://graph.instagram.com/v23.0"
JOURNAL = C.MEDIA / "instagram.json"
JETON = C.MEDIA / "instagram_jeton.json"
HASHTAGS = "#thinkerview #interview #debat #france #reels"


def actif() -> bool:
    return bool(os.environ.get("IG_TOKEN") or JETON.exists())


def _journal() -> dict:
    return json.loads(JOURNAL.read_text(encoding="utf-8")) if JOURNAL.exists() else {}


def jeton() -> str:
    """Le jeton le plus recent, renouvele s'il a plus de 7 jours."""
    etat = json.loads(JETON.read_text(encoding="utf-8")) if JETON.exists() else {}
    tok = etat.get("jeton") or os.environ.get("IG_TOKEN", "")
    if not tok:
        raise RuntimeError("IG_TOKEN absent du .env")
    maj = datetime.fromisoformat(etat["maj"]) if etat.get("maj") else None
    if maj is None:
        # premier usage : on note la date, le renouvellement exige un jeton d'au moins 24 h
        JETON.parent.mkdir(parents=True, exist_ok=True)
        JETON.write_text(json.dumps({"jeton": tok, "maj": datetime.now().isoformat()}),
                         encoding="utf-8")
    elif datetime.now() - maj > timedelta(days=7):
        r = requests.get("https://graph.instagram.com/refresh_access_token", timeout=30,
                         params={"grant_type": "ig_refresh_token", "access_token": tok})
        if r.ok and r.json().get("access_token"):
            tok = r.json()["access_token"]
            JETON.write_text(json.dumps({"jeton": tok, "maj": datetime.now().isoformat()}),
                             encoding="utf-8")
        else:
            print(f"Instagram : renouvellement du jeton refuse ({r.status_code}) {r.text[:120]}")
    return tok


def _appel(methode: str, chemin: str, **kw) -> dict:
    r = requests.request(methode, chemin if chemin.startswith("http") else API + chemin,
                         timeout=kw.pop("timeout", 60), **kw)
    d = r.json() if "json" in r.headers.get("content-type", "") else {"brut": r.text[:200]}
    if r.status_code >= 400 or "error" in d:
        msg = d.get("error", {}).get("message") if isinstance(d.get("error"), dict) else d
        raise RuntimeError(f"Instagram {r.status_code} : {msg}")
    return d


def compte(tok: str) -> dict:
    return _appel("GET", "/me", params={"fields": "user_id,username,account_type",
                                        "access_token": tok})


def legende(idx: dict, clip: dict) -> str:
    invite = C.invite(idx.get("titre", "")) or "l'invite"
    titre = clip.get("titre", "").replace("#Shorts", "").strip()
    return (f"{titre}\n\n"
            f"🎙️ Extrait de l'interview de {invite} par Thinkerview "
            f"(licence Creative Commons CC BY).\n"
            f"▶️ Plus d'extraits chaque jour sur notre chaîne YouTube : lien en bio.\n\n"
            f"{HASHTAGS}")[:2200]


def _attendre(tok: str, conteneur: str) -> None:
    """Instagram traite la video avant publication : jusqu'a quelques minutes."""
    for _ in range(40):
        st = _appel("GET", f"/{conteneur}", params={"fields": "status_code,status",
                                                     "access_token": tok})
        if st.get("status_code") == "FINISHED":
            return
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram a refuse la video : {st.get('status')}")
        time.sleep(15)
    raise RuntimeError("Instagram : traitement de la video trop long (10 min)")


def publier(vid: str, n: int, simuler: bool = False) -> str:
    idx = P.index(vid)
    clip = idx["clips"][n - 1]
    cle, jrn = f"{vid}_{n}", _journal()
    if cle in jrn:
        print(f"Instagram : deja publie le {jrn[cle]['date']}")
        return jrn[cle].get("url", "")
    auj = datetime.now().date().isoformat()
    if sum(1 for v in jrn.values() if v.get("date", "").startswith(auj)) >= P.par_jour():
        print(f"Instagram : deja {P.par_jour()} publication(s) aujourd'hui")
        return ""
    video = P.fichier(idx, clip)
    texte = legende(idx, clip)
    print(f"Instagram : {video.name}\n{texte}")
    if simuler:
        print("--simuler : rien n'a ete envoye a Instagram.")
        return ""

    tok = jeton()
    ig = compte(tok)["user_id"]
    # envoi direct du fichier (« resumable ») : pas besoin d'heberger la video
    c = _appel("POST", f"/{ig}/media", data={
        "media_type": "REELS", "upload_type": "resumable", "caption": texte,
        "share_to_feed": "true", "access_token": tok})
    with open(video, "rb") as f:
        r = requests.post(c["uri"], data=f, timeout=600, headers={
            "Authorization": f"OAuth {tok}", "offset": "0",
            "file_size": str(video.stat().st_size)})
    if r.status_code >= 400:
        raise RuntimeError(f"Instagram : envoi du fichier refuse ({r.status_code}) {r.text[:160]}")
    _attendre(tok, c["id"])
    pub = _appel("POST", f"/{ig}/media_publish",
                 data={"creation_id": c["id"], "access_token": tok})
    lien = _appel("GET", f"/{pub['id']}", params={"fields": "permalink",
                                                   "access_token": tok}).get("permalink", "")
    jrn[cle] = {"date": datetime.now().isoformat(timespec="minutes"), "id": pub["id"],
                "url": lien, "titre": clip.get("titre", "")}
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    JOURNAL.write_text(json.dumps(jrn, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Instagram : {lien or pub['id']}")
    return lien


def main() -> int:
    C.charger_env()
    if "--verifier" in sys.argv:
        c = compte(jeton())
        print(f"Jeton valide : @{c.get('username')} ({c.get('account_type')}), id {c.get('user_id')}")
        return 0
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args or "--clip" not in sys.argv:
        print(__doc__)
        return 2
    if not actif():
        sys.exit("IG_TOKEN manque dans le .env")
    publier(args[0], int(sys.argv[sys.argv.index("--clip") + 1]), "--simuler" in sys.argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
