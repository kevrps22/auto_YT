"""Banc d'essai de la SELECTION VISUELLE, sans produire de video.

Le montage coute plusieurs minutes par sujet : impossible d'iterer dessus. Ce
script s'arrete juste apres le choix des clips et dit combien chaque sujet en
obtient. Il appelle `select_visuals`, exactement la fonction qu'utilise la
chaine reelle — un test qui re-implemente ce chemin donne de faux succes.

    python probe.py                      la liste de sujets par defaut
    python probe.py "les requins" "le sel"
    VISUAL_DEBUG=1 python probe.py "..."  dit pourquoi chaque clip est ecarte

Objectif : BODY >= 8 sur la quasi-totalite des sujets. En dessous, la video se
construit sur des images repetees.
"""
from __future__ import annotations

import sys
import time

import generate as G

SUJETS = [
    "l'argent liquide", "les cartes bancaires", "le cafe", "tes dents",
    "le sel", "la peste noire", "le grand incendie de Londres 1666",
    "les fourmis", "les requins", "le sommeil",
]
OBJECTIF = 8


def probe(topic: str) -> dict:
    # l'anti-doublon est global : sans remise a zero, le 2e sujet herite du 1er
    G._seen_ids.clear()
    G._seen_prefixes.clear()
    t0 = time.time()
    script = G.make_script(topic)
    V = G.select_visuals(script, topic)
    return {"topic": topic, "titre": script.get("title", ""),
            "hook": len(V["hook_clips"]), "body": len(V["body_clips"]),
            "total": len(V["clips"]), "sec": time.time() - t0}


def main() -> int:
    sujets = sys.argv[1:] or SUJETS
    res = []
    for t in sujets:
        print(f"\n{'=' * 72}\n### {t}\n{'=' * 72}")
        try:
            r = probe(t)
        except Exception as e:
            print(f"  ECHEC {type(e).__name__}: {e}")
            res.append({"topic": t, "hook": 0, "body": 0, "total": 0, "sec": 0,
                        "titre": f"ECHEC {type(e).__name__}"})
            continue
        res.append(r)
        print(f"  -> hook {r['hook']}  body {r['body']}  total {r['total']}"
              f"  ({r['sec']:.0f}s)")

    print(f"\n{'=' * 72}\nBILAN   (objectif : body >= {OBJECTIF})\n{'=' * 72}")
    print(f"{'body':>5} {'hook':>5} {'tot':>4}  sujet")
    ok = 0
    for r in sorted(res, key=lambda x: x["body"]):
        flag = "OK " if r["body"] >= OBJECTIF else "-- "
        ok += r["body"] >= OBJECTIF
        print(f"{flag}{r['body']:>3} {r['hook']:>5} {r['total']:>4}  {r['topic']}")
    print(f"\n{ok}/{len(res)} sujets atteignent l'objectif")
    bodies = sorted(r["body"] for r in res)
    if bodies:
        print(f"mediane body : {bodies[len(bodies) // 2]}   pire : {bodies[0]}")
    return 0 if ok == len(res) else 1


if __name__ == "__main__":
    raise SystemExit(main())
