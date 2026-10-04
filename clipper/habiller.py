"""Habille la VOIX d'un extrait avec un montage explicatif original.

    python clipper/habiller.py <id> --clip 7          (numero dans l'index out/<id>.json)
    python clipper/habiller.py <id> --clip 7 --replan (redemande le plan visuel)

On garde l'audio de l'invite, on jette son image, et on fabrique a la place un
montage : plans d'illustration et cartes typographiques, un par idee du discours.

Pourquoi : YouTube exclut des revenus « la remise en ligne de contenus de
createurs YouTube » et les « compilations sans ajout de contenu original ». Des
sous-titres sur l'image d'origine ne transforment rien ; un montage explicatif
construit sur mesure, si. Ca ne garantit pas l'acceptation, la revue est humaine.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import clipper as C  # noqa: E402

SORTIE = C.OUT_HABILLE
FOND = "0x0E1015"                    # fond des cartes typographiques
ACCENT = "0xFFB020"                  # meme orange que le mot prononce des sous-titres
# verbes et etats : presents dans les requetes, inutiles pour reconnaitre le sujet
ACTIONS = {"spinning", "driving", "pouring", "falling", "moving", "flowing", "working",
           "running", "glowing", "burning", "closeup", "close", "macro", "slow", "motion",
           "aerial", "view", "shot", "modern", "large", "small", "people", "person"}
# Mots trop banals pour prouver a eux seuls qu'un plan illustre le propos : ils
# reviennent dans un slug Pexels sur deux. « perfume-bottle-model » decrochait
# « atom model », « businessman-holding-post-it » decrochait « man touching forehead ».
# Ils comptent encore, mais un plan retenu doit avoir AU MOINS un mot fort.
FAIBLES = {"model", "double", "business", "businessman", "office", "table", "screen",
           "light", "lights", "background", "abstract", "digital", "holding", "touching",
           "walking", "sitting", "standing", "looking", "young", "white", "black", "blue",
           "hand", "hands", "woman", "girl", "city", "night", "time", "life"}
# Poses de studio et contenus promotionnels : le mot est bien dans le descriptif,
# mais l'image ne montre rien du propos.
BANNIS = {"posing", "poses", "fashion", "influencer", "advertisement", "logo",
          # un sujet francais illustre par Wall Street, le 04/10
          "flag", "flags", "american", "america", "usa", "washington", "capitol",
          "nyc", "manhattan"}
# Sous 1920 px de haut, le plan est agrandi pour remplir le cadre puis encore par le
# zoom : c'est exactement ce qui faisait « cheap » sur deux ou trois plans.
HAUTEUR_MIN = 1920
# Etalonnage commun a TOUTE la video, passe en une fois sur le fond assemble. Le
# faire plan par plan laissait chaque source avec son propre contraste.
ETALON = ("eq=contrast=1.10:saturation=1.04:gamma=0.98,"
          # teal-orange discret : ombres vers le bleu, hautes lumieres vers le chaud
          "colorbalance=rs=-0.025:bs=0.045:rh=0.035:bh=-0.03,"
          # rattrape la mollesse des sources reencodees, sans halo (0.5 seulement)
          "unsharp=5:5:0.5:5:5:0.0,"
          "vignette=PI/6")
CARTE_Y = 730                        # centre du bloc de texte des cartes
# Rien en dessous de cette ligne : sur Shorts, la colonne de boutons a droite et le
# titre en bas mangent le cadre. Une carte qui descend plus bas est remontee.
CARTE_BAS_MAX = 1280
FOND_HAUT = "0x1A2233"               # halo du degrade, au centre de la carte
FOND_BAS = "0x07090E"                # bords, presque noir
# eclat blanc de 1 a 2 images a chaque coupe : les changements de plan etaient secs
FLASH = ",fade=t=in:st=0:d=0.05:color=white,"
MUSIQUE = C.ROOT / "assets" / "music" / "bg.mp3"
VOL_MUSIQUE = 0.10                   # lit les silences sans jamais couvrir la voix
VOL_SFX = 0.55


# ------------------------------------------------------------------ Gemini
def _gemini(prompt: str, etiquette: str) -> str:
    """Un appel Gemini, en passant au modele suivant quand l'un est indisponible ou
    a epuise son quota gratuit (20 requetes par jour et par modele)."""
    from google import genai
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    for m in C.GEMINI_MODELS:
        try:
            r = client.models.generate_content(model=m, contents=prompt)
            print(f"[{etiquette}] modele {m}")
            return r.text
        except Exception as e:
            if any(x in str(e) for x in ("RESOURCE_EXHAUSTED", "429", "NOT_FOUND", "404",
                                         "UNAVAILABLE", "503", "INTERNAL", "500")):
                print(f"[{etiquette}] {m} indispo -> suivant")
                continue
            raise
    raise RuntimeError("Tous les modeles Gemini sont epuises.")


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFD", t.lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def _jetons(t: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", _norm(t))


# ------------------------------------------------------------------ segments de parole
SEG_MIN, SEG_MAX = 1.8, 4.5          # duree d'un segment, en secondes
# Au-dela, meme avec le mouvement de camera, un plan parait fige : un plan de 9,6 s
# couvrait deux phrases entieres le 04/10.
PLAN_MAX = 6.5


def segmenter(mots: list[dict], s: float, duree: float) -> list[dict]:
    """Decoupe la parole en segments de 2 a 4,5 s, coupes sur les fins de phrase et
    les pauses. Ce sont EUX qui fixent les instants du montage, et non Gemini : il
    inventait ses propres bornes, et ses cartes tombaient a cote de ce qui etait dit
    (« EUROPE DE L'OUEST » affiche pendant « la vraisemblance », le 04/10)."""
    brut, cur = [], []
    for i, w in enumerate(mots):
        cur.append(w)
        long = w["e"] - cur[0]["s"]
        nxt = mots[i + 1] if i + 1 < len(mots) else None
        fin_phrase = w["w"].rstrip().endswith((".", "?", "!", "…"))
        pause = nxt is not None and nxt["s"] - w["e"] > 0.35
        if nxt is None or long >= SEG_MAX or (long >= SEG_MIN and (fin_phrase or pause)):
            brut.append(cur)
            cur = []
    segs = []
    for k, ms in enumerate(brut):
        debut = 0.0 if k == 0 else round(ms[0]["s"] - s, 2)
        fin = round(brut[k + 1][0]["s"] - s, 2) if k + 1 < len(brut) else round(duree, 2)
        if segs and fin - debut < 1.2:       # bout de phrase isole : avec le precedent
            segs[-1]["mots"] += ms
            segs[-1]["fin"] = fin
            continue
        segs.append({"mots": list(ms), "debut": debut, "fin": fin})
    for k, sg in enumerate(segs, 1):
        sg["n"] = k
        sg["texte"] = " ".join(x["w"] for x in sg["mots"]).strip()
    return segs


# ------------------------------------------------------------------ plan, segment par segment
def planifier(segs: list[dict], titre: str) -> list[dict]:
    """Un seul appel : pour chaque segment, le texte corrige et ce qu'il faut
    montrer. Gemini ne choisit plus QUAND, seulement QUOI."""
    lignes = "\n".join(f'{sg["n"]}. [{sg["debut"]:.1f}-{sg["fin"]:.1f} s] {sg["texte"]}'
                       for sg in segs)
    prompt = (
        "Tu es monteur de videos explicatives verticales. Voici un extrait de "
        f"« {titre} », deja decoupe en segments numerotes, avec ce qui est DIT dans "
        "chacun.\n\n"
        "Pour CHAQUE segment, rends un objet JSON :\n"
        '- "n" : le numero du segment.\n'
        '- "texte" : le texte du segment, avec SEULEMENT les erreurs de transcription '
        "corrigees — des mots mal entendus (« la vraie semblance » -> « la "
        "vraisemblance », « 8000 mal par mois » -> « 8000 balles par mois »). Ne "
        "reformule rien, ne resume rien, garde l'oral. Si rien n'est faux, recopie.\n"
        '- "visuel" : "video", "carte" ou "suite".\n'
        '  * "carte" UNIQUEMENT si le segment enonce un chiffre, une date, un '
        "pourcentage ou un nom propre.\n"
        '  * "suite" garde l\'image du segment precedent quand l\'idee continue : une '
        "image doit tenir 4 a 8 secondes, pas changer a chaque phrase.\n"
        '  * "video" sinon. Jamais deux "carte" de suite.\n'
        '- "carte" : si visuel = carte, 1 a 4 mots ou un chiffre PRIS DANS CE QUI EST '
        "DIT dans ce segment, en MAJUSCULES ACCENTUEES (« -20 % D'EAU », « 2038 »).\n"
        '- "requete" : TOUJOURS, meme pour une carte : 2 a 4 mots ANGLAIS decrivant un '
        "OBJET, une MATIERE ou un PHENOMENE NATUREL concret qui illustre CE segment "
        "precis. Interdits : personnes qui font un geste, batiments officiels, "
        "drapeaux, villes, rues, bureaux, ecrans, tout ce qui designe un pays — une "
        "banque d'images renvoie ces lieux d'un autre pays que celui dont on parle.\n"
        '- "repli" : 1 a 3 mots PRIS DANS CE QUI EST DIT, l\'idee du segment, en '
        "MAJUSCULES ACCENTUEES. Affiche si aucune image ne convient.\n\n"
        "Le premier segment est l'accroche : son image doit etre la plus forte.\n"
        "Reponds UNIQUEMENT par le tableau JSON, un objet par segment, dans l'ordre.\n\n"
        f"SEGMENTS :\n{lignes}"
    )
    plan = C._json(_gemini(prompt, "plan"))
    par_n = {int(p.get("n", 0)): p for p in plan if isinstance(p, dict)}
    return [par_n.get(sg["n"], {}) for sg in segs]


def _dit(etiquette: str, texte: str) -> bool:
    """La carte affiche-t-elle quelque chose qui est reellement DIT ? Un chiffre
    ou un mot de trois lettres au moins, present dans le texte du segment."""
    dits = set(_jetons(texte))
    return any(j in dits for j in _jetons(etiquette) if len(j) >= 3 or j.isdigit())


VIDES = {"les", "des", "une", "est", "que", "qui", "pas", "pour", "dans", "avec", "sur",
         "mais", "donc", "alors", "cette", "ces", "son", "ses", "leur", "nous", "vous",
         "ils", "elle", "tout", "tous", "plus", "tres", "fait", "faut", "avoir", "etre",
         "comme", "aussi", "bien", "quand", "parce", "voila", "enfin"}


def _repli(texte: str) -> str:
    """A defaut de mieux : les deux mots les plus longs de la phrase."""
    mots = [m.strip(" ,.;:!?…«»\"'") for m in texte.split()]
    pleins = [m for m in mots if len(m) >= 5 and _norm(m) not in VIDES]
    garde = sorted(sorted(set(pleins), key=len, reverse=True)[:2], key=pleins.index)
    return " ".join(garde).upper() or "ÉCOUTEZ"


def corriger(segs: list[dict], plan: list[dict]) -> list[dict]:
    """Reporte le texte corrige sur les horodatages de Whisper, mot a mot. Les mots
    identiques gardent leur instant exact ; un mot corrige occupe le creneau de
    ceux qu'il remplace. Si la « correction » s'eloigne trop de ce qui a ete
    entendu, c'est que Gemini a reformule : on garde Whisper."""
    out: list[dict] = []
    for sg, p in zip(segs, plan):
        wh = sg["mots"]
        corr = (p.get("texte") or "").split()
        a, b = [_norm(w["w"]) for w in wh], [_norm(x) for x in corr]
        sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
        if not corr or sm.ratio() < 0.6:
            out += wh
            continue
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "equal":
                out += [{**wh[i1 + k], "w": corr[j1 + k]} for k in range(i2 - i1)]
            elif j2 > j1:                       # remplacement ou insertion
                if i2 > i1:
                    t0, t1 = wh[i1]["s"], wh[i2 - 1]["e"]
                else:
                    t0 = wh[i1 - 1]["e"] if i1 > 0 else wh[0]["s"]
                    t1 = wh[i1]["s"] if i1 < len(wh) else t0 + 0.15 * (j2 - j1)
                t1 = max(t1, t0 + 0.08 * (j2 - j1))
                poids = [max(1, len(corr[j])) for j in range(j1, j2)]
                t = t0
                for j, pd in zip(range(j1, j2), poids):
                    d = (t1 - t0) * pd / sum(poids)
                    out.append({"w": corr[j], "s": round(t, 2), "e": round(t + d, 2)})
                    t += d
    return out


def composer(segs: list[dict], plan: list[dict]) -> list[dict]:
    """Transforme le plan en scenes contigues, en faisant RESPECTER par le code ce
    que la consigne demande — Gemini ne le fait pas toujours (deux cartes de suite
    le 04/10, alors que c'etait interdit en toutes lettres)."""
    scenes: list[dict] = []
    for sg, p in zip(segs, plan):
        texte = (p.get("texte") or sg["texte"]).strip()
        visuel = (p.get("visuel") or "video").strip().lower()
        carte = (p.get("carte") or "").strip().upper()
        requete = (p.get("requete") or "").strip()
        repli = (p.get("repli") or "").strip().upper()
        if not repli or not _dit(repli, texte):
            repli = _repli(texte)
        if visuel == "carte" and not (carte and _dit(carte, texte)):
            visuel = "video"                    # carte sans rapport avec ce qui est dit
        if visuel == "carte" and not scenes and requete:
            visuel = "video"                    # une diapositive en premiere seconde : non
        if visuel == "carte" and scenes and scenes[-1]["type"] == "carte":
            visuel = "video"
        if visuel == "suite":
            prec = scenes[-1] if scenes else None
            # on ne prolonge qu'une image filmee, et jamais au-dela de PLAN_MAX ;
            # sinon le segment recoit sa propre image
            if prec and prec["type"] == "video" and sg["fin"] - prec["debut"] <= PLAN_MAX:
                prec["fin"] = sg["fin"]
                prec["texte"] += " " + texte
                continue
            visuel = "video"
        if visuel != "carte" and not requete:
            if scenes and scenes[-1]["type"] == "carte":
                scenes[-1]["fin"] = sg["fin"]
                continue
            visuel, carte = "carte", repli
        base = {"debut": sg["debut"], "fin": sg["fin"], "texte": texte,
                "requete": requete, "large": (p.get("large") or "").strip(), "repli": repli}
        if visuel == "carte":
            # La carte reste le temps de la lire ; le reste du segment est illustre
            # par une image du MEME segment, pour que la suivante tombe a l'heure.
            lu = lisible(carte)
            if requete and sg["fin"] - sg["debut"] - lu >= 1.0:
                coupe = round(sg["debut"] + lu, 2)
                scenes.append({**base, "type": "carte", "carte": carte, "fin": coupe})
                scenes.append({**base, "type": "video", "debut": coupe})
            else:
                scenes.append({**base, "type": "carte", "carte": carte})
        else:
            scenes.append({**base, "type": "video"})
    return scenes


# ------------------------------------------------------------------ images d'illustration
_pris: set[int] = set()


def _present(mot: str, toks: set[str]) -> bool:
    """Le mot, au pluriel pres, comme MOT ENTIER du descriptif. En sous-chaine,
    « atom » decrochait « perfume-atomizer » : le quantique illustre par un flacon
    de parfum. Le slug Pexels est deja une liste de mots separes par des tirets."""
    return mot in toks or f"{mot}s" in toks or (mot.endswith("s") and mot[:-1] in toks)


def _meilleur_fichier(v: dict) -> dict | None:
    """Le fichier vertical le plus defini d'une video Pexels, s'il tient la route.
    En dessous de HAUTEUR_MIN l'image est agrandie pour remplir le cadre, et le
    zoompan l'agrandit encore de 16 % : ca se voit immediatement."""
    verticaux = [f for f in v["video_files"]
                 if (f.get("height") or 0) >= (f.get("width") or 1)]
    if not verticaux:
        return None
    f = max(verticaux, key=lambda f: f.get("height") or 0)
    return f if (f.get("height") or 0) >= HAUTEUR_MIN else None


def _description(v: dict) -> str:
    """Le slug de l'URL Pexels, rendu lisible : c'est le seul descriptif disponible."""
    bout = v.get("url", "").rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"-?\d+$", "", bout).replace("-", " ").strip()


