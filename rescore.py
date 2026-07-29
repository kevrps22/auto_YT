"""
rescore.py — (Re)note la viralite des videos deja generees, avec Gemini.

Les videos creees avant l'ajout du score n'ont pas de note : l'app retombe alors
sur une heuristique grossiere. Ce script demande a Gemini une vraie note pour
toutes les videos de output/lib/ et l'ecrit dans leur meta.json.

Usage :
  python rescore.py          # ne note que celles qui n'ont pas de note
  python rescore.py --all    # renote TOUT (comparaison sur la meme echelle)
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from generate import _load_env, OUT_DIR  # noqa: E402

_load_env()

PROMPT = """Tu es un expert des YouTube Shorts viraux. Note le potentiel viral REEL de ce short.

TITRE : {title}
SCRIPT : {desc}

Note de 0 a 100. Sois SEVERE et DISCRIMINANT (evite les ex-aequo, la moyenne doit
tourner autour de 55, utilise toute l'echelle).

Note HAUT si : parle du spectateur lui-meme (ton corps, ton argent, ton quotidien),
provoque degout/peur/colere, detruit une croyance repandue, chiffre hallucinant,
sujet universel que tout le monde comprend en 1 seconde.

Note BAS si : abstrait ou lointain, objet/lieu que le spectateur ne verra jamais,
niche, deja vu mille fois, demande des connaissances prealables, tiede.

Reponds UNIQUEMENT en JSON : {{"virality": <entier>, "virality_reason": "<1 phrase courte>"}}"""


# Le quota gratuit est PAR MODELE : on utilise ici un autre modele que generate.py
# pour ne pas epuiser le quota de generation des videos.
MODELS = ["gemini-flash-lite-latest", "gemini-2.0-flash", "gemini-2.0-flash-lite",
          "gemini-flash-latest"]


def score(client, title, desc, model):
    r = client.models.generate_content(
        model=model,
        contents=PROMPT.format(title=title, desc=desc[:800]),
    )
    raw = r.text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(raw)


def main():
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        sys.exit("GEMINI_API_KEY absente du .env")
    from google import genai
    client = genai.Client(api_key=key)

    redo_all = "--all" in sys.argv
    folders = sorted((OUT_DIR / "lib").glob("*/"))
    done, mi = 0, 0
    for folder in folders:
        mf = folder / "meta.json"
        if not mf.exists():
            continue
        meta = json.loads(mf.read_text(encoding="utf-8"))
        if not redo_all and isinstance(meta.get("virality"), (int, float)):
            continue
        title = meta.get("title", "")
        while mi < len(MODELS):
            try:
                res = score(client, title, meta.get("description", ""), MODELS[mi])
                meta["virality"] = int(res["virality"])
                meta["virality_reason"] = res.get("virality_reason", "")
                mf.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                done += 1
                print(f"  {meta['virality']:3d}  {title[:55]}")
                time.sleep(4)                       # respecte la limite par minute
                break
            except Exception as e:
                err = str(e)
                if any(x in err for x in ("RESOURCE_EXHAUSTED", "429", "NOT_FOUND", "404")):
                    mi += 1                          # quota epuise / modele absent -> suivant
                    if mi < len(MODELS):
                        print(f"  ... bascule sur {MODELS[mi]}")
                        continue
                    print("  Tous les quotas du jour sont epuises. Relance demain.")
                else:
                    print(f"  ERR  {title[:45]} ({str(e)[:70]})")
                break
        if mi >= len(MODELS):
            break
    print(f"\n{done} video(s) notee(s). Rafraichis la galerie dans l'app.")


if __name__ == "__main__":
    main()
