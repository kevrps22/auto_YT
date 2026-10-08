"""Releve les chiffres de la chaine pour l'appli mobile « Stats Shorts ».

    python youtube/stats.py            releve et publie stats.json (branche « stats »)
    python youtube/stats.py --local    releve et ecrit media/stats.json sans rien envoyer

L'appli lit https://raw.githubusercontent.com/<depot>/stats/stats.json : elle n'a
ainsi besoin d'aucune cle. Lance toutes les heures par clipper-stats.timer.

Ce qui est releve :
- vues, likes, commentaires de chaque Short, YouTube + Dailymotion, rapproches par
  le titre (Dailymotion recoit le titre YouTube sans « #Shorts ») ;
- un instantane par heure (48 h) : vues gagnees aujourd'hui, sur 24 h, par heure ;
- un point par jour et par clip (60 jours) : la courbe de chaque Short ;
- les vues quotidiennes de YouTube Analytics (exactes, mais avec 2-3 jours de retard) ;
- les derniers commentaires, et si la chaine y a deja repondu ;
- la source (l'invite) de chaque clip et son creneau de publication ;
- l'etat du Pi : stock, prochaines publications, sources restantes, disque, journal.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE / "clipper"))
sys.path.insert(0, str(RACINE / "youtube"))

import clipper as C          # noqa: E402
import github_io as G        # noqa: E402
import publier as P          # noqa: E402
import soir as S             # noqa: E402

YT = "https://www.googleapis.com/youtube/v3"
DM = "https://api.dailymotion.com"
BRANCHE, FICHIER = "stats", "stats.json"
HEURES_GARDEES, JOURS_PAR_CLIP = 48, 60
CRENEAUX = (12, 19)


def _nu(t: str) -> str:
    return S._nu(t.replace("#Shorts", "").replace("#shorts", ""))


def _local(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()


def _invite_desc(desc: str) -> str:
    """L'invite d'apres la description YouTube, pour les clips d'avant le Pi."""
    import re
    m = re.search(r"(?:interview|entretien) (?:de|d'|avec) ([^\n.,]+?) (?:par|sur|chez)", desc)
    return m.group(1).strip() if m else ""


# ------------------------------------------------------------------ plateformes
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
            videos.append({"id": v["id"], "titre": v["snippet"]["title"],
                           "date": _local(v["snippet"]["publishedAt"]).isoformat(),
                           "url": f"https://youtube.com/shorts/{v['id']}",
                           "invite": _invite_desc(v["snippet"].get("description", "")),
                           "miniature": f"https://i.ytimg.com/vi/{v['id']}/mqdefault.jpg",
                           "vues": int(s.get("viewCount", 0)), "likes": int(s.get("likeCount", 0)),
                           "commentaires": int(s.get("commentCount", 0))})
    chaine = {"id": ch["id"], "nom": ch["snippet"]["title"],
              "avatar": ch["snippet"]["thumbnails"]["default"]["url"],
              "abonnes": int(ch["statistics"].get("subscriberCount", 0))}
    return chaine, videos


def dailymotion() -> tuple[dict, list[dict]]:
    nom = os.environ.get("DM_CHANNEL")
    if not nom:
        return {}, []
    # L'API publique est en retard (le 08/10 : 2 vues et l'ancien titre, quand le
    # Studio en montrait 45) : avec la cle privee, on lit les chiffres du Studio.
    base, h = DM, {}
    try:
        import dailymotion as D
        if D.actif():
            base, h = D.API + "/rest", {"Authorization": f"Bearer {D._jeton()}"}
    except Exception as e:                       # noqa: BLE001
        print(f"Dailymotion : cle privee refusee ({type(e).__name__}), API publique")
    u = requests.get(f"{base}/user/{nom}", timeout=30, headers=h,
                     params={"fields": "followers_total"}).json()
    videos, page = [], 1
    while page and page <= 10:
        r = requests.get(f"{base}/user/{nom}/videos", timeout=30, headers=h, params={
            "fields": "id,title,created_time,views_total,likes_total",
            "limit": 100, "page": page}).json()
        for v in r.get("list", []):
            videos.append({"id": v["id"], "titre": v["title"],
                           "url": f"https://www.dailymotion.com/video/{v['id']}",
                           "date": datetime.fromtimestamp(v["created_time"]).astimezone().isoformat(),
                           "vues": int(v.get("views_total") or 0),
                           "likes": int(v.get("likes_total") or 0)})
        page = page + 1 if r.get("has_more") else None
    return {"abonnes": int(u.get("followers_total") or 0)}, videos