def candidats(requete: str, large: str = "", k: int = 8) -> list[dict]:
    """Jusqu'a k plans Pexels verticaux et nets, tires de la requete precise puis de
    la requete large. Une recherche precise echoue souvent — « dry rusty tap »
    ramenait une jetee rouillee et une mouette — et le juge doit avoir de vrais
    choix. Rien n'est telecharge ici : c'est lui qui tranchera."""
    vus: set[int] = set()
    out: list[dict] = []
    for q in (requete, large):
        if q and len(out) < k:
            out += [c for c in _pexels(q, vus) if len(out) < k]
    return out


def _pexels(requete: str, vus: set[int], k: int = 6) -> list[dict]:
    """Les meilleurs plans d'UNE requete. Le 4K natif d'abord, le Full HD ensuite."""
    cle = os.getenv("PEXELS_API_KEY")
    if not cle or not requete:
        return []
    mots = [m for m in re.findall(r"[a-z]+", requete.lower())
            if len(m) > 3 and m not in ACTIONS]
    forts = [m for m in mots if m not in FAIBLES]
    notes = []
    for taille in ("large", "medium"):
        try:
            r = requests.get("https://api.pexels.com/videos/search", timeout=30,
                             headers={"Authorization": cle},
                             params={"query": requete, "orientation": "portrait",
                                     "per_page": 15, "size": taille})
            vids = r.json().get("videos", [])
        except Exception:
            continue
        for v in vids:
            toks = set(re.findall(r"[a-z]+", v.get("url", "").lower()))
            if v["id"] in _pris or v["id"] in vus or toks & BANNIS:
                continue
            f = _meilleur_fichier(v)
            if not f:
                continue
            vus.add(v["id"])
            fort = sum(1 for m in forts if _present(m, toks))
            note = sum(1 for m in mots if _present(m, toks))
            notes.append({"id": v["id"], "desc": _description(v), "lien": f["link"],
                          "hauteur": f.get("height") or 0, "rang": (fort, note)})
        if sum(1 for n in notes if n["rang"][0]) >= k:
            break
    notes.sort(key=lambda n: (-n["rang"][0], -n["rang"][1], -n["hauteur"]))
    return notes[:k]


