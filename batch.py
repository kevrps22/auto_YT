"""Genere en serie tous les sujets d'une feuille Google Sheets publiee.

    python batch.py --list          # affiche le planning sans rien generer
    python batch.py                 # genere tout ce qui manque
    python batch.py --limit 5       # s'arrete apres 5 videos

Une video deja presente dans output/lib/ n'est jamais regeneree : le script est
relancable apres une coupure ou un quota Gemini epuise.
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

HERE = Path(__file__).parent
SHEET_ID = "1z7z6DANL_ifeMV8B8sKxaF9T9KtLj6lHMO1f0Y-RpWU"
LIB = HERE / "output" / "lib"
PY = str(HERE / ".venv" / "Scripts" / "python.exe")


def slug(s: str) -> str:
    """MEME slugification que archive() dans generate.py — sans retirer les accents.
    'mystère' y devient 'myst-re' (le è tombe dans [^a-z0-9]). Normaliser en ASCII
    ici donnait 'mystere' : les 8 sujets accentues passaient pour non generes."""
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:40]


def load_rows() -> list[dict]:
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8"))))
    out = []
    for row in rows:
        topic = (row.get("Sujet / Titre du Short") or "").strip()
        if topic:
            out.append({"date": (row.get("Date") or "").strip(),
                        "cat": (row.get("Catégorie") or "").strip(),
                        "topic": topic})
    return out


def already_done(topic: str) -> Path | None:
    """Une video de ce sujet est-elle deja dans la galerie ?"""
    want = slug(topic)
    if not LIB.exists():
        return None
    for d in LIB.iterdir():
        # les dossiers sont nommes <date>_<slug du topic>
        if d.is_dir() and d.name.split("_", 2)[-1] == want:
            return d
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="affiche sans generer")
    ap.add_argument("--limit", type=int, help="nombre max de videos")
    a = ap.parse_args()

    rows = load_rows()
    todo = [r for r in rows if not already_done(r["topic"])]
    print(f"{len(rows)} sujets dans la feuille — {len(rows) - len(todo)} deja generes, "
          f"{len(todo)} a produire\n")

    if a.list:
        for r in rows:
            mark = "[ok]  " if already_done(r["topic"]) else "[ a faire ]"
            print(f"  {r['date']:>11}  {r['cat']:<12} {mark} {r['topic']}")
        return

    todo = todo[:a.limit] if a.limit else todo
    ok, ko = [], []
    for n, r in enumerate(todo, 1):
        print(f"\n===== [{n}/{len(todo)}] {r['topic']} =====", flush=True)
        t0 = time.time()
        p = subprocess.run([PY, "-u", "generate.py", r["topic"]],
                           cwd=str(HERE), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        tail = [l for l in (p.stdout or "").splitlines() if l.startswith("[")][-6:]
        print("\n".join(tail), flush=True)
        if p.returncode == 0 and already_done(r["topic"]):
            ok.append(r["topic"])
            print(f"  -> OK en {time.time() - t0:.0f}s", flush=True)
        else:
            err = (p.stderr or "").strip().splitlines()
            ko.append((r["topic"], err[-1] if err else f"code {p.returncode}"))
            print(f"  -> ECHEC : {ko[-1][1][:120]}", flush=True)

    print(f"\n===== BILAN {datetime.now():%H:%M} =====")
    print(f"  {len(ok)} generees, {len(ko)} en echec")
    for t, e in ko:
        print(f"    - {t} : {e[:100]}")


if __name__ == "__main__":
    main()
