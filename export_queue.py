"""Prepare la file d'attente du Raspberry Pi sur une carte SD / cle USB.

Copie les videos NON PUBLIEES (video + meta.json uniquement) vers le support.
Les miniatures et posters restent sur le PC : inutiles au Pi, ils gonflent la carte.

    python export_queue.py --dest E:\queue              # tout ce qui n'est pas publie
    python export_queue.py --dest E:\queue --limit 100  # 100 videos max
    python export_queue.py --dest E:\queue --dry-run
"""
from __future__ import annotations

import argparse
import glob
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from daily_upload import load_env, norm, published   # meme logique, zero doublon

HERE = Path(__file__).parent
LIB = HERE / "output" / "lib"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", required=True, help="dossier cible (carte SD)")
    ap.add_argument("--limit", type=int, help="nombre max de videos")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    load_env()
    dest = Path(a.dest)
    titles, _ = published()

    todo = []
    for d in sorted(glob.glob(str(LIB / "*"))):
        f = Path(d) / "meta.json"
        if not f.exists():
            continue
        m = json.loads(f.read_text(encoding="utf-8"))
        if norm(m.get("title", "")) in titles:
            continue                       # deja en ligne
        if (dest / Path(d).name).exists():
            continue                       # deja sur la carte
        todo.append((m.get("virality") or 0, Path(d), m))

    todo.sort(key=lambda x: -x[0])
    if a.limit:
        todo = todo[:a.limit]

    if not todo:
        print("Rien a copier : la carte est deja a jour.")
        return 0

    total = 0
    for _, src, meta in todo:
        video = src / meta.get("file", "")
        if not video.exists():
            mp4 = sorted(src.glob("*.mp4"))
            if not mp4:
                continue
            video = mp4[0]
        total += video.stat().st_size
        print(f"  [{meta.get('virality')}] {meta.get('title')[:52]}")
        if a.dry_run:
            continue
        out = dest / src.name
        out.mkdir(parents=True, exist_ok=True)
        shutil.copy2(video, out / video.name)
        shutil.copy2(src / "meta.json", out / "meta.json")

    mo = total / 1024**2
    print(f"\n{len(todo)} videos, {mo:.0f} Mo ({mo/1024:.1f} Go) -> {dest}")
    print(f"soit {len(todo)} jours d'autonomie pour le Pi.")
    if a.dry_run:
        print("(--dry-run : rien n'a ete copie)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
