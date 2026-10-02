"""Lit les VRAIES donnees de retention de la chaine (YouTube Analytics API).

Jusqu'ici le projet publiait sans jamais savoir a quelle seconde les gens
decrochent : le jeton OAuth ne portait que la permission d'upload. C'est la
metrique reine des Shorts, et elle est desormais lisible.

    python analytics.py                 tableau de toutes les videos (APV, vues, likes)
    python analytics.py --hook          retention moyenne seconde par seconde (LE diagnostic)
    python analytics.py "fort knox"     courbe detaillee d'une video (recherche par titre)
    python analytics.py --jours 90      fenetre d'analyse (defaut : depuis le debut)

Necessite la permission yt-analytics.readonly :  python upload.py --auth
"""
from __future__ import annotations

import datetime as dt
import sys
import unicodedata

from googleapiclient.discovery import build

from upload import load_creds

# La console Windows est en cp1252 : sans ca, un titre accentue fait planter
# l'affichage en plein tableau (UnicodeEncodeError).
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Les donnees Analytics ne sont consolidees qu'au bout de ~48h : une video
# publiee hier affichera des chiffres partiels, voire vides.
DELAI_CONSOLIDATION_H = 48


def _norm(s: str) -> str:
    """minuscules sans accents, pour la recherche par titre"""
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def _services():
    creds = load_creds()
    return (build("youtubeAnalytics", "v2", credentials=creds),
            build("youtube", "v3", credentials=creds))


def lister_videos(yt) -> list[dict]:
    """Toutes les videos de la chaine, de la plus ancienne a la plus recente."""
    ch = yt.channels().list(part="contentDetails", mine=True).execute()["items"][0]
    playlist = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    ids, page = [], None
    while True:
        r = yt.playlistItems().list(part="contentDetails", playlistId=playlist,
                                    maxResults=50, pageToken=page).execute()
        ids += [i["contentDetails"]["videoId"] for i in r["items"]]
        page = r.get("nextPageToken")
        if not page:
            break
    out = []
    for i in range(0, len(ids), 50):
        r = yt.videos().list(part="snippet,contentDetails",
                             id=",".join(ids[i:i + 50])).execute()
        for v in r["items"]:
            d = v["contentDetails"]["duration"]          # ex. PT23S
            sec = 0
            if "M" in d:
                sec += 60 * int(d.split("PT")[1].split("M")[0])
            if "S" in d:
                sec += int(d.split("M")[-1].replace("PT", "").rstrip("S") or 0)
            out.append({"id": v["id"], "titre": v["snippet"]["title"],
                        "date": v["snippet"]["publishedAt"][:10], "duree": sec})
    out.sort(key=lambda x: x["date"])
    return out


def _fenetre(jours: int | None) -> tuple[str, str]:
    fin = dt.date.today()
    debut = fin - dt.timedelta(days=jours) if jours else dt.date(2026, 7, 1)
    return debut.isoformat(), fin.isoformat()


def stats_videos(ya, videos: list[dict], jours=None) -> list[dict]:
    """Metriques par video. averageViewPercentage = le pourcentage de la video
    effectivement regarde : c'est LE chiffre qui decide de la diffusion."""
    debut, fin = _fenetre(jours)
    r = ya.reports().query(
        ids="channel==MINE", startDate=debut, endDate=fin,
        metrics="views,averageViewPercentage,averageViewDuration,likes,shares,"
                "subscribersGained",
        dimensions="video", sort="-views", maxResults=200).execute()
    par_id = {}
    for row in r.get("rows", []):
        par_id[row[0]] = dict(vues=row[1], apv=row[2], duree_vue=row[3],
                              likes=row[4], partages=row[5], abos=row[6])
    for v in videos:
        v.update(par_id.get(v["id"], {}))
    return [v for v in videos if "vues" in v]


def courbe_retention(ya, video_id: str, jours=None) -> list[tuple[float, float]]:
    """Retention relative : pour chaque point de la video (0 a 1), la part de
    spectateurs encore presents. Renvoie [(position_relative, part), ...]."""
    debut, fin = _fenetre(jours)
    try:
        r = ya.reports().query(
            ids="channel==MINE", startDate=debut, endDate=fin,
            metrics="audienceWatchRatio", dimensions="elapsedVideoTimeRatio",
            filters=f"video=={video_id}", sort="elapsedVideoTimeRatio").execute()
    except Exception as e:
        print(f"  (courbe indisponible : {str(e)[:90]})")
        return []
    return [(row[0], row[1]) for row in r.get("rows", [])]


def _barre(v: float, largeur=42, plein="█", vide="·") -> str:
    n = max(0, min(largeur, round(v * largeur)))
    return plein * n + vide * (largeur - n)