def juger(scenes: list[dict], pool: dict[int, list[dict]]) -> dict[int, int]:
    """Un seul appel pour toute la video : pour chaque scene, Gemini lit ce qui est
    dit et la description des plans proposes, et choisit — ou refuse tout. C'est le
    controle qui manquait : le filtre par mots-cles ne savait pas qu'un drapeau
    americain ne convient pas a une phrase sur la France (le 04/10)."""
    if not pool:
        return {}
    blocs = []
    for k, cands in pool.items():
        liste = "\n".join(f"   {j}. {c['desc']}" for j, c in enumerate(cands, 1))
        blocs.append(f"SCENE {k} — on entend : « {scenes[k]['texte']} »\n{liste}")
    prompt = (
        "Tu choisis les images d'une video explicative. Pour chaque scene, voici ce "
        "qui est DIT pendant qu'elle est a l'ecran, puis des plans video proposes par "
        "une banque d'images, decrits par leur titre.\n\n"
        "Choisis le plan qui ILLUSTRE LE MIEUX CE QUI EST DIT. Refuse avec 0 :\n"
        "- un plan dont le lieu ou les symboles contredisent le propos (drapeau, "
        "monument ou ville d'un autre pays que celui dont on parle) ;\n"
        "- des personnes qui posent, des mains sans contexte, des bureaux, des ecrans ;\n"
        "- un plan qui n'a qu'un MOT en commun avec la phrase, pas le sens.\n"
        "Mieux vaut 0 qu'un plan faux : la scene recevra un autre traitement.\n\n"
        'Reponds UNIQUEMENT par un tableau JSON : [{"scene": 0, "choix": 3}, ...]\n\n'
        + "\n\n".join(blocs)
    )
    try:
        rep = C._json(_gemini(prompt, "juge"))
    except Exception as e:                      # noqa: BLE001
        print(f"[juge] indisponible ({type(e).__name__}) : premier candidat retenu")
        return {k: 1 for k in pool}
    out = {}
    for r in rep if isinstance(rep, list) else []:
        try:
            out[int(r["scene"])] = int(r.get("choix", 0))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _telecharger(lien: str, dst: Path) -> bool:
    try:
        with requests.get(lien, stream=True, timeout=60) as d:
            d.raise_for_status()
            with dst.open("wb") as out:
                for bloc in d.iter_content(1 << 20):
                    out.write(bloc)
        return True
    except Exception:
        return False


