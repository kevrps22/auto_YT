"""Releve les vues de chaque Short, YouTube + Dailymotion, pour l'appli mobile.

    python youtube/stats.py            releve et publie stats.json (branche « stats »)
    python youtube/stats.py --local    releve et ecrit media/stats.json sans rien envoyer

L'appli lit https://raw.githubusercontent.com/<depot>/stats/stats.json : elle n'a
ainsi besoin d'aucune cle. Lance toutes les heures par clipper-stats.timer.

Les deux versions d'un clip sont rapprochees par le titre (Dailymotion recoit le
titre YouTube sans « #Shorts »). L'historique garde un point par jour : le total
de la chaine a la derniere releve de la journee.
"""
from __future__ import annotations

import base64
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
import github_io as G        # noqa: E402

YT = "https://www.googleapis.com/youtube/v3"
DM = "https://api.dailymotion.com"
BRANCHE, FICHIER = "stats", "stats.json"


def _nu(t: str) -> str:
    from soir import _nu as nu
    return nu(t.replace("#Shorts", "").replace("#shorts", ""))


def youtube() -> tuple[dict, list[dict]]:
    cle = os.environ["YT_API_KEY"]
    ch = requests.get(f"{YT}/channels", timeout=30, params={
        "part": "contentDetails,statistics,snippet", "key": cle,
        "forHandle": os.environ.get("YT_CHANNEL", "@Attends_Quoi")}).json()["items"][0]
    ids, page = [], None
    for _ in range(6):
        r = requests.get(f"{YT}/playlistItems", timeout=30, params={
            "part": "contentDetails", "maxResults": 50, "key": cle,
            "playlistId": ch["contentDetails"]["relatedPlaylists"]["uploads"],
            **({"pageToken": page} if page else {})}).json()
        ids += [i["contentDetails"]["videoId"] for i in r.get("items", [])]
        page = r.get("nextPageToken")
        if not page:
            break
    videos = []
    for i in range(0, len(ids), 50):
        for v in requests.get(f"{YT}/videos", timeout=30, params={
                "part": "snippet,statistics", "id": ",".join(ids[i:i + 50]),
                "key": cle}).json().get("items", []):
            s = v["statistics"]
            videos.append({"titre": v["snippet"]["title"], "date": v["snippet"]["publishedAt"],
                           "url": f"https://youtube.com/shorts/{v['id']}",
                           "vues": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
                           "commentaires": int(s.get("commentCount", 0))})
    chaine = {"nom": ch["snippet"]["title"],
              "abonnes": int(ch["statistics"].get("subscriberCount", 0))}
    return chaine, videos


def dailymotion() -> tuple[dict, list[dict]]:
    nom = os.environ.get("DM_CHANNEL")
    if not nom:
        return {}, []
    u = requests.get(f"{DM}/user/{nom}", timeout=30,
                     params={"fields": "followers_total"}).json()
    videos, page = [], 1
    while page and page <= 10:
        r = requests.get(f"{DM}/user/{nom}/videos", timeout=30, params={
            "fields": "id,title,created_time,views_total,likes_total",
            "limit": 100, "page": page}).json()
        for v in r.get("list", []):
            videos.append({"titre": v["title"], "url": f"https://www.dailymotion.com/video/{v['id']}",
                           "date": datetime.fromtimestamp(v["created_time"]).isoformat(),
                           "vues": int(v.get("views_total") or 0),
                           "likes": int(v.get("likes_total") or 0)})
        page = page + 1 if r.get("has_more") else None
    return {"abonnes": int(u.get("followers_total") or 0)}, videos


def relever() -> dict:
    yt_ch, yt = youtube()
    try:
        dm_ch, dm = dailymotion()
    except Exception as e:                       # noqa: BLE001
        print(f"Dailymotion injoignable ({type(e).__name__}) : releve YouTube seule")
        dm_ch, dm = {}, []
    clips: dict[str, dict] = {}
    for plateforme, liste in (("youtube", yt), ("dailymotion", dm)):
        for v in liste:
            c = clips.setdefault(_nu(v["titre"]), {
                "titre": v["titre"].replace("#Shorts", "").strip(), "date": v["date"]})
            c[plateforme] = {k: v[k] for k in v if k not in ("titre", "date")}
            c["date"] = min(c["date"], v["date"])
    for c in clips.values():
        c["vues"] = sum(c.get(p, {}).get("vues", 0) for p in ("youtube", "dailymotion"))
    return {"maj": datetime.now().isoformat(timespec="minutes"),
            "chaine": {"nom": yt_ch["nom"], "youtube": {"abonnes": yt_ch["abonnes"]},
                       "dailymotion": dm_ch},
            "totaux": {"youtube": sum(v["vues"] for v in yt),
                       "dailymotion": sum(v["vues"] for v in dm)},
            "clips": sorted(clips.values(), key=lambda c: c["date"], reverse=True)}


def publier(stats: dict) -> None:
    depot = G.DEPOT
    if G._api("GET", f"/repos/{depot}/branches/{BRANCHE}").status_code == 404:
        sha = G._api("GET", f"/repos/{depot}/git/ref/heads/main").json()["object"]["sha"]
        G._api("POST", f"/repos/{depot}/git/refs",
               json={"ref": f"refs/heads/{BRANCHE}", "sha": sha}).raise_for_status()
    r = G._api("GET", f"/repos/{depot}/contents/{FICHIER}", params={"ref": BRANCHE})
    ancien = r.json() if r.status_code == 200 else None
    historique = []
    if ancien:
        historique = json.loads(base64.b64decode(ancien["content"])).get("historique", [])
    jour = stats["maj"][:10]
    historique = [h for h in historique if h["date"] != jour] + [{
        "date": jour, **stats["totaux"], "abonnes": stats["chaine"]["youtube"]["abonnes"]}]
    stats["historique"] = historique[-365:]
    corps = {"message": f"stats {stats['maj']}", "branch": BRANCHE,
             "content": base64.b64encode(json.dumps(stats, ensure_ascii=False).encode()).decode()}
    if ancien:
        corps["sha"] = ancien["sha"]
    G._api("PUT", f"/repos/{depot}/contents/{FICHIER}", json=corps).raise_for_status()


def main() -> int:
    C.charger_env()
    stats = relever()
    print(f"{len(stats['clips'])} clips — YouTube {stats['totaux']['youtube']:,} vues, "
          f"Dailymotion {stats['totaux']['dailymotion']:,} vues".replace(",", " "))
    if "--local" in sys.argv:
        (C.MEDIA / FICHIER).write_text(json.dumps(stats, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
        return 0
    publier(stats)
    print(f"publie sur la branche « {BRANCHE} »")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