def commentaires(chaine_id: str, titres: dict[str, str]) -> list[dict]:
    """Les 100 derniers fils de commentaires de la chaine (API publique)."""
    r = requests.get(f"{YT}/commentThreads", timeout=30, params={
        "part": "snippet,replies", "allThreadsRelatedToChannelId": chaine_id,
        "maxResults": 100, "order": "time", "textFormat": "plainText",
        "key": os.environ["YT_API_KEY"]}).json()
    out = []
    for t in r.get("items", []):
        top = t["snippet"]["topLevelComment"]
        s = top["snippet"]
        reponses = [{"auteur": x["snippet"]["authorDisplayName"],
                     "texte": x["snippet"]["textDisplay"],
                     "date": _local(x["snippet"]["publishedAt"]).isoformat(),
                     "chaine": x["snippet"].get("authorChannelId", {}).get("value") == chaine_id}
                    for x in t.get("replies", {}).get("comments", [])]
        out.append({"id": top["id"], "video": t["snippet"]["videoId"],
                    "titre": titres.get(t["snippet"]["videoId"], ""),
                    "auteur": s["authorDisplayName"],
                    "avatar": s.get("authorProfileImageUrl", ""),
                    "texte": s["textDisplay"], "likes": s.get("likeCount", 0),
                    "date": _local(s["publishedAt"]).isoformat(),
                    "nb_reponses": t["snippet"].get("totalReplyCount", 0),
                    "repondu": any(x["chaine"] for x in reponses),
                    "reponses": sorted(reponses, key=lambda x: x["date"])})
    return out


def analytics() -> list[dict]:
    """Vues et abonnes par jour selon YouTube Analytics (90 jours). Exact mais en
    retard de 2-3 jours : l'appli le complete avec ses propres releves."""
    try:
        from googleapiclient.discovery import build
        from upload import load_creds
        fin = date.today()
        r = build("youtubeAnalytics", "v2", credentials=load_creds(),
                  cache_discovery=False).reports().query(
            ids="channel==MINE", startDate=str(fin - timedelta(days=90)), endDate=str(fin),
            metrics="views,estimatedMinutesWatched,subscribersGained,subscribersLost,likes",
            dimensions="day", sort="day").execute()
        cols = [c["name"] for c in r["columnHeaders"]]
        return [dict(zip(cols, l)) for l in r.get("rows", [])]
    except (Exception, SystemExit) as e:        # noqa: BLE001 (jeton expire, quota...)
        print(f"Analytics indisponible ({type(e).__name__}: {str(e)[:80]})")
        return []


# ------------------------------------------------------------------ cote Pi
def sources_par_clip() -> dict[str, dict]:
    """Titre normalise -> source (invite, id). Tous les index du Pi, publies ou non."""
    out = {}
    for f in sorted(C.OUT.glob("*.json")):
        try:
            idx = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        src = {"id": idx.get("id", f.stem),
               "invite": C.invite(idx.get("titre", "")) or idx.get("titre", "")[:40]}
        for c in idx.get("clips", []):
            out[_nu(c.get("titre", ""))] = src
    return out