def illustrer(scenes: list[dict], travail: Path) -> list[dict]:
    """Trouve, fait juger et telecharge l'image de chaque scene video, puis decide
    du sort des scenes restees sans image acceptable.

    Deux passes, parce que le repli a besoin de connaitre la suite : le 04/10, les
    premieres scenes refusees sont devenues sept cartes d'affilee faute d'image
    filmee AVANT elles — alors qu'il y en avait de bonnes juste APRES."""
    pool = {k: c for k, sc in enumerate(scenes)
            if sc["type"] == "video" and (c := candidats(sc["requete"], sc.get("large", "")))}
    choix = juger(scenes, pool)

    images: dict[int, dict] = {}                # 1re passe : ce qui a ete accepte
    for k, cands in pool.items():
        j = choix.get(k, 0)
        if not 1 <= j <= len(cands) or cands[j - 1]["id"] in _pris:
            continue
        c, dst = cands[j - 1], travail / f"src{k:02d}.mp4"
        if _telecharger(c["lien"], dst):
            _pris.add(c["id"])
            images[k] = {"fichier": dst, "hauteur": c["hauteur"], "desc": c["desc"]}

    final: list[dict] = []                      # 2e passe : le montage
    for k, sc in enumerate(scenes):
        if sc["type"] == "video" and k in images:
            final.append({**sc, **images[k]})
            continue
        if sc["type"] == "video":
            prec = final[-1] if final else None
            if prec and prec["type"] == "video" and sc["fin"] - prec["debut"] <= PLAN_MAX:
                prec["fin"] = sc["fin"]          # on prolonge l'image filmee precedente
                continue
            # sinon on reprend l'image filmee la plus proche, avant ou apres : le
            # cadrage alterne d'une scene a l'autre, ca se lit comme un recadrage
            proches = [i for i in images if i < k][-1:] or [i for i in images if i > k][:1]
            if proches:
                img = images[proches[0]]
                final.append({**sc, **img, "desc": img["desc"] + " (reprise)"})
                continue
            # Aucune image dans toute la video : une carte, jamais etiree au-dela de
            # ce qu'elle dit (l'aplat fige corrige le 22/09).
            sc = {**sc, "type": "carte", "carte": sc["repli"]}
        prec = final[-1] if final else None
        if prec and prec["type"] == "carte" and prec["carte"] == sc["carte"]:
            prec["fin"] = sc["fin"]
            continue
        final.append(sc)
    return final


