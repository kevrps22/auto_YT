"""Echanges avec GitHub : deposer une video source, lancer le rendu, recuperer les clips.

Sert depuis le PC aujourd'hui, et depuis l'Orange Pi ensuite.

    python clipper/github_io.py envoyer <lien YouTube> [--n 8] [--sans-cache]
    python clipper/github_io.py suivre <id>
    python clipper/github_io.py recuperer <id>

Le telechargement se fait ICI, depuis l'adresse de la maison : YouTube bloque les
serveurs de GitHub. GitHub ne fait que la transcription et le rendu.

Acces : variable GITHUB_TOKEN (jeton a portee limitee a ce depot, pour l'Orange Pi),
sinon l'acces deja enregistre par Git sur la machine (PC).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

DEPOT = os.environ.get("GITHUB_REPO", "kevrps22/auto_YT")
API = f"https://api.github.com/repos/{DEPOT}"
WORKFLOW = "clipper.yml"
BRANCHE = "main"


def jeton() -> str:
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok
    r = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                       capture_output=True, text=True, timeout=30)
    tok = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l).get("password")
    if not tok:
        raise RuntimeError("Aucun acces GitHub : definis GITHUB_TOKEN.")
    return tok


def _h(extra: dict | None = None) -> dict:
    return {"Authorization": f"Bearer {jeton()}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", **(extra or {})}


def _api(methode: str, chemin: str, **kw) -> requests.Response:
    r = requests.request(methode, chemin if chemin.startswith("http") else API + chemin,
                         headers=_h(kw.pop("headers", None)), timeout=kw.pop("timeout", 60), **kw)
    if r.status_code >= 400 and r.status_code != 404:
        raise RuntimeError(f"GitHub {methode} {chemin} -> {r.status_code} : {r.text[:300]}")
    return r


def release(vid: str, titre: str = "") -> dict:
    tag = f"clipper-{vid}"
    r = _api("GET", f"/releases/tags/{tag}")
    if r.status_code == 200:
        return r.json()
    return _api("POST", "/releases", json={
        "tag_name": tag, "target_commitish": BRANCHE, "name": f"Clipper · {titre or vid}",
        "body": "Source et clips generes par le workflow Clipper. Ne rien publier d'ici : "
                "c'est l'Orange Pi qui publie, depuis la maison."}).json()


# Au-dela de cette taille, GitHub a renvoye une erreur 500 « Error saving asset »
# APRES dix minutes de transfert. On decoupe donc, et une tranche ratee se refait
# seule sans tout reprendre. Le workflow recolle les tranches avec `cat`.
TRANCHE_MAX = 100 * 1024 * 1024


class _Tranche:
    """Portion de fichier lue par morceaux, avec avancement. La taille est exposee
    pour que requests envoie un Content-Length, exige par GitHub."""

    def __init__(self, chemin: Path, debut: int, longueur: int, etiquette: str = ""):
        self.f = chemin.open("rb")
        self.f.seek(debut)
        self.taille = self.reste = longueur
        self.lu, self.palier, self.t0, self.etiquette = 0, 20, time.time(), etiquette

    def __len__(self) -> int:
        return self.taille

    def read(self, n: int = -1) -> bytes:
        if self.reste <= 0:
            return b""
        bloc = self.f.read(min(n if n and n > 0 else 1 << 20, self.reste))
        self.reste -= len(bloc)
        self.lu += len(bloc)
        pct = 100 * self.lu // max(1, self.taille)
        if pct >= self.palier:
            self.palier = (pct // 20 + 1) * 20
            debit = self.lu / 1e6 / max(0.1, time.time() - self.t0)
            print(f"    {self.etiquette}{pct:3d} %  ({debit:.1f} Mo/s)", flush=True)
        return bloc

    def close(self) -> None:
        self.f.close()


def _assets(rel: dict) -> list[dict]:
    """Tous les fichiers de la release, y compris ceux d'un envoi INTERROMPU : ils y
    restent a l'etat "starter", absents de rel["assets"], et bloquent le meme nom."""
    return _api("GET", f"/releases/{rel['id']}/assets", params={"per_page": 100}).json()


def _supprimer(rel: dict, nom: str) -> None:
    for a in _assets(rel):
        if a["name"] == nom:
            _api("DELETE", f"/releases/assets/{a['id']}")


def _envoyer(rel: dict, nom: str, fichier: Path, debut: int, longueur: int,
             etiquette: str = "") -> None:
    for essai in range(1, 4):
        corps = _Tranche(fichier, debut, longueur, etiquette)
        try:
            _api("POST", rel["upload_url"].split("{")[0], params={"name": nom}, data=corps,
                 timeout=None, headers={"Content-Type": "application/octet-stream"})
            return
        except (RuntimeError, requests.RequestException) as e:
            print(f"    echec ({str(e)[:90]}) -> essai {essai}/3", flush=True)
            _supprimer(rel, nom)                      # une tranche incomplete bloquerait le nom
            time.sleep(5 * essai)
        finally:
            corps.close()
    raise RuntimeError(f"Envoi impossible apres 3 essais : {nom}")