def etat_pi() -> dict:
    stock = [{"titre": c.get("titre", ""), "score": sc,
              "source": C.invite(idx.get("titre", "")) or vid}
             for sc, vid, n, c, idx in S.stock()]
    faites = S.sources_faites()
    restantes = [i for i in S.sources_choisies() if i not in faites]
    # prochains creneaux : les deux heures fixes, en sautant ce qui est deja passe
    maintenant, prochaines = datetime.now(), []
    for j in range(3):
        for h in CRENEAUX:
            t = (maintenant + timedelta(days=j)).replace(hour=h, minute=0, second=0, microsecond=0)
            if t > maintenant and len(prochaines) < len(stock) and len(prochaines) < 4:
                prochaines.append({"heure": t.isoformat(timespec="minutes"),
                                   "titre": stock[len(prochaines)]["titre"]})
    temp = None
    try:
        temp = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        pass
    disque = shutil.disk_usage(C.MEDIA if C.MEDIA.exists() else RACINE)
    journal = S.LOG.read_text(encoding="utf-8").splitlines()[-15:] if S.LOG.exists() else []
    return {"stock": stock, "prochaines": prochaines,
            "sources_restantes": len(restantes), "format": P.format_publie(),
            "par_jour": P.par_jour(), "temperature": temp,
            "disque_libre_go": round(disque.free / 1e9, 1),
            "disque_total_go": round(disque.total / 1e9, 1), "journal": journal}


# ------------------------------------------------------------------ assemblage
def relever(ancien: dict) -> dict:
    yt_ch, yt = youtube()
    try:
        dm_ch, dm = dailymotion()
    except Exception as e:                       # noqa: BLE001
        print(f"Dailymotion injoignable ({type(e).__name__}) : releve YouTube seule")
        dm_ch, dm = {}, []
    sources = sources_par_clip()
    clips: dict[str, dict] = {}
    for plateforme, liste in (("youtube", yt), ("dailymotion", dm)):
        for v in liste:
            cle = _nu(v["titre"])
            c = clips.setdefault(cle, {
                "id": v["id"], "titre": v["titre"].replace("#Shorts", "").strip(),
                "date": v["date"]})
            c[plateforme] = {k: v[k] for k in v
                             if k not in ("titre", "date", "miniature", "invite")}
            if v.get("invite"):
                c["source"] = {"id": "", "invite": v["invite"]}
            if "miniature" in v:
                c["miniature"] = v["miniature"]
            if plateforme == "youtube":
                c["id"], c["date"] = v["id"], v["date"]
    for cle, c in clips.items():
        c["vues"] = sum(c.get(p, {}).get("vues", 0) for p in ("youtube", "dailymotion"))
        c["likes"] = sum(c.get(p, {}).get("likes", 0) for p in ("youtube", "dailymotion"))
        if cle in sources:
            c["source"] = sources[cle]
        h = _local(c["date"]).hour
        c["creneau"] = "12 h" if 11 <= h < 15 else "19 h" if 17 <= h < 22 else "autre"

    maintenant = datetime.now()
    totaux = {"youtube": sum(v["vues"] for v in yt), "dailymotion": sum(v["vues"] for v in dm)}
    stats = {"version": 2, "maj": maintenant.isoformat(timespec="minutes"),
             "chaine": {"nom": yt_ch["nom"], "avatar": yt_ch["avatar"], "id": yt_ch["id"],
                        "youtube": {"abonnes": yt_ch["abonnes"]}, "dailymotion": dm_ch},
             "totaux": totaux}

    # --- instantanes horaires, et ce qu'on en deduit (aujourd'hui, 24 h)
    inst = [i for i in ancien.get("instantanes", [])
            if datetime.fromisoformat(i["t"]) > maintenant - timedelta(hours=HEURES_GARDEES)]
    inst.append({"t": stats["maj"], **totaux, "abonnes": yt_ch["abonnes"],
                 "clips": {c["id"]: c["vues"] for c in clips.values()}})
    stats["instantanes"] = inst
    minuit = maintenant.replace(hour=0, minute=0, second=0, microsecond=0)
    avant_minuit = [i for i in inst if datetime.fromisoformat(i["t"]) < minuit]
    il_y_a_24h = [i for i in inst
                  if datetime.fromisoformat(i["t"]) <= maintenant - timedelta(hours=23, minutes=30)]
    hist = [h for h in ancien.get("historique", []) if h["date"] < str(minuit.date())]
    # reference de minuit : le dernier instantane d'hier, a defaut le dernier jour connu
    ref = avant_minuit[-1] if avant_minuit else (hist[-1] if hist else None)
    stats["aujourdhui"] = {p: totaux[p] - ref.get(p, 0) for p in totaux} if ref else None
    if ref and "abonnes" in ref:
        stats["aujourdhui"]["abonnes"] = yt_ch["abonnes"] - ref["abonnes"]
    ref24 = il_y_a_24h[-1] if il_y_a_24h else None
    for c in clips.values():
        if ref and "clips" in ref:
            c["aujourdhui"] = c["vues"] - ref["clips"].get(c["id"], 0)
        if ref24:
            c["gain24"] = c["vues"] - ref24["clips"].get(c["id"], 0)

    # --- un point par jour : la chaine (365 j) et chaque clip (60 j)
    jour = str(maintenant.date())
    stats["historique"] = (hist + [{"date": jour, **totaux, "abonnes": yt_ch["abonnes"]}])[-365:]
    series = ancien.get("series", {})
    limite = str(maintenant.date() - timedelta(days=JOURS_PAR_CLIP))
    for c in clips.values():
        s = [p for p in series.get(c["id"], []) if p[0] != jour and p[0] >= limite]
        series[c["id"]] = s + [[jour, c["vues"]]]
    stats["series"] = {k: v for k, v in series.items() if k in {c["id"] for c in clips.values()}}

    stats["clips"] = sorted(clips.values(), key=lambda c: c["date"], reverse=True)
    stats["analytics"] = analytics() or ancien.get("analytics", [])
    try:
        stats["commentaires"] = commentaires(yt_ch["id"], {c["id"]: c["titre"]
                                                           for c in clips.values()})
    except Exception as e:                       # noqa: BLE001
        print(f"Commentaires indisponibles ({type(e).__name__})")
        stats["commentaires"] = ancien.get("commentaires", [])
    try:
        stats["pi"] = etat_pi()
    except Exception as e:                       # noqa: BLE001
        print(f"Etat du Pi indisponible ({type(e).__name__}: {e})")
    return stats