# ------------------------------------------------------------------ rendu des scenes
def _decouper(texte: str, largeur: int = 12) -> str:
    lignes, ligne = [], ""
    for mot in texte.split():
        if len(ligne) + len(mot) + 1 > largeur and ligne:
            lignes.append(ligne)
            ligne = mot
        else:
            ligne = f"{ligne} {mot}".strip()
    lignes.append(ligne)
    return "\n".join(lignes[:3])


def lisible(texte: str) -> float:
    """Temps qu'il faut pour LIRE une carte. Au-dela, le mot est lu depuis longtemps
    et l'image ne bouge plus : c'est la que le spectateur decroche."""
    return min(2.6, max(1.4, 1.0 + 0.45 * max(1, len(texte.split()))))


def scene_carte(texte: str, duree: float, dst: Path, travail: Path, flash: bool = True) -> None:
    """Carte typographique : halo radial, lignes qui arrivent l'une apres l'autre,
    filet qui se trace, et l'ensemble qui se pose (zoom 106 % -> 100 %)."""
    lignes = _decouper(texte.upper()).splitlines()
    police = str(C.FONTS_DIR / "Montserrat-Black.ttf").replace("\\", "/").replace(":", r"\:")
    long_max = max(len(x) for x in lignes)
    taille = 132 if long_max <= 9 else (104 if long_max <= 13 else 86)
    inter = int(taille * 1.20)
    # centres de chaque ligne, bloc centre sur CARTE_Y, puis remonte si le filet
    # descendait dans la zone masquee par l'interface Shorts
    centres = [CARTE_Y - (len(lignes) - 1) * inter / 2 + i * inter for i in range(len(lignes))]
    filet = centres[-1] + taille * 0.62 + 46
    if filet + 12 > CARTE_BAS_MAX:
        ecart = filet + 12 - CARTE_BAS_MAX
        centres = [c - ecart for c in centres]
        filet -= ecart

    vf = []
    for i, ligne in enumerate(lignes):
        f = travail / f"carte_{abs(hash(texte)) % 99999}_{i}.txt"
        f.write_text(ligne, encoding="utf-8")   # textfile : zero probleme d'echappement
        chemin = str(f).replace("\\", "/").replace(":", r"\:")
        # les lignes arrivent l'une apres l'autre, au rythme ou on les lit
        st = 0.08 + 0.13 * i
        # derniere ligne d'un bloc de plusieurs : en orange, c'est elle qu'on retient
        couleur = ACCENT if (len(lignes) > 1 and i == len(lignes) - 1) else "white"
        vf.append(f"drawtext=fontfile='{police}':textfile='{chemin}':fontcolor={couleur}:"
                  f"fontsize={taille}:x=(w-text_w)/2:y={int(centres[i])}-text_h/2:"
                  f"alpha='min(1,max(0,(t-{st:.2f})*10))'")
    # le filet se trace depuis le centre une fois le texte pose
    st_filet = 0.08 + 0.13 * len(lignes)
    trace = f"min(300,1100*max(0,t-{st_filet:.2f}))"
    vf.append(f"drawbox=x='(iw-{trace})/2':y={int(filet)}:w='{trace}':h=9:"
              f"color={ACCENT}@0.95:t=fill")
    # grain leger : sans lui, le degrade sombre fait des cercles de banding a l'encodage
    # pas de vignettage ici : celui de l'etalonnage global s'applique aussi aux cartes
    vf.append("noise=alls=4:allf=t")
    # la carte se POSE au lieu de deriver : 106 % ramenes a 100 % en 1 s
    vf.append(f"zoompan=z='max(1.06-0.0022*on,1.0)':d=1:x='iw/2-(iw/zoom/2)':"
              f"y='ih/2-(ih/zoom/2)':s={C.W}x{C.H}:fps=30")
    vf.append(f"fade=t=in:st=0:d=0.12{FLASH if flash else ','}setsar=1")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    f"gradients=s={C.W}x{C.H}:c0={FOND_HAUT}:c1={FOND_BAS}:type=radial:"
                    f"x0=540:y0={CARTE_Y}:speed=0:r=30:d={duree:.2f}",
                    "-t", f"{duree:.2f}", "-vf", ",".join(vf),
                    "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                    "-pix_fmt", "yuv420p", str(dst)], check=True, capture_output=True)


def scene_video(source: Path, duree: float, dst: Path, sens: int, flash: bool = True) -> None:
    """Plan d'illustration recadre en vertical, avec un lent mouvement de camera."""
    # mouvement plus franc : a 0,0009 par image, un plan de 4 s paraissait fige
    z = ("min(1.00+0.0016*on,1.16)" if sens else "max(1.16-0.0016*on,1.00)")
    vf = (f"scale={int(C.W * 1.2)}:{int(C.H * 1.2)}:force_original_aspect_ratio=increase,"
          f"crop={int(C.W * 1.2)}:{int(C.H * 1.2)},"
          f"zoompan=z='{z}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
          # plus d'etalonnage ici : il est passe en une fois sur le montage entier
          f"s={C.W}x{C.H}:fps=30,"
          f"fade=t=in:st=0:d=0.12{FLASH if flash else ','}setsar=1")
    subprocess.run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(source), "-t", f"{duree:.2f}",
                    "-vf", vf, "-an", "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                    "-pix_fmt", "yuv420p", str(dst)], check=True, capture_output=True)