def deposer(rel: dict, fichier: Path) -> None:
    taille = fichier.stat().st_size
    if taille <= TRANCHE_MAX:
        _supprimer(rel, fichier.name)
        print(f"  envoi {fichier.name} ({taille / 1e6:.0f} Mo)...", flush=True)
        t0 = time.time()
        _envoyer(rel, fichier.name, fichier, 0, taille)
        print(f"  envoye en {time.time() - t0:.0f} s", flush=True)
        return
    n = -(-taille // TRANCHE_MAX)
    deja = {a["name"]: a for a in _assets(rel) if a["state"] == "uploaded"}
    print(f"  envoi {fichier.name} ({taille / 1e6:.0f} Mo) en {n} tranches", flush=True)
    t0 = time.time()
    for i in range(n):
        nom = f"{fichier.name}.part{i + 1:02d}"
        longueur = min(TRANCHE_MAX, taille - i * TRANCHE_MAX)
        if deja.get(nom, {}).get("size") == longueur:      # reprise apres un echec
            print(f"    [{i + 1}/{n}] deja envoyee", flush=True)
            continue
        _supprimer(rel, nom)
        _envoyer(rel, nom, fichier, i * TRANCHE_MAX, longueur, f"[{i + 1}/{n}] ")
    _supprimer(rel, fichier.name)                          # l'entier ne doit pas trainer
    print(f"  envoye en {time.time() - t0:.0f} s", flush=True)


def lancer(vid: str, n: int = 8, rechoisir: bool = False) -> None:
    _api("POST", f"/actions/workflows/{WORKFLOW}/dispatches", json={
        "ref": BRANCHE, "inputs": {"video_id": vid, "n": str(n),
                                   "rechoisir": "true" if rechoisir else "false"}})
    print(f"Rendu lance sur GitHub pour {vid}.")


def dernier_run(vid: str) -> dict | None:
    runs = _api("GET", f"/actions/workflows/{WORKFLOW}/runs",
                params={"event": "workflow_dispatch", "per_page": 20}).json()
    for run in runs.get("workflow_runs", []):
        if vid in (run.get("display_title") or ""):
            return run
    return None


def suivre(vid: str, pause: int = 60) -> dict:
    """Attend la fin du dernier rendu de cette video, en affichant l'etape en cours."""
    run, etape_vue = None, None
    for _ in range(10):                               # le run met quelques secondes a apparaitre
        run = dernier_run(vid)
        if run:
            break
        time.sleep(6)
    if not run:
        raise RuntimeError(f"Aucun rendu trouve pour {vid}.")
    print(f"Suivi : {run['html_url']}")
    while True:
        run = _api("GET", f"/actions/runs/{run['id']}").json()
        jobs = _api("GET", f"/actions/runs/{run['id']}/jobs").json().get("jobs", [])
        en_cours = [s["name"] for j in jobs for s in j.get("steps", []) if s["status"] == "in_progress"]
        etape = en_cours[0] if en_cours else run["status"]
        if etape != etape_vue:
            print(f"  {datetime.now():%H:%M}  {etape}", flush=True)
            etape_vue = etape
        if run["status"] == "completed":
            print(f"Termine : {run['conclusion']}")
            return run
        time.sleep(pause)


def recuperer(vid: str, dest: Path | None = None) -> dict:
    dest = dest or HERE / "out"
    dest.mkdir(parents=True, exist_ok=True)
    rel = _api("GET", f"/releases/tags/clipper-{vid}")
    if rel.status_code == 404:
        raise RuntimeError(f"Pas de release clipper-{vid}.")
    voulus = [a for a in rel.json()["assets"]
              if a["name"] == f"{vid}.json" or (a["name"].startswith(f"{vid}_") and a["name"].endswith(".mp4"))]
    for a in voulus:
        cible = dest / a["name"]
        if cible.exists() and cible.stat().st_size == a["size"]:
            continue
        with requests.get(a["url"], headers=_h({"Accept": "application/octet-stream"}),
                          stream=True, timeout=120) as r:
            r.raise_for_status()
            with cible.open("wb") as f:
                for bloc in r.iter_content(1 << 20):
                    f.write(bloc)
        print(f"  recupere {a['name']}")
    return json.loads((dest / f"{vid}.json").read_text(encoding="utf-8"))


def envoyer(url: str, n: int = 8, avec_cache: bool = True) -> str:
    import pipeline as P
    P.assurer_ffmpeg()
    src, meta = P.telecharger(url, lambda e, a, d: None)
    print(f"Source : {meta['titre']} ({meta['chaine']}) · {meta['licence']}")
    if "creative" not in (meta.get("licence") or "").lower():
        print("ATTENTION : licence YouTube standard, les clips seront marques NON publiables.")
    rel = release(meta["id"], meta["titre"])
    deposer(rel, src.with_suffix(".info.json"))
    if avec_cache:                                    # une transcription faite sur le PC evite des heures
        for ext in (".words.json", ".scenes.txt"):
            if src.with_suffix(ext).exists():
                deposer(rel, src.with_suffix(ext))
    deposer(rel, src)
    lancer(meta["id"], n)
    return meta["id"]


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    cmd, arg = sys.argv[1], sys.argv[2]
    n = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 8
    if cmd == "envoyer":
        vid = envoyer(arg, n, "--sans-cache" not in sys.argv)
        run = suivre(vid)
        if run["conclusion"] == "success":
            index = recuperer(vid)
            print(f"{len(index['clips'])} clips dans clipper/out")
    elif cmd == "suivre":
        suivre(arg)
    elif cmd == "recuperer":
        index = recuperer(arg)
        print(f"{len(index['clips'])} clips dans clipper/out")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
