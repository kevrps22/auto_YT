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

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import clipper as C  # noqa: E402

SORTIE = C.OUT_HABILLE
FOND = "0x0E1015"                    # fond des cartes typographiques
ACCENT = "0xFFB020"                  # meme orange que le mot prononce des sous-titres
SCENE_MIN, SCENE_MAX = 2.2, 6.0
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
BANNIS = {"posing", "poses", "fashion", "influencer", "advertisement", "logo"}
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


# ------------------------------------------------------------------ plan visuel
def plan_visuel(texte: str, duree: float, titre: str) -> list[dict]:
    """Gemini decoupe le discours en scenes et dit quoi montrer sur chacune."""
    from google import genai
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    prompt = (
        "Tu es monteur de videos explicatives. Voici la transcription horodatee d'un "
        f"extrait de {duree:.0f} secondes, tire de « {titre} ».\n\n"
        "Fabrique le PLAN VISUEL qui accompagne cette voix : une scene par idee, "
        f"entre {SCENE_MIN:.0f} et {SCENE_MAX:.0f} secondes, sans trou ni chevauchement, "
        f"de 0.0 a {duree:.1f}.\n\n"
        "Deux types de scenes :\n"
        '- "video" : une image d\'illustration filmable. Donne `requete`, 2 a 4 mots '
        "ANGLAIS tres concrets : un OBJET, une MATIERE ou un LIEU. Jamais d'abstraction, "
        "jamais de metaphore, jamais de mot de cadrage, et JAMAIS quelqu'un en train de "
        "faire un geste : « man touching his forehead » ne ramene que des poses de "
        "studio sans rapport, alors qu'un objet ramene l'objet. Donne AUSSI `texte`, le "
        "mot cle francais correspondant : il s'affiche si aucune image n'est trouvee.\n"
        '- "carte" : un mot ou un chiffre cle affiche en grand. Donne `texte`, 1 a 4 mots '
        "FRANCAIS en majuscules. Sers-t'en pour les chiffres, les definitions et les "
        "idees qu'aucune image ne montre.\n\n"
        "La MOITIE au moins des scenes doit etre de type video : une suite de cartes "
        "donne un diaporama, pas une video explicative. Jamais plus d'une carte de "
        "suite. Les scenes suivent ce qui est DIT au meme moment.\n\n"
        "Les `texte` s'affichent a l'ecran : francais en MAJUSCULES ACCENTUEES "
        "(ecris ÉNERGIE et non ENERGIE, SYSTÈME et non SYSTEME).\n"
        "Reponds UNIQUEMENT par un tableau JSON :\n"
        '[{"debut": 0.0, "fin": 3.4, "type": "video", "requete": "power plant turbine", '
        '"texte": ""}]\n\n'
        f"TRANSCRIPTION :\n{texte}"
    )
    for m in C.GEMINI_MODELS:
        try:
            r = client.models.generate_content(model=m, contents=prompt)
            print(f"[plan] modele {m}")
            return C._json(r.text)
        except Exception as e:
            if any(x in str(e) for x in ("RESOURCE_EXHAUSTED", "429", "NOT_FOUND", "404",
                                         "UNAVAILABLE", "503", "INTERNAL", "500")):
                print(f"[plan] {m} indispo -> suivant")
                continue
            raise
    raise RuntimeError("Tous les modeles Gemini sont epuises.")


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


