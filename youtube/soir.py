"""Le pilote de l'Orange Pi : approvisionne le stock, publie un clip par soir.

    python youtube/soir.py --approvisionner     (matin) prend une source, la fait rendre
    python youtube/soir.py --publier            (19 h)  habille et publie le meilleur clip
    python youtube/soir.py --etat               ou en est le stock
    python youtube/soir.py --recoler           marque comme publies les clips deja en ligne
    python youtube/soir.py --menage            supprime sources et clips devenus inutiles
    ... --simuler                               fait tout sauf l'envoi a YouTube

Deux taches separees, et c'est volontaire : l'approvisionnement dure une heure
(telechargement + rendu GitHub), la publication doit partir a l'heure dite.

Tout est rejouable. Une coupure de courant ne fait perdre que l'etape en cours :
transcription, choix des moments, plan visuel et clips deja rendus sont en cache,
et le journal des publications empeche de poster deux fois le meme clip.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "clipper"))
sys.path.insert(0, str(RACINE / "youtube"))

import clipper as C          # noqa: E402
import github_io as G        # noqa: E402
import publier as P          # noqa: E402

CHAINE_SOURCE = "@thinkerview"
# Duree exploitable d'un entretien : sous 20 min il n'y a pas 4 bons moments,
# au-dela de 3 h le rendu approche la limite de 6 h des serveurs GitHub.
DUREE_MIN, DUREE_MAX = 20 * 60, 3 * 3600
CLIPS_PAR_SOURCE = 4
JOURNAL = C.MEDIA / "publies.json"
VERROU = C.MEDIA / "soir.lock"
LOG = C.MEDIA / "soir.log"
# Les sources choisies a la main, une par ligne (identifiant ou lien), dans
# l'ordre ou les traiter. Vide ou absent : on retombe sur la plus vue.
SOURCES = RACINE / "sources.txt"


def trace(msg: str) -> None:
    ligne = f"{datetime.now():%Y-%m-%d %H:%M}  {msg}"
    print(ligne, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(ligne + "\n")


class Verrou:
    """Empeche deux executions de se chevaucher — un approvisionnement qui traine
    ne doit pas croiser la publication du soir. Un verrou de plus de 6 h est
    considere comme abandonne (coupure de courant en plein travail)."""

    def __enter__(self):
        if VERROU.exists() and time.time() - VERROU.stat().st_mtime < 6 * 3600:
            sys.exit(f"Une autre execution est en cours depuis "
                     f"{(time.time() - VERROU.stat().st_mtime) / 60:.0f} min.")
        VERROU.parent.mkdir(parents=True, exist_ok=True)
        VERROU.write_text(str(os.getpid()), encoding="utf-8")
        return self

    def __exit__(self, *_):
        VERROU.unlink(missing_ok=True)


# ------------------------------------------------------------------ etat du stock
def journal() -> dict:
    return json.loads(JOURNAL.read_text(encoding="utf-8")) if JOURNAL.exists() else {}


def ecrire_journal(jrn: dict) -> None:
    """media/ est hors depot : sur une machine fraiche, le dossier n'existe pas
    encore et l'ecriture du journal echouait avant meme la premiere publication."""
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    JOURNAL.write_text(json.dumps(jrn, ensure_ascii=False, indent=1), encoding="utf-8")


def stock() -> list[tuple[int, str, int, dict, dict]]:
    """Les clips rendus, jamais publies, triés du meilleur au moins bon.
    Chaque entree : (score, id source, numero, clip, index)."""
    jrn, dispo = journal(), []
    for f in sorted(C.OUT.glob("*.json")):
        idx = json.loads(f.read_text(encoding="utf-8"))
        if not C.publiable(idx):
            continue
        # Au-dela du 4e clip d'une meme source, les vues s'effondrent (chez Barrau :
        # 43k/3,7k/51k sur les trois premiers, 1,2k a 5,7k sur les quatre suivants).
        # La coupe s'applique ici et pas seulement au rendu : une source rendue en 8
        # clips n'en proposera que 4.
        for n, clip in enumerate(idx.get("clips", [])[:CLIPS_PAR_SOURCE], 1):
            if f"{idx['id']}_{n}" in jrn:
                continue
            if not (C.OUT / clip["fichier"]).exists():
                continue
            dispo.append((clip.get("score") or 0, idx["id"], n, clip, idx))
    return sorted(dispo, key=lambda x: -x[0])