# ------------------------------------------------------------------ GitHub
def lire_ancien() -> tuple[dict, str | None]:
    if G._api("GET", f"/branches/{BRANCHE}").status_code == 404:
        sha = G._api("GET", "/git/ref/heads/main").json()["object"]["sha"]
        G._api("POST", "/git/refs",
               json={"ref": f"refs/heads/{BRANCHE}", "sha": sha}).raise_for_status()
    r = G._api("GET", f"/contents/{FICHIER}", params={"ref": BRANCHE})
    if r.status_code != 200:
        return {}, None
    d = r.json()
    if not d.get("content"):             # au-dela d'1 Mo, l'API ne renvoie pas le contenu
        d["content"] = base64.b64encode(requests.get(d["download_url"], timeout=60).content)
    return json.loads(base64.b64decode(d["content"])), d["sha"]


def publier(stats: dict, sha: str | None) -> None:
    corps = {"message": f"stats {stats['maj']}", "branch": BRANCHE,
             "content": base64.b64encode(json.dumps(stats, ensure_ascii=False,
                                                    separators=(",", ":")).encode()).decode()}
    if sha:
        corps["sha"] = sha
    G._api("PUT", f"/contents/{FICHIER}", json=corps).raise_for_status()


def main() -> int:
    C.charger_env()
    local = "--local" in sys.argv
    f_local = C.MEDIA / FICHIER
    if local:
        ancien = json.loads(f_local.read_text(encoding="utf-8")) if f_local.exists() else {}
        sha = None
    else:
        ancien, sha = lire_ancien()
    stats = relever(ancien)
    a = stats.get("aujourdhui") or {}
    print(f"{len(stats['clips'])} clips — YouTube {stats['totaux']['youtube']:,} vues "
          f"(+{a.get('youtube', '?')} aujourd'hui), Dailymotion {stats['totaux']['dailymotion']:,} "
          f"— {len(stats['commentaires'])} commentaires".replace(",", " "))
    if local:
        f_local.write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
        return 0
    publier(stats, sha)
    print(f"publie sur la branche « {BRANCHE} »")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