# ------------------------------------------------------------------ effets sonores
def piste_sfx(impacts: list[tuple[float, str]], duree: float, dst: Path) -> None:
    """Fabrique la piste d'effets, sans aucun fichier son a fournir : un impact grave
    sur les cartes, un souffle sur les plans filmes. Tout est synthetise ici."""
    import numpy as np
    import wave

    sr = 48000
    piste = np.zeros(int((duree + 1.0) * sr), dtype=np.float32)
    for t, genre in impacts:
        i = int(t * sr)
        if genre == "boom":                       # sinus descendant 110 -> 45 Hz
            n = int(0.45 * sr)
            x = np.arange(n) / sr
            f = 110 * np.exp(-x * 3.2) + 45
            son = np.sin(2 * np.pi * f * x) * np.exp(-x * 6.0) * 0.6
        elif genre == "clic":                     # petit pop sur l'apparition d'un mot
            n = int(0.05 * sr)
            x = np.arange(n) / sr
            son = (np.sin(2 * np.pi * 1800 * x) * np.exp(-x * 90.0) * 0.10).astype(np.float32)
        else:                                     # souffle : bruit filtre, montee-descente
            n = int(0.35 * sr)
            x = np.arange(n) / sr
            bruit = np.random.default_rng(i).standard_normal(n).astype(np.float32)
            bruit = np.diff(bruit, prepend=0.0)    # derivation = passe-haut grossier
            env = np.minimum(x / 0.12, 1.0) * np.exp(-np.maximum(x - 0.12, 0) * 9.0)
            son = bruit * env * 0.12
        fin = min(len(piste), i + len(son))
        piste[i:fin] += son[: fin - i]
    piste = np.clip(piste, -1.0, 1.0)
    with wave.open(str(dst), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((piste * 32767).astype("<i2").tobytes())


def _ecrire_wav(piste, dst: Path, sr: int = 48000) -> None:
    import numpy as np
    import wave
    with wave.open(str(dst), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(piste, -1, 1) * 32767).astype("<i2").tobytes())