def cmd_tableau(ya, yt, jours=None):
    videos = stats_videos(ya, lister_videos(yt), jours)
    if not videos:
        print("Aucune donnee. Les stats mettent ~48h a se consolider.")
        return
    videos.sort(key=lambda v: v.get("apv", 0), reverse=True)
    print(f"\n{'date':11}{'vues':>7}{'APV%':>7}{'vue(s)':>8}{'lik%':>6}{'abo':>5}  titre")
    print("-" * 96)
    for v in videos:
        lk = 100 * v["likes"] / v["vues"] if v["vues"] else 0
        print(f"{v['date']} {v['vues']:6d} {v.get('apv', 0):6.1f} "
              f"{v.get('duree_vue', 0):7d} {lk:5.1f} {v.get('abos', 0):4d}  {v['titre'][:44]}")
    apv = [v["apv"] for v in videos if v.get("apv")]
    if apv:
        apv.sort()
        med = apv[len(apv) // 2]
        print("-" * 96)
        print(f"APV median : {med:.1f}%   (>100% = la boucle fonctionne, "
              f"les gens revoient le debut)")
        print(f"meilleur   : {apv[-1]:.1f}%      pire : {apv[0]:.1f}%")


def cmd_hook(ya, yt, jours=None):
    """Retention moyenne en debut de video, toutes videos confondues.
    C'est le diagnostic central : si ca s'effondre avant 15% de la video,
    le probleme est le hook, pas le corps."""
    videos = stats_videos(ya, lister_videos(yt), jours)
    videos = [v for v in videos if v.get("vues", 0) >= 100]
    if not videos:
        print("Pas assez de donnees.")
        return
    print(f"\nRetention moyenne sur {len(videos)} videos (>=100 vues)\n")
    cumul: dict[float, list[float]] = {}
    for v in videos:
        for pos, part in courbe_retention(ya, v["id"], jours):
            cumul.setdefault(round(pos, 2), []).append(part)
    if not cumul:
        print("Aucune courbe disponible (permission ou delai de consolidation).")
        return
    duree_moy = sum(v["duree"] for v in videos) / len(videos)
    print(f"{'position':>9} {'~sec':>5}  {'retention':<44} part")
    for pos in sorted(cumul):
        if pos > 0.32 and round(pos * 100) % 10:      # detail sur le debut seulement
            continue
        vals = cumul[pos]
        moy = sum(vals) / len(vals)
        print(f"{pos * 100:8.0f}% {pos * duree_moy:5.1f}  {_barre(min(moy, 1.0))} {moy * 100:5.1f}%")
    debut = [sum(v) / len(v) for p, v in sorted(cumul.items()) if p <= 0.15]
    if debut:
        perte = (1 - debut[-1]) * 100
        print(f"\n>>> {perte:.0f}% des spectateurs sont partis dans les "
              f"{0.15 * duree_moy:.1f} premieres secondes.")
        print("    Objectif Shorts : moins de 20%. Au-dela, c'est le hook qu'il faut reprendre.")


def cmd_video(ya, yt, requete: str, jours=None):
    videos = lister_videos(yt)
    q = _norm(requete)
    trouve = [v for v in videos if q in _norm(v["titre"])] or \
             [v for v in videos if v["id"] == requete]
    if not trouve:
        print(f"Aucune video ne correspond a « {requete} ».")
        return
    v = trouve[0]
    stats = stats_videos(ya, [v], jours)
    s = stats[0] if stats else {}
    print(f"\n{v['titre']}\n{v['date']}  |  {v['duree']}s  |  "
          f"{s.get('vues', 0)} vues  |  APV {s.get('apv', 0):.1f}%  |  "
          f"{s.get('likes', 0)} likes")
    courbe = courbe_retention(ya, v["id"], jours)
    if not courbe:
        return
    print(f"\n{'sec':>5} {'retention':<44} part")
    prec = None
    for pos, part in courbe:
        sec = pos * v["duree"]
        chute = ""
        if prec is not None and prec - part > 0.05:
            chute = f"  <<< chute de {100 * (prec - part):.0f} pts"
        print(f"{sec:5.1f} {_barre(min(part, 1.0))} {part * 100:5.1f}%{chute}")
        prec = part


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    jours = None
    if "--jours" in sys.argv:
        jours = int(sys.argv[sys.argv.index("--jours") + 1])
    ya, yt = _services()
    if "--hook" in sys.argv:
        cmd_hook(ya, yt, jours)
    elif args:
        cmd_video(ya, yt, " ".join(args), jours)
    else:
        cmd_tableau(ya, yt, jours)


if __name__ == "__main__":
    main()
