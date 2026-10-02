"""Publie UN clip de l'index Clipper sur YouTube.

    python youtube/publier.py <id> --liste          montre les clips et leur etat
    python youtube/publier.py <id> --clip 7         publie le clip 7 (version habillee)
    python youtube/publier.py <id> --clip 7 --brut  publie le clip sans habillage
    python youtube/publier.py <id> --clip 7 --simuler  n'envoie rien, montre tout
    python youtube/publier.py <id> --clip 7 --prive    en non repertorie, pour verifier

C'est le script que l'Orange Pi lance le soir. Il refuse de lui-meme :
- un clip dont la source n'est pas en Creative Commons (`publiable` faux) ;
- un clip deja publie ;
- une deuxieme publication le meme jour (deux Shorts/jour sur une chaine jeune
  est un signal de spam, et le lot programme d'aout a coute un facteur 4).

Le journal des publications est dans media/publies.json.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import date
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "clipper"))
sys.path.insert(0, str(RACINE / "youtube"))

import clipper as C          # noqa: E402  (dossiers du projet, description(), publiable())
from upload import load_creds  # noqa: E402

JOURNAL = C.MEDIA / "publies.json"


def _journal() -> dict:
    return json.loads(JOURNAL.read_text(encoding="utf-8")) if JOURNAL.exists() else {}


def index(vid: str) -> dict:
    f = C.OUT / f"{vid}.json"
    if not f.exists():
        sys.exit(f"Index absent : {f}\n-> recupere le rendu : python clipper/github_io.py recuperer {vid}")
    return json.loads(f.read_text(encoding="utf-8"))


def fichier(idx: dict, clip: dict, brut: bool = False) -> Path:
    """La version habillee si elle existe : c'est elle qui porte l'apport original
    exige pour la monetisation. Le clip brut n'est qu'un repli de test."""
    if not brut:
        habille = C.OUT_HABILLE / (Path(clip["fichier"]).stem + "_explique.mp4")
        if habille.exists():
            return habille
    return C.OUT / clip["fichier"]


def texte(idx: dict, clip: dict) -> tuple[str, str]:
    """Titre et description. La description est TOUJOURS recalculee : celle rangee
    dans l'index date du rendu et garde l'ancien format quand le modele change."""
    titre = clip.get("titre", "").strip()
    if "#shorts" not in titre.lower() and len(titre) < 92:
        titre = f"{titre} #Shorts"
    return titre[:100], C.description(clip, idx)[:4900]


def lister(vid: str) -> None:
    idx, jrn = index(vid), _journal()
    print(f"{idx.get('titre', vid)} — {idx.get('chaine', '')} — {idx.get('licence', '?')}\n")
    for i, c in enumerate(idx["clips"], 1):
        f = fichier(idx, c)
        etat = "PUBLIE " + jrn[f"{vid}_{i}"]["date"] if f"{vid}_{i}" in jrn else (
            "habille" if f.parent == C.OUT_HABILLE else "brut")
        if not c.get("publiable", "creative" in (idx.get("licence") or "").lower()):
            etat = "NON PUBLIABLE (licence)"
        print(f"  [{i}] score {str(c.get('score', '?')):>3}  {c.get('duree', 0):5.1f}s  "
              f"{etat:<24} {c.get('titre', '')[:48]}")


def publier(vid: str, n: int, brut: bool = False, prive: bool = False,
            force: bool = False, simuler: bool = False) -> str:
    idx = index(vid)
    if not 1 <= n <= len(idx["clips"]):
        sys.exit(f"Clip {n} inexistant (1 a {len(idx['clips'])}).")
    clip = idx["clips"][n - 1]
    cle, jrn = f"{vid}_{n}", _journal()

    publiable = clip.get("publiable", "creative" in (idx.get("licence") or "").lower())
    if not publiable and not force:
        sys.exit("Source hors Creative Commons : publication refusee.\n"
                 "Il faut l'accord ecrit du createur (--force pour passer outre).")
    if cle in jrn and not force:
        sys.exit(f"Deja publie le {jrn[cle]['date']} : {jrn[cle]['url']}")
    auj = str(date.today())
    if any(v["date"] == auj for v in jrn.values()) and not force:
        sys.exit(f"Une video a deja ete publiee aujourd'hui ({auj}). Une par jour.")

    video = fichier(idx, clip, brut)
    if not video.exists():
        sys.exit(f"Fichier absent : {video}")
    titre, desc = texte(idx, clip)
    print(f"Fichier : {video.name} ({video.stat().st_size / 1e6:.0f} Mo)")
    print(f"Titre   : {titre}")
    print(f"Etat    : {'non repertorie' if prive else 'public'}\n")
    if simuler:
        print(desc)
        print("\n--simuler : rien n'a ete envoye.")
        return ""

    yt = build("youtube", "v3", credentials=load_creds())
    corps = {"snippet": {"title": titre, "description": desc, "categoryId": "27",
                         "tags": ["shorts", "science", "explication"]},
             "status": {"privacyStatus": "unlisted" if prive else "public",
                        "selfDeclaredMadeForKids": False}}
    media = MediaFileUpload(str(video), chunksize=-1, resumable=True)
    # l'envoi echoue parfois sur une coupure reseau : trois essais valent mieux
    # qu'une soiree sans publication
    for essai in range(1, 4):
        try:
            rep = yt.videos().insert(part="snippet,status", body=corps, media_body=media).execute()
            break
        except Exception as e:                      # noqa: BLE001 (l'API leve large)
            if essai == 3:
                raise
            print(f"  echec ({str(e)[:90]}) -> essai {essai + 1}/3")
            time.sleep(20 * essai)
    url = f"https://youtu.be/{rep['id']}"
    jrn[cle] = {"date": auj, "url": url, "titre": titre, "fichier": video.name}
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    JOURNAL.write_text(json.dumps(jrn, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Publie : {url}")
    return url


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    vid = args[0]
    if "--liste" in sys.argv:
        lister(vid)
        return 0
    if "--clip" not in sys.argv:
        print(__doc__)
        return 2
    n = int(sys.argv[sys.argv.index("--clip") + 1])
    publier(vid, n, brut="--brut" in sys.argv, prive="--prive" in sys.argv,
            force="--force" in sys.argv, simuler="--simuler" in sys.argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