def piste_ambiance(plans: list[tuple[float, float, Path]], duree: float, dst: Path) -> None:
    """Son d'ambiance des plans d'illustration : beaucoup de videos Pexels portent leur
    propre son (circulation, vent, machines). On le reprend tres bas, adouci et fondu."""
    import numpy as np

    sr = 48000
    piste = np.zeros(int((duree + 1.0) * sr), dtype=np.float32)
    for t, d, source in plans:
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-t", f"{d + 0.5:.2f}",
                            "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                           capture_output=True)
        if len(r.stdout) < sr:                    # clip muet : rien a reprendre
            continue
        son = np.frombuffer(r.stdout, dtype="<i2").astype(np.float32) / 32768
        son = np.convolve(son, np.ones(6, np.float32) / 6, mode="same")   # adoucit les aigus
        n = min(len(son), int(d * sr))
        env = np.ones(n, dtype=np.float32)
        f = min(int(0.4 * sr), n // 2)
        env[:f], env[n - f:] = np.linspace(0, 1, f), np.linspace(1, 0, f)
        i = int(t * sr)
        fin = min(len(piste), i + n)
        piste[i:fin] += son[: fin - i] * env[: fin - i] * 0.22
    _ecrire_wav(piste, dst)


def piste_basse(duree: float, dst: Path) -> None:
    """Ligne de basse synthetisee : une pulsation grave toutes les 1,2 s sur un bourdon.
    C'est ce qui tient l'oreille entre deux phrases, sans melodie qui distrairait."""
    import numpy as np

    sr = 48000
    x = np.arange(int((duree + 1.0) * sr)) / sr
    bourdon = np.sin(2 * np.pi * 41 * x) * 0.05
    piste = bourdon.astype(np.float32)
    for k in range(int(duree / 1.2) + 1):
        i = int(k * 1.2 * sr)
        n = int(0.9 * sr)
        u = np.arange(n) / sr
        note = np.sin(2 * np.pi * 55 * u) * np.exp(-u * 3.5) * 0.16
        fin = min(len(piste), i + n)
        piste[i:fin] += note[: fin - i].astype(np.float32)
    _ecrire_wav(piste, dst)


# ------------------------------------------------------------------ assemblage
def habiller(vid: str, numero: int, replan: bool = False) -> Path:
    src = C.SRC / f"{vid}.mp4"
    index = json.loads((C.OUT / f"{vid}.json").read_text(encoding="utf-8"))
    choix = json.loads(src.with_suffix(".clips.json").read_text(encoding="utf-8"))
    clip = choix[numero - 1]
    mots = C.recoller(json.loads(src.with_suffix(".words.json").read_text(encoding="utf-8")))
    phr = C.phrases(mots)
    s, e = C.caler(clip, mots, phr, C.scenes(src))
    duree = e - s
    print(f"[extrait] {clip['titre']} · {s:.1f}-{e:.1f}s ({duree:.0f}s)")

    segs = segmenter(C.mots_dans(mots, s, e), s, duree)
    # Le plan est mis en cache avec les segments qu'il decrit : si le decoupage
    # change, l'ancien plan ne correspond plus a rien et il est refait.
    cache = src.with_suffix(f".plan{numero}.v2.json")
    plan = None
    if cache.exists() and not replan:
        lu = json.loads(cache.read_text(encoding="utf-8"))
        if lu.get("segments") == [sg["texte"] for sg in segs]:
            plan = lu["plan"]
            print("[plan] relu depuis le cache")
    if plan is None:
        plan = planifier(segs, index.get("titre", vid))
        cache.write_text(json.dumps({"segments": [sg["texte"] for sg in segs], "plan": plan},
                                    ensure_ascii=False, indent=1), encoding="utf-8")
    mots_corr = corriger(segs, plan)
    scenes = composer(segs, plan)

    SORTIE.mkdir(exist_ok=True)
    base = SORTIE / f"{vid}_{numero}_{C.slug(clip['titre'])}_explique"
    with tempfile.TemporaryDirectory() as tmp:
        travail = Path(tmp)
        scenes = illustrer(scenes, travail)
        if not scenes:                         # pas de parole exploitable
            scenes = [{"type": "carte", "carte": (clip.get("accroche") or clip["titre"]).upper(),
                       "debut": 0.0, "fin": round(duree, 2)}]
        segments, impacts, ambiances = [], [], []
        for i, sc in enumerate(scenes):
            d = sc["fin"] - sc["debut"]
            seg = travail / f"seg{i:02d}.mp4"
            if sc["type"] == "video":
                scene_video(sc["fichier"], d, seg, i % 2, flash=bool(segments))
                impacts.append((sc["debut"], "souffle"))
                ambiances.append((sc["debut"], d, sc["fichier"]))
                print(f"  {sc['debut']:5.1f}s  video  {sc['hauteur']}p  {sc['desc'][:50]}")
            else:
                scene_carte(sc["carte"], d, seg, travail, flash=bool(segments))
                impacts.append((sc["debut"], "boom"))
                print(f"  {sc['debut']:5.1f}s  carte  {sc['carte']}")
            segments.append((seg, d))

        liste = travail / "liste.txt"
        liste.write_text("".join(f"file '{p.as_posix()}'\n" for p, _ in segments), encoding="utf-8")
        fond = travail / "fond.mp4"
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(liste),
                        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                        "-r", "30", "-pix_fmt", "yuv420p", str(fond)],
                       check=True, capture_output=True)

        ass = base.with_suffix(".ass")
        C.sous_titres(mots_corr, s, e, ass, clip.get("accroche", ""))
        ass_ff = str(ass).replace("\\", "/").replace(":", r"\:")
        fonts_ff = str(C.FONTS_DIR).replace("\\", "/").replace(":", r"\:")
        dst = base.with_suffix(".mp4")
        # un clic discret a chaque nouveau groupe de mots : l'oreille suit la lecture
        for g in C.grouper(C.mots_dans(mots_corr, s, e)):
            impacts.append((max(0.0, g[0]["s"] - s), "clic"))
        sfx, amb, basse = travail / "sfx.wav", travail / "amb.wav", travail / "basse.wav"
        piste_sfx(impacts, duree, sfx)
        piste_ambiance(ambiances, duree, amb)
        piste_basse(duree, basse)
        # Trois pistes : la voix intacte, les impacts, et la musique qui s'efface sous
        # la voix (sidechaincompress). Aucun filtre sur la voix : ils la faisaient
        # gresiller sur l'ancien generateur.
        # asplit : un label ffmpeg ne se consomme qu'UNE fois, or la voix sert deux
        # fois, au mixage et comme declencheur de l'attenuation de la musique.
        melange = (f"[1:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                   f"asplit=2[voix][voix_duck];"
                   f"[2:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                   f"volume={VOL_SFX}[sfx];"
                   f"[5:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                   f"volume={VOL_MUSIQUE},afade=t=in:st=0:d=0.8,"
                   f"afade=t=out:st={max(0.0, duree - 1.2):.2f}:d=1.2[mus];"
                   f"[3:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[amb];"
                   f"[4:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[bas];"
                   f"[mus][bas]amix=inputs=2:normalize=0[lit];"
                   f"[lit][voix_duck]sidechaincompress=threshold=0.02:ratio=12:attack=15:"
                   f"release=350[litd];"
                   f"[voix][sfx][amb][litd]amix=inputs=4:duration=first:normalize=0,"
                   # YouTube ramene les videos trop fortes a -14 LUFS mais ne REMONTE
                   # jamais une video trop faible : sans ca, la source Thinkerview
                   # (-21 LUFS) sortait 7 dB sous les autres Shorts, ce qui s'entend
                   # sur un telephone. loudnorm travaille en 192 kHz, d'ou le aresample.
                   f"loudnorm=I=-14:TP=-1.0:LRA=11,aresample=48000[a]")
        # barre de progression : le spectateur voit la fin approcher et reste
        barre = (f"drawbox=x=0:y=0:w='iw*t/{duree:.2f}':h=7:color=0xFFB020@0.85:t=fill")
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(fond), "-ss", f"{s:.2f}", "-t", f"{duree:.2f}",
             "-i", str(src), "-i", str(sfx), "-i", str(amb), "-i", str(basse),
             "-stream_loop", "-1", "-i", str(MUSIQUE),
             "-filter_complex",
             # etalonnage AVANT les sous-titres : il unifie les plans et les cartes,
             # mais laisse intacts le blanc et l'orange des sous-titres, qui sont les
             # deux seuls reperes fixes de la video
             f"[0:v]{ETALON},subtitles='{ass_ff}':fontsdir='{fonts_ff}',{barre}[v];{melange}",
             "-map", "[v]", "-map", "[a]", "-shortest",
             # "medium" sur un PC, "veryfast" sur les quatre petits coeurs du Pi :
             # le gain de qualite ne vaut pas une heure d'encodage de plus
             "-c:v", "libx264", "-crf", "18",
             "-preset", os.environ.get("CLIPPER_X264_PRESET", "medium"), "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dst)],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode:
            raise RuntimeError(r.stderr[-1200:])
        ass.unlink(missing_ok=True)
    print(f"-> {dst}")
    return dst


# ------------------------------------------------------------------ incrustation
# L'invite reste a l'ecran ; des plans viennent par moments par-dessus son image,
# puis repartent. Le montage integral (habiller) retirait l'humain : 48 s d'images
# de banque sur une voix, ca se lit comme une video faite par une IA (04/10).
INCRUSTE_DEPUIS = 10.0       # les dix premieres secondes restent sur l'invite
INCRUSTE_MAX = 3.5           # une incrustation ne dure jamais plus
CARRE_Y = C.FG_Y             # le carre 1080x1080 ou l'invite apparait dans le clip brut