def chercher_clip(requete: str, dst: Path) -> int:
    """Telecharge un plan vertical Pexels pertinent ET net. Renvoie sa hauteur en
    pixels, 0 si rien ne convient.

    Deux passes : d'abord les sources 4K natives, puis le Full HD. Pertinence
    d'abord, definition ensuite : un plan net hors sujet reste hors sujet."""
    cle = os.getenv("PEXELS_API_KEY")
    if not cle or not requete:
        return 0
    # On note les candidats sur les NOMS de la requete. Exiger n'importe quel mot
    # laissait passer un disque vinyle pour « electricity meter spinning » (via
    # "spinning") ; exiger le mot le plus long rejetait presque toutes les recherches.
    mots = [m for m in re.findall(r"[a-z]+", requete.lower())
            if len(m) > 3 and m not in ACTIONS]
    forts = [m for m in mots if m not in FAIBLES]
    notes: list[tuple[int, int, int, dict, dict]] = []
    vus: set[int] = set()
    for taille in ("large", "medium"):             # large = 4K mini chez Pexels
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
            note = sum(1 for m in mots if _present(m, toks))
            fort = sum(1 for m in forts if _present(m, toks))
            if v["id"] in _pris or v["id"] in vus or not fort or toks & BANNIS:
                continue
            f = _meilleur_fichier(v)
            if not f:
                continue
            vus.add(v["id"])
            notes.append((fort, note, f.get("height") or 0, v, f))
        if notes:                                  # du 4K pertinent : inutile de descendre
            break
    # D'abord les plans qui recoupent DEUX mots de la requete : un seul mot commun ne
    # prouve rien sur un slug qui en aligne dix. S'il n'y en a aucun, un mot fort
    # suffit ; sinon on rend la main et la scene devient une carte, ce qui vaut
    # toujours mieux qu'un plan hors sujet.
    retenus = [n for n in notes if n[1] >= 2] or notes
    for _, _, hauteur, v, f in sorted(retenus, key=lambda x: (-x[1], -x[0], -x[2])):
        try:
            with requests.get(f["link"], stream=True, timeout=60) as d:
                d.raise_for_status()
                with dst.open("wb") as out:
                    for bloc in d.iter_content(1 << 20):
                        out.write(bloc)
        except Exception:
            continue
        _pris.add(v["id"])
        return hauteur
    return 0


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
def rythmer(plan: list[dict], duree: float) -> list[dict]:
    """Donne sa duree a chaque scene. Une carte ne garde que son temps de lecture et
    rend le reste a la scene suivante, qui demarre donc plus tot : la carte tombe
    toujours sur le mot qui la declenche, mais ne s'attarde plus apres."""
    scenes, t = [], 0.0
    for p in plan:
        fin = min(float(p.get("fin", t + 4)), duree)
        d = max(1.2, fin - t)
        scenes.append({**p, "d": d})
        t += d
    for i, p in enumerate(scenes):
        if p.get("type") != "carte" or i + 1 >= len(scenes):
            continue
        garde = min(p["d"], lisible(p.get("texte") or ""))
        scenes[i + 1]["d"] += p["d"] - garde
        p["d"] = garde
    return scenes


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

    dedans = [p for p in phr if p["e"] > s and p["s"] < e]
    texte = "\n".join(f"[{max(0, p['s'] - s):.1f}-{min(duree, p['e'] - s):.1f}] {p['t']}"
                      for p in dedans)
    cache = src.with_suffix(f".plan{numero}.json")
    if cache.exists() and not replan:
        plan = json.loads(cache.read_text(encoding="utf-8"))
        print("[plan] relu depuis le cache")
    else:
        plan = plan_visuel(texte, duree, index.get("titre", vid))
        cache.write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")

    SORTIE.mkdir(exist_ok=True)
    base = SORTIE / f"{vid}_{numero}_{C.slug(clip['titre'])}_explique"
    with tempfile.TemporaryDirectory() as tmp:
        travail = Path(tmp)
        plan = rythmer(plan, duree)
        segments, impacts, ambiances, t = [], [], [], 0.0
        for i, sc in enumerate(plan):
            d = sc["d"]
            if t >= duree - 0.3:
                break
            seg = travail / f"seg{i:02d}.mp4"
            fait = False
            if sc.get("type") == "video":
                brut = travail / f"src{i:02d}.mp4"
                hauteur = chercher_clip(sc.get("requete", ""), brut)
                if hauteur:
                    scene_video(brut, d, seg, i % 2, flash=bool(segments))
                    fait = True
                    impacts.append((t, "souffle"))
                    ambiances.append((t, d, brut))
                    print(f"  {t:5.1f}s  video  {hauteur}p  {sc.get('requete')}")
            if not fait:                       # pas d'image trouvee -> carte de repli
                # jamais la requete anglaise a l'ecran : elle n'est qu'une recherche
                txt = sc.get("texte") or clip["titre"].split(":")[0]
                # une carte de repli dure le temps qu'on la lit, comme les autres :
                # ici seulement, car on ne sait qu'a cet instant que Pexels a echoue
                garde = min(d, lisible(txt))
                if garde < d and i + 1 < len(plan):
                    plan[i + 1]["d"] += d - garde
                    d = garde
                scene_carte(txt, d, seg, travail, flash=bool(segments))
                impacts.append((t, "boom"))
                print(f"  {t:5.1f}s  carte  {txt}")
            segments.append((seg, d))
            t += d
        if t < duree:                          # la voix ne doit jamais tomber dans le vide
            seg = travail / "segfin.mp4"
            scene_carte(index.get("chaine", ""), duree - t, seg, travail)
            segments.append((seg, duree - t))
            impacts.append((t, "boom"))

        liste = travail / "liste.txt"
        liste.write_text("".join(f"file '{p.as_posix()}'\n" for p, _ in segments), encoding="utf-8")
        fond = travail / "fond.mp4"
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(liste),
                        "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                        "-r", "30", "-pix_fmt", "yuv420p", str(fond)],
                       check=True, capture_output=True)

        ass = base.with_suffix(".ass")
        C.sous_titres(mots, s, e, ass, clip.get("accroche", ""))
        ass_ff = str(ass).replace("\\", "/").replace(":", r"\:")
        fonts_ff = str(C.FONTS_DIR).replace("\\", "/").replace(":", r"\:")
        dst = base.with_suffix(".mp4")
        # un clic discret a chaque nouveau groupe de mots : l'oreille suit la lecture
        for g in C.grouper(C.mots_dans(mots, s, e)):
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


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    vid = sys.argv[1]
    numero = int(sys.argv[sys.argv.index("--clip") + 1]) if "--clip" in sys.argv else 1
    habiller(vid, numero, "--replan" in sys.argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