def menage(simuler: bool = False) -> int:
    """Libere la place. Un entretien de deux heures pese 1,6 Go : sans menage, les
    15 Go de la carte SD sont pleins en trois semaines et le Pi s'arrete.

    La video source est gardee tant qu'un seul de ses clips reste a publier :
    l'habillage y reprend la voix et y relit les changements de plan. Les index
    JSON, eux, ne sont JAMAIS supprimes — ils pesent quelques kilo-octets, ils
    disent ce qui a deja ete publie, et ce sont eux qui empechent de retraiter une
    source deja exploitee."""
    jrn, libere = journal(), 0

    def jeter(p: Path) -> None:
        nonlocal libere
        if p.exists():
            libere += p.stat().st_size
            if not simuler:
                p.unlink()

    for f in sorted(C.OUT.glob("*.json")):
        idx = json.loads(f.read_text(encoding="utf-8"))
        clips = idx.get("clips", [])
        garde = min(len(clips), CLIPS_PAR_SOURCE)
        for n, clip in enumerate(clips, 1):
            # publie, ou au-dela de la coupe a 4 : dans les deux cas il ne servira plus
            if n <= garde and f"{idx['id']}_{n}" not in jrn:
                continue
            jeter(C.OUT / clip["fichier"])
            jeter(C.OUT_HABILLE / (Path(clip["fichier"]).stem + "_explique.mp4"))
        if garde and all(f"{idx['id']}_{n}" in jrn for n in range(1, garde + 1)):
            src = C.SRC / f"{idx['id']}.mp4"   # source epuisee : plus rien a en tirer
            jeter(src)
            for ext in (".words.json", ".scenes.txt", ".clips.json", ".info.json"):
                jeter(src.with_suffix(ext))
            for p in sorted(C.SRC.glob(f"{idx['id']}.plan*.json")):
                jeter(p)
    if libere:
        trace(f"menage : {libere / 1e9:.2f} Go {'a liberer' if simuler else 'liberes'}")
    return libere


def sources_faites() -> set[str]:
    return {f.stem for f in C.OUT.glob("*.json")}


# ------------------------------------------------------------------ approvisionnement
def candidates() -> list[dict]:
    """Les entretiens de la chaine source, en Creative Commons, jamais traites.

    On pioche dans tout le catalogue et pas seulement dans les nouveautes : a
    raison d'une source tous les trois jours, les seules nouvelles publications ne
    suffiraient pas. Les plus vues d'abord — c'est le seul indice de qualite
    disponible avant de les avoir ecoutees."""
    cle = os.environ["YT_API_KEY"]
    api = "https://www.googleapis.com/youtube/v3"
    ch = requests.get(f"{api}/channels", timeout=30, params={
        "part": "contentDetails", "forHandle": CHAINE_SOURCE, "key": cle}).json()["items"][0]
    playlist, ids, page = ch["contentDetails"]["relatedPlaylists"]["uploads"], [], None
    for _ in range(4):                      # 200 videos : largement de quoi tenir
        r = requests.get(f"{api}/playlistItems", timeout=30, params={
            "part": "contentDetails", "playlistId": playlist, "maxResults": 50,
            "key": cle, **({"pageToken": page} if page else {})}).json()
        ids += [i["contentDetails"]["videoId"] for i in r.get("items", [])]
        page = r.get("nextPageToken")
        if not page:
            break
    deja, out = sources_faites(), []
    for i in range(0, len(ids), 50):
        for v in requests.get(f"{api}/videos", timeout=30, params={
                "part": "snippet,status,contentDetails,statistics",
                "id": ",".join(ids[i:i + 50]), "key": cle}).json().get("items", []):
            if v["id"] in deja or v["status"]["license"] != "creativeCommon":
                continue
            if not DUREE_MIN <= _secondes(v["contentDetails"]["duration"]) <= DUREE_MAX:
                continue
            out.append({"id": v["id"], "titre": v["snippet"]["title"],
                        "vues": int(v["statistics"].get("viewCount", 0))})
    out.sort(key=lambda x: -x["vues"])
    # La plus vue ne fait pas la plus virale : Giraud (1,7 M de vues) a donne
    # des clips a 1 500, Ferrari des clips a 24 000. La liste choisie passe devant.
    rang = {v: i for i, v in enumerate(sources_choisies())}
    return sorted(out, key=lambda x: rang.get(x["id"], len(rang)))


def sources_choisies() -> list[str]:
    import re
    if not SOURCES.exists():
        return []
    ids = []
    for ligne in SOURCES.read_text(encoding="utf-8").splitlines():
        ligne = ligne.split("#")[0].strip()
        m = re.search(r"(?:v=|youtu\.be/)?([\w-]{11})$", ligne)
        if m:
            ids.append(m.group(1))
    return ids


def _secondes(iso: str) -> int:
    """PT1H55M12S -> secondes. Ne lire que les minutes annoncait 55 min pour 1 h 55."""
    import re
    n = {u: int(v) for v, u in re.findall(r"(\d+)([HMS])", iso)}
    return n.get("H", 0) * 3600 + n.get("M", 0) * 60 + n.get("S", 0)


