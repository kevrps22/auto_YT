"""Point d'entree de Clipper sur GitHub Actions (voir .github/workflows/clipper.yml).

    python clipper/cloud.py <id> --etape transcription
    python clipper/cloud.py <id> --etape rendu --n 8

La video `media/src/<id>.mp4` et sa fiche `<id>.info.json` sont deposees par
l'Orange Pi : YouTube bloque les telechargements depuis les serveurs de GitHub.

Deux etapes separees, car une tache GitHub est coupee au bout de 6 heures : le
workflow sauvegarde la transcription des qu'elle est finie, et une relance repart
de la sans tout refaire.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pipeline as P  # noqa: E402


def _suivi_console():
    """Journal lisible dans GitHub : une ligne par tranche de 5 % ou par etape."""
    debut, dernier = time.time(), {"etape": None, "pct": -5}

    def suivi(etape: str, avance: float, detail: str) -> None:
        pct = int(avance * 100)
        if etape != dernier["etape"] or pct >= dernier["pct"] + 5:
            dernier.update(etape=etape, pct=pct)
            m, s = divmod(int(time.time() - debut), 60)
            print(f"[{m:3d}:{s:02d}] {pct:3d} %  {etape:15} {detail}", flush=True)

    return suivi


def _resume(index: dict) -> None:
    """Tableau des clips dans la page de resultat du workflow."""
    fichier = os.environ.get("GITHUB_STEP_SUMMARY")
    if not fichier:
        return
    lignes = [f"### {index.get('titre', index.get('id'))}", "",
              f"Chaine : {index.get('chaine', '')} · Licence : {index.get('licence', '')}", "",
              "| Score | Duree | Publiable | Titre |", "|---|---|---|---|"]
    for c in index.get("clips", []):
        lignes.append(f"| {c.get('score')} | {c['duree']:.0f} s | "
                      f"{'oui' if c.get('publiable') else 'NON'} | {c['titre']} |")
    with open(fichier, "a", encoding="utf-8") as f:
        f.write("\n".join(lignes) + "\n")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    vid = sys.argv[1]
    etape = sys.argv[sys.argv.index("--etape") + 1] if "--etape" in sys.argv else "tout"
    n = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 4
    src = P.SRC / f"{vid}.mp4"
    info_f = src.with_suffix(".info.json")
    if not src.exists() or not info_f.exists():
        print(f"Source absente : il faut {src.name} et {info_f.name} dans {P.SRC}")
        return 1
    meta = json.loads(info_f.read_text(encoding="utf-8"))
    print(f"Source : {meta.get('titre')} ({meta.get('chaine')}) · licence : {meta.get('licence')}")
    P.assurer_ffmpeg()
    suivi = _suivi_console()

    if etape in ("transcription", "tout"):
        P.preparer(src, suivi)
    if etape in ("rendu", "tout"):
        if not src.with_suffix(".clips.json").exists() and not os.environ.get("GEMINI_API_KEY"):
            print("GEMINI_API_KEY manquante : impossible de choisir les moments.")
            return 1
        index = P.traiter_fichier(src, meta, n, suivi)
        _resume(index)
        bloques = sum(1 for c in index["clips"] if not c.get("publiable"))
        print(f"{len(index['clips'])} clips rendus"
              + (f", dont {bloques} NON publiables (licence)" if bloques else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
