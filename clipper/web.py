"""Page locale du clipper : colle un lien YouTube, recupere des Shorts.

    python clipper/web.py              ouvre http://localhost:8765 dans le navigateur
    python clipper/web.py --no-browser

La page est aussi accessible depuis le telephone, sur le meme Wi-Fi, a l'adresse
affichee au demarrage. Windows peut demander d'autoriser Python dans le pare-feu.

Un seul traitement a la fois : transcription et rendu occupent tout le processeur.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pipeline as P  # noqa: E402

PORT = 8765
app = Flask(__name__, static_folder=None)

_verrou = threading.Lock()
_job: dict = {"etat": "libre"}


def _maj(**champs) -> None:
    with _verrou:
        _job.update(champs)


def _lancer(url: str, n: int, rechoisir: bool) -> None:
    debut = time.time()

    def suivi(etape: str, avance: float, detail: str) -> None:
        _maj(etape=etape, avance=round(avance, 4), detail=detail,
             ecoule=round(time.time() - debut))

    try:
        index = P.traiter(url, n, suivi, rechoisir)
        _maj(etat="termine", avance=1.0, video=index["id"], ecoule=round(time.time() - debut))
    except Exception as e:                    # l'erreur s'affiche sur la page, pas seulement ici
        traceback.print_exc()
        _maj(etat="erreur", erreur=f"{type(e).__name__} : {e}")


@app.get("/")
def accueil():
    return send_from_directory(HERE / "web", "index.html")


@app.post("/api/generer")
def generer():
    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    if "youtu" not in url:
        return jsonify(erreur="Colle un lien YouTube."), 400
    n = max(1, min(12, int(data.get("n") or 8)))
    with _verrou:
        if _job.get("etat") == "en_cours":
            return jsonify(erreur="Un traitement est deja en cours."), 409
        _job.clear()
        _job.update(etat="en_cours", url=url, n=n, etape="telechargement", avance=0.0,
                    detail="demarrage", ecoule=0)
    threading.Thread(target=_lancer, args=(url, n, bool(data.get("rechoisir"))),
                     daemon=True).start()
    return jsonify(ok=True)


@app.get("/api/etat")
def etat():
    with _verrou:
        return jsonify(dict(_job))


@app.get("/api/videos")
def videos():
    out = []
    for f in sorted(P.OUT.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            index = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        index["clips"] = [c for c in index.get("clips", []) if (P.OUT / c["fichier"]).exists()]
        if index["clips"]:
            out.append(index)
    return jsonify(out)


@app.get("/clips/<path:nom>")
def clip(nom: str):
    if "/" in nom or "\\" in nom or not nom.endswith(".mp4"):
        abort(404)
    # conditional=True : requetes partielles, donc lecture et avance rapide dans la video
    return send_from_directory(P.OUT, nom, conditional=True,
                               as_attachment=request.args.get("dl") == "1")


def _ip_locale() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))       # aucun paquet envoye : sert a lire l'IP du reseau local
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


if __name__ == "__main__":
    P.OUT.mkdir(exist_ok=True)
    print(f"\n  Clipper pret :  http://localhost:{PORT}")
    print(f"  Depuis le telephone (meme Wi-Fi) :  http://{_ip_locale()}:{PORT}\n")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
