"""Republie en Reel sur la Page Facebook un clip deja parti sur YouTube.

    python youtube/facebook.py <id> --clip 3             publie le clip 3
    python youtube/facebook.py <id> --clip 3 --simuler   n'envoie rien, montre tout
    python youtube/facebook.py --verifier                 teste le jeton et ses droits

Le partage automatique Instagram -> Facebook ne suit pas les Reels publies par
l'API (constate le 11/10) : le Pi publie donc lui-meme sur la Page, juste apres
Instagram, avec la meme version a etiquette.

Il faut dans le .env : FB_PAGE_ID et FB_PAGE_TOKEN, avec l'autorisation
pages_manage_posts en plus de la lecture des statistiques. La video n'est pas
envoyee depuis le Pi : Facebook la lit par son lien public sur GitHub.

Journal : media/facebook.json (empeche de publier deux fois le meme clip).
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "clipper"))
sys.path.insert(0, str(RACINE / "youtube"))

import clipper as C          # noqa: E402
import instagram as I        # noqa: E402  (legende et lien public, les memes)
import publier as P          # noqa: E402

G = "https://graph.facebook.com/v23.0"
JOURNAL = C.MEDIA / "facebook.json"


def actif() -> bool:
    return bool(os.environ.get("FB_PAGE_TOKEN") and os.environ.get("FB_PAGE_ID"))


def _journal() -> dict:
    return json.loads(JOURNAL.read_text(encoding="utf-8")) if JOURNAL.exists() else {}


def page() -> tuple[str, str]:
    """(identifiant, jeton de Page). Un jeton utilisateur est echange contre celui
    de la Page, comme dans stats.py."""
    pid, tok = os.environ["FB_PAGE_ID"], os.environ["FB_PAGE_TOKEN"]
    r = requests.get(f"{G}/{pid}", timeout=30,
                     params={"fields": "access_token,name", "access_token": tok}).json()
    if r.get("access_token"):
        tok = r["access_token"]
    elif "error" in r:
        raise RuntimeError(f"Page Facebook inaccessible : {r['error'].get('message')}")
    return pid, tok


def _appel(methode: str, url: str, **kw) -> dict:
    r = requests.request(methode, url, timeout=kw.pop("timeout", 60), **kw)
    try:
        d = r.json()
    except ValueError:
        d = {"brut": r.text[:200]}
    if r.status_code >= 400 or "error" in d:
        msg = d.get("error", {}).get("message") if isinstance(d.get("error"), dict) else d
        raise RuntimeError(f"Facebook {r.status_code} : {msg}")
    return d


def publier(vid: str, n: int, simuler: bool = False) -> str:
    idx = P.index(vid)
    clip = idx["clips"][n - 1]
    cle, jrn = f"{vid}_{n}", _journal()
    if cle in jrn:
        print(f"Facebook : deja publie le {jrn[cle]['date']}")
        return jrn[cle].get("url", "")
    auj = datetime.now().date().isoformat()
    if sum(1 for v in jrn.values() if v.get("date", "").startswith(auj)) >= P.par_jour():
        print(f"Facebook : deja {P.par_jour()} publication(s) aujourd'hui")
        return ""
    nom = clip.get("fichier_insta") or clip["fichier"]
    texte = I.legende(idx, clip)
    print(f"Facebook : {nom}")
    if simuler:
        print("--simuler : rien n'a ete envoye a Facebook.")
        return ""

    pid, tok = page()
    # 1) ouverture d'un envoi  2) Facebook va chercher la video a son lien GitHub
    # 3) publication du Reel avec sa legende
    d = _appel("POST", f"{G}/{pid}/video_reels",
               data={"upload_phase": "start", "access_token": tok})
    _appel("POST", d["upload_url"], timeout=300, headers={
        "Authorization": f"OAuth {tok}", "file_url": I.lien_public(vid, nom)})
    _appel("POST", f"{G}/{pid}/video_reels", data={
        "upload_phase": "finish", "video_id": d["video_id"], "video_state": "PUBLISHED",
        "description": texte, "access_token": tok})
    # le traitement prend une minute ou deux ; on ne bloque pas la soiree au-dela
    lien = ""
    for _ in range(12):
        st = requests.get(f"{G}/{d['video_id']}", timeout=30, params={
            "fields": "status,permalink_url", "access_token": tok}).json()
        if st.get("status", {}).get("video_status") == "ready":
            lien = st.get("permalink_url", "")
            break
        if st.get("status", {}).get("video_status") == "error":
            raise RuntimeError(f"Facebook a refuse la video : {st.get('status')}")
        time.sleep(15)
    if lien and not lien.startswith("http"):
        lien = "https://www.facebook.com" + lien
    jrn[cle] = {"date": datetime.now().isoformat(timespec="minutes"),
                "id": d["video_id"], "url": lien, "titre": clip.get("titre", "")}
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    JOURNAL.write_text(json.dumps(jrn, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Facebook : {lien or d['video_id']}")
    return lien


def main() -> int:
    C.charger_env()
    if "--verifier" in sys.argv:
        pid, tok = page()
        perms = requests.get(f"{G}/me/permissions", timeout=30, params={
            "access_token": os.environ["FB_PAGE_TOKEN"]}).json().get("data", [])
        ok = {p["permission"] for p in perms if p.get("status") == "granted"}
        print(f"Page {pid} accessible.")
        if perms:
            print("pages_manage_posts :", "OK" if "pages_manage_posts" in ok
                  else "MANQUANTE (le Pi ne pourra pas publier)")
        return 0
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args or "--clip" not in sys.argv:
        print(__doc__)
        return 2
    if not actif():
        sys.exit("FB_PAGE_ID et FB_PAGE_TOKEN manquent dans le .env")
    publier(args[0], int(sys.argv[sys.argv.index("--clip") + 1]), "--simuler" in sys.argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