def incruster(vid: str, numero: int, replan: bool = False) -> Path:
    """Pose des plans d'illustration sur le clip BRUT, dans le carre de l'invite :
    l'accroche en haut et les sous-titres en bas restent toujours visibles.

    Si aucune image ne convient a une phrase, on ne met rien : l'invite reste. C'est
    ce qui manquait au montage integral, oblige de remplir chaque seconde."""
    src = C.SRC / f"{vid}.mp4"
    index = json.loads((C.OUT / f"{vid}.json").read_text(encoding="utf-8"))
    brut = C.OUT / index["clips"][numero - 1]["fichier"]
    choix = json.loads(src.with_suffix(".clips.json").read_text(encoding="utf-8"))
    clip = choix[numero - 1]
    mots = C.recoller(json.loads(src.with_suffix(".words.json").read_text(encoding="utf-8")))
    s, e = C.caler(clip, mots, C.phrases(mots), C.scenes(src))
    duree = e - s
    print(f"[incrustation] {clip['titre']} · {duree:.0f}s")

    segs = segmenter(C.mots_dans(mots, s, e), s, duree)
    cache = src.with_suffix(f".plan{numero}.v2.json")
    plan = None
    if cache.exists() and not replan:
        lu = json.loads(cache.read_text(encoding="utf-8"))
        if lu.get("segments") == [sg["texte"] for sg in segs]:
            plan = lu["plan"]
            print("[plan] relu depuis le cache")
    if plan is None:
        plan = planifier(segs, index.get("titre", vid))
        cache.write_text(json.dumps({"segments": [sg["texte"] for sg in segs], "plan": plan},
                                    ensure_ascii=False, indent=1), encoding="utf-8")

    # Une incrustation par segment au plus, jamais deux segments de suite : le
    # retour a l'invite entre deux plans est ce qui garde la video humaine.
    moments, dernier = [], -2
    for k, (sg, p) in enumerate(zip(segs, plan)):
        if sg["debut"] < INCRUSTE_DEPUIS or k == dernier + 1:
            continue
        texte = (p.get("texte") or sg["texte"]).strip()
        a, b = sg["debut"], min(sg["fin"], sg["debut"] + INCRUSTE_MAX)
        if b - a < 1.5:
            continue
        carte = (p.get("carte") or "").strip().upper()
        if p.get("visuel") == "carte" and carte and _dit(carte, texte):
            moments.append({"type": "carte", "carte": carte, "texte": texte,
                            "debut": a, "fin": min(b, a + lisible(carte))})
        elif p.get("requete"):
            moments.append({"type": "video", "requete": p["requete"], "texte": texte,
                            "large": p.get("large", ""), "debut": a, "fin": b})
        else:
            continue
        dernier = k

    SORTIE.mkdir(exist_ok=True)
    dst = SORTIE / f"{Path(brut).stem}_incruste.mp4"
    with tempfile.TemporaryDirectory() as tmp:
        travail = Path(tmp)
        pool = {i: c for i, m in enumerate(moments)
                if m["type"] == "video" and (c := candidats(m["requete"], m["large"]))}
        verdict = juger(moments, pool)
        poses = []                              # (moment, fichier, decalage du recadrage)
        for i, m in enumerate(moments):
            d = m["fin"] - m["debut"]
            seg = travail / f"inc{i:02d}.mp4"
            if m["type"] == "carte":
                scene_carte(m["carte"], d, seg, travail)
                # la carte est dessinee centree sur CARTE_Y : on garde le carre autour
                poses.append((m, seg, CARTE_Y - 540))
                print(f"  {m['debut']:5.1f}s  carte  {m['carte']}")
                continue
            j, cands = verdict.get(i, 0), pool.get(i, [])
            if not 1 <= j <= len(cands) or cands[j - 1]["id"] in _pris:
                print(f"  {m['debut']:5.1f}s  (l'invite reste : aucune image ne convient)")
                continue
            c, source = cands[j - 1], travail / f"src{i:02d}.mp4"
            if not _telecharger(c["lien"], source):
                continue
            _pris.add(c["id"])
            scene_video(source, d, seg, i % 2)
            poses.append((m, seg, (C.H - 1080) // 2))
            print(f"  {m['debut']:5.1f}s  video  {c['hauteur']}p  {c['desc'][:50]}")

        cmd = ["ffmpeg", "-y", "-i", str(brut)]
        fc, base = [], "0:v"
        for k, (m, seg, oy) in enumerate(poses, 1):
            cmd += ["-i", str(seg)]
            a, b = m["debut"], m["fin"]
            fc.append(f"[{k}:v]setpts=PTS-STARTPTS+{a:.3f}/TB,crop=1080:1080:0:{oy},"
                      f"{ETALON}[i{k}]")
            fc.append(f"[{base}][i{k}]overlay=0:{CARRE_Y}:enable='between(t,{a:.3f},{b:.3f})'"
                      f":eof_action=pass[v{k}]")
            base = f"v{k}"
        # Le son des sources Thinkerview sort a -21 LUFS : ramene a -14, comme
        # l'habillage, faute de quoi la video passe 7 dB sous les autres Shorts.
        fc.append("[0:a]loudnorm=I=-14:TP=-1.0:LRA=11,aresample=48000[a]")
        sortie_v = f"[{base}]" if poses else "0:v"
        r = subprocess.run(cmd + ["-filter_complex", ";".join(fc), "-map", sortie_v, "-map", "[a]",
                                  "-c:v", "libx264", "-crf", "18",
                                  "-preset", os.environ.get("CLIPPER_X264_PRESET", "medium"),
                                  "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                                  "-movflags", "+faststart", str(dst)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode:
            raise RuntimeError(r.stderr[-1200:])
    couvert = sum(m["fin"] - m["debut"] for m, _, _ in poses)
    print(f"-> {dst}  ({len(poses)} incrustations, {100 * couvert / duree:.0f} % du temps)")
    return dst


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    vid = sys.argv[1]
    numero = int(sys.argv[sys.argv.index("--clip") + 1]) if "--clip" in sys.argv else 1
    if "--incruste" in sys.argv:
        incruster(vid, numero, "--replan" in sys.argv)
    else:
        habiller(vid, numero, "--replan" in sys.argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
