"""Revenus Dailymotion estimes, via la Reporting API (GraphQL partenaire).

Dailymotion reserve cette API aux partenaires verifies : avec une cle ordinaire,
la demande est refusee. On ne fait alors rien d'autre que noter la raison, que
l'appli affiche telle quelle — jamais un chiffre invente.

Un rapport met jusqu'a deux heures a etre genere. D'ou un fonctionnement en deux
temps, au fil des releves horaires de stats.py :
- une fois par jour, on DEMANDE un rapport (revenus + vues par jour, 30 jours) ;
- aux releves suivants, on regarde s'il est pret, et on le telecharge.
L'etat entre deux releves est garde dans media/dm_rapport.json.
"""
from __future__ import annotations

import csv
import io
import json
import os
from datetime import date, datetime, timedelta

import requests

import clipper as C

API = "https://partner.api.dailymotion.com"
ETAT = C.MEDIA / "dm_rapport.json"

DEMANDE = """mutation ($input: AskPartnerReportFileInput!) {
  askPartnerReportFile(input: $input) { reportFile { reportToken } }
}"""
SUIVI = """query ($reportToken: String!) {
  partner { reportFile(reportToken: $reportToken) {
    status downloadLinks { edges { node { link } } } } }
}"""


def _jeton() -> str:
    r = requests.post(f"{API}/oauth/v1/token", timeout=30, data={
        "grant_type": "client_credentials", "scope": "access_revenue create_reports",
        "client_id": os.environ["DM_API_KEY"], "client_secret": os.environ["DM_API_SECRET"]})
    if r.status_code >= 400:
        raise RuntimeError(f"jeton refuse ({r.status_code}) : {r.text[:160]}")
    return r.json()["access_token"]


def _gql(requete: str, variables: dict) -> dict:
    r = requests.post(f"{API}/graphql", timeout=60,
                      headers={"Authorization": f"Bearer {_jeton()}"},
                      json={"query": requete, "variables": variables})
    d = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code >= 400 or d.get("errors"):
        msg = (d.get("errors") or [{}])[0].get("message") or r.text[:160]
        raise RuntimeError(f"Reporting API : {msg}")
    return d["data"]


def _lire_csv(lien: str) -> list[dict]:
    texte = requests.get(lien, timeout=120).text
    jours = []
    for l in csv.DictReader(io.StringIO(texte)):
        cle = {k.lower(): v for k, v in l.items()}
        gain = next((v for k, v in cle.items() if "earning" in k), "0") or "0"
        vues = next((v for k, v in cle.items() if "view" in k), "0") or "0"
        jours.append({"date": cle.get("day", ""), "euros": round(float(gain), 4),
                      "vues": int(float(vues))})
    return sorted(jours, key=lambda j: j["date"])


def releve() -> dict:
    """Ce que l'appli affiche : {"jours": [...], "total_30j": x, "maj": ...} ou
    {"erreur": "..."}. Ne leve jamais."""
    if not (os.environ.get("DM_API_KEY") and os.environ.get("DM_CHANNEL")):
        return {"erreur": "cle Dailymotion absente du .env"}
    etat = json.loads(ETAT.read_text(encoding="utf-8")) if ETAT.exists() else {}
    try:
        if etat.get("jeton_rapport"):
            f = _gql(SUIVI, {"reportToken": etat["jeton_rapport"]})["partner"]["reportFile"]
            if f["status"] == "FINISHED":
                liens = [e["node"]["link"] for e in f["downloadLinks"]["edges"]]
                jours = [j for l in liens for j in _lire_csv(l)]
                etat = {"resultat": {"jours": jours,
                                     "total_30j": round(sum(j["euros"] for j in jours), 2),
                                     "maj": datetime.now().isoformat(timespec="minutes")},
                        "demande_le": etat.get("demande_le")}
            elif f["status"] not in ("IN_PROGRESS", "PENDING", "PROCESSING"):
                etat.pop("jeton_rapport")
                etat["erreur"] = f"rapport {f['status']}"
        if not etat.get("jeton_rapport") and etat.get("demande_le") != str(date.today()):
            fin = date.today() - timedelta(days=1)
            d = _gql(DEMANDE, {"input": {
                "metrics": ["ESTIMATED_EARNINGS_EUR", "VIEWS"], "dimensions": ["DAY"],
                "filters": {"videoOwnerChannelSlug": os.environ["DM_CHANNEL"]},
                "startDate": str(fin - timedelta(days=29)), "endDate": str(fin),
                "product": "CONTENT"}})
            etat["jeton_rapport"] = d["askPartnerReportFile"]["reportFile"]["reportToken"]
            etat["demande_le"] = str(date.today())
            etat.pop("erreur", None)
    except Exception as e:                       # noqa: BLE001
        etat["erreur"] = str(e)[:240]
        etat["demande_le"] = str(date.today())   # on ne reessaie que demain
        etat.pop("jeton_rapport", None)
    ETAT.parent.mkdir(parents=True, exist_ok=True)
    ETAT.write_text(json.dumps(etat, ensure_ascii=False, indent=1), encoding="utf-8")
    if etat.get("resultat"):
        return etat["resultat"]
    if etat.get("erreur"):
        return {"erreur": etat["erreur"]}
    return {"en_attente": True}