def approvisionner(simuler: bool = False) -> None:
    menage(simuler)                            # avant de mesurer la place, pas apres
    reste = len(stock())
    trace(f"stock : {reste} clip(s) disponible(s)")
    if reste > P.par_jour():
        trace("rien a faire, le stock tient encore")
        return
    # Une source de deux heures pese jusqu'a 2 Go, et le rendu en ajoute autant en
    # fichiers temporaires. Mieux vaut ne rien commencer que remplir la carte et
    # planter en plein travail.
    libre = shutil.disk_usage(C.MEDIA if C.MEDIA.exists() else C.ROOT).free
    if libre < 5e9:
        trace(f"ESPACE INSUFFISANT : {libre / 1e9:.1f} Go libres, il en faut 5")
        return
    choix = candidates()
    if not choix:
        trace("AUCUNE source disponible : catalogue epuise ou API injoignable")
        return
    src = choix[0]
    trace(f"nouvelle source : {src['titre'][:60]} ({src['vues']} vues)")
    if simuler:
        trace("--simuler : la source n'est ni telechargee ni envoyee")
        return
    vid = G.envoyer(f"https://www.youtube.com/watch?v={src['id']}", CLIPS_PAR_SOURCE)
    run = G.suivre(vid)
    if run["conclusion"] != "success":
        trace(f"ECHEC du rendu GitHub : {run['conclusion']}")
        return
    index = G.recuperer(vid)
    trace(f"{len(index['clips'])} clips recuperes")
    if P.format_publie() == "brut":
        # En format brut, la source ne sert plus a rien une fois les clips rendus :
        # on publie les extraits tels quels. La garder jusqu'a la derniere
        # publication immobilisait 2,8 Go pendant deux jours sur une carte de 15.
        # (Les formats montes, eux, reprennent sa bande-son : ils la conservent.)
        source = C.SRC / f"{vid}.mp4"
        if source.exists():
            taille = source.stat().st_size
            source.unlink()
            trace(f"source supprimee apres recuperation : {taille / 1e9:.2f} Go liberes")


# ------------------------------------------------------------------ publication
def deja_en_ligne(titre: str) -> bool:
    """Le titre est-il deja sur la chaine ? Seul garde-fou contre le cas ou le
    courant saute APRES l'envoi et AVANT l'ecriture du journal : la video serait
    en ligne sans trace locale, et le lendemain on la republierait."""
    try:
        cle = os.environ["YT_API_KEY"]
        api = "https://www.googleapis.com/youtube/v3"
        ch = requests.get(f"{api}/channels", timeout=30, params={
            "part": "contentDetails", "forHandle": os.environ.get("YT_CHANNEL", "@Attends_Quoi"),
            "key": cle}).json()["items"][0]
        it = requests.get(f"{api}/playlistItems", timeout=30, params={
            "part": "snippet", "maxResults": 15, "key": cle,
            "playlistId": ch["contentDetails"]["relatedPlaylists"]["uploads"]}).json()["items"]
    except Exception as e:                  # sans reseau on ne bloque pas la soiree
        trace(f"verification de la chaine impossible ({type(e).__name__})")
        return False
    vu = {_nu(i["snippet"]["title"]) for i in it}
    return _nu(titre) in vu


def _nu(t: str) -> str:
    """Titre normalise : YouTube retouche la ponctuation, « physique : L'effet »
    devient « physique L'effet »."""
    import re
    import unicodedata
    t = unicodedata.normalize("NFD", t.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def recoler(simuler: bool = False) -> None:
    """Marque comme publies les clips deja en ligne sur la chaine.

    A lancer UNE FOIS avant de laisser le Pi travailler seul : les publications
    faites a la main ne sont pas dans le journal, et sans ce recollement le Pi
    reprendrait la serie depuis le debut."""
    cle = os.environ["YT_API_KEY"]
    api = "https://www.googleapis.com/youtube/v3"
    ch = requests.get(f"{api}/channels", timeout=30, params={
        "part": "contentDetails", "forHandle": os.environ.get("YT_CHANNEL", "@Attends_Quoi"),
        "key": cle}).json()["items"][0]
    playlist, en_ligne, page = ch["contentDetails"]["relatedPlaylists"]["uploads"], {}, None
    for _ in range(2):
        r = requests.get(f"{api}/playlistItems", timeout=30, params={
            "part": "snippet,contentDetails", "playlistId": playlist, "maxResults": 50,
            "key": cle, **({"pageToken": page} if page else {})}).json()
        for i in r.get("items", []):
            en_ligne[_nu(i["snippet"]["title"])] = (
                i["contentDetails"]["videoId"], i["snippet"]["publishedAt"][:10])
        page = r.get("nextPageToken")
        if not page:
            break
    jrn, ajoutes = journal(), 0
    for f in sorted(C.OUT.glob("*.json")):
        idx = json.loads(f.read_text(encoding="utf-8"))
        for n, clip in enumerate(idx.get("clips", []), 1):
            cle_clip = f"{idx['id']}_{n}"
            trouve = en_ligne.get(_nu(clip.get("titre", "")))
            if not trouve or cle_clip in jrn:
                continue
            vid_yt, quand = trouve
            jrn[cle_clip] = {"date": quand, "url": f"https://youtu.be/{vid_yt}",
                             "titre": clip.get("titre", ""), "fichier": clip["fichier"],
                             "note": "publie a la main, retrouve sur la chaine"}
            ajoutes += 1
            trace(f"deja en ligne : [{n}] {clip.get('titre', '')[:46]}  ({quand})")
    if simuler:
        trace(f"--simuler : {ajoutes} clip(s) auraient ete marques")
        return
    ecrire_journal(jrn)
    trace(f"{ajoutes} clip(s) marques comme publies, {len(stock())} restent en stock")


def publier(simuler: bool = False) -> None:
    jrn = journal()
    auj = str(date.today())
    raison = P.trop_tot(jrn)
    if raison:
        # en simulation rien n'est envoye : s'arreter ici empecherait de tester
        # la chaine le jour meme d'une publication faite a la main
        trace(raison + (" (ignore en simulation)" if simuler else ", on s'arrete"))
        if not simuler:
            return
    dispo = stock()
    if not dispo:
        trace("STOCK VIDE : rien a publier ce soir")
        return
    score, vid, n, clip, idx = dispo[0]
    trace(f"choisi : [{n}] score {score} — {clip.get('titre', '')[:50]} ({vid})")

    habille = C.OUT_HABILLE / (Path(clip["fichier"]).stem + "_explique.mp4")
    if P.format_publie() == "brut":
        trace("format brut : l'extrait part tel quel, sans habillage")
    elif not habille.exists():
        trace("habillage en cours (compter 20 a 30 min sur le Pi)")
        if simuler:
            trace("--simuler : habillage saute")
        else:
            # Un habillage impossible ne doit pas faire sauter la soiree : il manque
            # parfois la source ou sa transcription (clip rendu ailleurs, fichiers
            # effaces). On publie alors le clip brut, comme avant l'habillage.
            try:
                import habiller as H
                H.habiller(vid, n)
                trace(f"habille : {habille.name}")
            except Exception as e:                  # noqa: BLE001
                trace(f"HABILLAGE IMPOSSIBLE ({type(e).__name__}: {str(e)[:120]})")
                trace("-> publication du clip brut")

    if deja_en_ligne(clip.get("titre", "")):
        trace("ce titre est DEJA en ligne : publication annulee, journal mis a jour")
        if not simuler:
            jrn[f"{vid}_{n}"] = {"date": auj, "url": "", "titre": clip.get("titre", ""),
                                 "fichier": habille.name, "note": "retrouve sur la chaine"}
            ecrire_journal(jrn)
        return
    url = P.publier(vid, n, simuler=simuler)
    trace("publication terminee")
    # Dailymotion en bonus, AVANT le menage qui efface le clip. Son echec ne
    # doit jamais faire croire que la soiree a rate : YouTube est deja parti.
    import dailymotion as D
    if D.actif() and (url or simuler):
        try:
            D.publier(vid, n, simuler=simuler)
            trace("dailymotion : publie")
        except Exception as e:                      # noqa: BLE001
            trace(f"DAILYMOTION IMPOSSIBLE ({type(e).__name__}: {str(e)[:120]})")
    menage(simuler)          # le clip publie et sa version habillee ne servent plus


def etat() -> None:
    jrn, dispo = journal(), stock()
    print(f"{len(jrn)} clip(s) publie(s), {len(dispo)} en stock\n")
    for score, vid, n, clip, _ in dispo:
        h = C.OUT_HABILLE / (Path(clip["fichier"]).stem + "_explique.mp4")
        print(f"  score {score:>3}  [{'habille' if h.exists() else 'brut   '}]  "
              f"{vid} #{n}  {clip.get('titre', '')[:48]}")
    if LOG.exists():
        print("\nderniers evenements :")
        for l in LOG.read_text(encoding="utf-8").splitlines()[-6:]:
            print("  " + l)


def main() -> int:
    C.charger_env()
    simuler = "--simuler" in sys.argv
    if "--etat" in sys.argv:
        etat()
    elif "--menage" in sys.argv:
        with Verrou():
            menage(simuler)
    elif "--recoler" in sys.argv:
        with Verrou():
            recoler(simuler)
    elif "--approvisionner" in sys.argv:
        with Verrou():
            approvisionner(simuler)
    elif "--publier" in sys.argv:
        with Verrou():
            publier(simuler)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
