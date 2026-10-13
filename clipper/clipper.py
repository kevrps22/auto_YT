"""Decoupe une video longue en Shorts, facon Opus Clip.

    python clipper/transcribe.py media/src/<id>.mp4       1. transcription (une fois)
    python clipper/clipper.py media/src/<id>.mp4 --n 8     2. choix + rendu
        --titre "..."   titre de la video source (aide Gemini)
        --rechoisir     redemande le choix des moments a Gemini

Etapes : changements de scene (ffmpeg) + transcription mot a mot -> Gemini choisit
les moments -> calage image ET phrase -> rendu vertical avec sous-titres.

Ce qu'on a appris en comparant avec 11 clips Opus Clip de la meme video :
- Opus coupe sur les CHANGEMENTS D'IMAGE : 13 de ses 20 frontieres tombent a moins
  de 0,6 s d'un changement de scene. Chaque clip = un post, de son apparition a
  l'arrivee du suivant. Mais il ignore la parole et commence souvent en plein mot.
  On cumule les deux : ouverture sur l'apparition de l'image, fin sur un point final.
- Accroche en encadre blanc, uniquement pendant les premieres secondes.
- Sous-titres en casse normale, mot prononce en couleur, apparition animee.
- Il garde les 60 i/s de la source.
- Il retient aussi le sponsor et la lecture des dons : on les exclut.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# Le plus capable en tete. Chaque modele a son propre quota gratuit (20 req/jour),
# donc allonger la liste augmente le nombre de videos possibles par jour.
GEMINI_MODELS = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash",
                 "gemini-3.5-flash", "gemini-3-flash-preview", "gemini-flash-latest",
                 "gemini-3.5-flash-lite", "gemini-flash-lite-latest"]


def charger_env() -> None:
    """Lit le .env du projet (CLE=valeur) sans ecraser les variables deja definies :
    sur GitHub, la cle vient des secrets et il n'y a pas de .env."""
    f = ROOT / ".env"
    if not f.exists():
        return
    for ligne in f.read_text(encoding="utf-8").splitlines():
        ligne = ligne.strip()
        if ligne and not ligne.startswith("#") and "=" in ligne:
            cle, val = ligne.split("=", 1)
            os.environ.setdefault(cle.strip(), val.strip().strip("\"'"))


charger_env()

W, H = 1080, 1920
MIN_S, MAX_S = 20.0, 45.0            # 45 s max : au-dela, la courbe de retention fond (Anticor 56 s : 20 % a la fin, Klein 47 s : 50 %)
# Zone conservee de la source 16:9 : un carre central. A 78 % de la largeur,
# l'image commentee restait minuscule au milieu d'un ecran sombre.
SRC_CROP_W = 0.5625
FG_Y = 380                           # haut du premier plan (carre 1080x1080)
HOOK_Y = 170                         # encadre d'accroche
HOOK_DUR = 3.2                       # l'accroche disparait apres l'ouverture, comme chez Opus
# Etiquette de la version Instagram/Facebook : courte, affichee TOUT le clip, sous
# le visage. C'est la recette de @thinkerview_nofficiel (166 k abonnes, 20 k a
# 790 k vues par Reel sur les memes interviews) : le spectateur sait en une seconde
# de quoi on parle. YouTube garde le clip habituel.
ETI_Y = 960                          # centre de l'ecran : c'est la que l'oeil tombe
ETI_DUR = 5.0                        # puis elle s'efface : le visage reste degage ensuite
# Volume cible des plateformes (-14 LUFS). Les interviews Thinkerview sortent vers
# -21 : un clip plus faible que le precedent dans le fil se fait passer.
LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"
CAP_Y = 1540                         # ligne de base des sous-titres (hors zone de l'interface Shorts)
WORDS_PER_CAP = 3
# Whisper date le debut d'un mot a l'attaque du son ; l'oeil, lui, a besoin d'une
# image ou deux d'avance pour que la lecture paraisse simultanee. Sans cette avance
# les sous-titres semblent suivre la voix au lieu de l'accompagner.
AVANCE_CAP = 0.10
# Polices EMBARQUEES dans assets/fonts/ (licence OFL) : Segoe UI n'existe que sous Windows,
# et le rendu doit etre identique sur le PC et sur les serveurs Linux de GitHub.
# Dossiers du projet : le code dans clipper/, les polices et la musique dans
# assets/, les videos dans media/ (hors Git, ce sont des gigaoctets).
FONTS_DIR = ROOT / "assets" / "fonts"
MEDIA = ROOT / "media"
SRC = MEDIA / "src"                  # sources telechargees + transcriptions
OUT = MEDIA / "out"                  # clips bruts + index par video
OUT_HABILLE = MEDIA / "out_habille"  # clips remontes en video explicative
CAP_FONT = "Montserrat Black"
HOOK_FONT = "Montserrat"                # graisse Bold, activee dans le style
ACTIVE = r"&H0020B0FF&"              # mot prononce : orange-jaune (#FFB020, ASS en BGR)
SCENE_SEUIL = 0.30                   # sensibilite de la detection de changement d'image
# mots d'amorce qu'un monteur coupe en debut d'extrait
FILLERS = {"alors", "bah", "ben", "donc", "euh", "et", "mais", "ouais", "bon", "enfin", "genre"}
# repliques vides qu'on ne laisse pas en fin d'extrait ("Euh...", "Ouais, ouais.")
VIDES = {"euh", "ouais", "ah", "oh", "ok", "okay", "bon", "voila", "voilà", "hein", "bref", "mh", "hm"}


# ------------------------------------------------------------------ transcription
def recoller(words: list[dict]) -> list[dict]:
    """Whisper decoupe le francais aux apostrophes : "c" + "'est", "j" + "'ai".
    Affiche tel quel, ca donne « C 'EST » a l'ecran. On recolle."""
    out: list[dict] = []
    for w in words:
        t = w["w"].strip()
        if not t:
            continue
        if out and (t.startswith(("'", "’", "-")) or out[-1]["w"].endswith(("'", "’"))):
            out[-1] = {"w": out[-1]["w"] + t, "s": out[-1]["s"], "e": w["e"]}
        else:
            out.append({"w": t, "s": w["s"], "e": w["e"]})
    return out


def phrases(words: list[dict], gap: float = 0.55) -> list[dict]:
    """Regroupe les mots en phrases : coupure sur ponctuation forte ou silence.
    `fin` distingue une vraie fin de phrase d'une simple pause."""
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        fin = w["w"].endswith((".", "?", "!", "…"))
        pause = nxt is not None and nxt["s"] - w["e"] > gap
        if fin or pause or nxt is None:
            out.append({"s": cur[0]["s"], "e": cur[-1]["e"], "fin": fin,
                        "t": " ".join(x["w"] for x in cur)})
            cur = []
    return out


# ------------------------------------------------------------------ changements d'image
def scenes(src: Path) -> list[float]:
    """Instants ou l'image change nettement (nouveau post, nouvelle video).
    Analyse en 320 px a 10 i/s : 45 s pour 13 min de source. Mis en cache."""
    cache = src.with_suffix(".scenes.txt")
    if not cache.exists():
        r = subprocess.run(
            ["ffmpeg", "-v", "info", "-i", str(src), "-an", "-vf",
             f"fps=10,scale=320:-2,select='gt(scene,{SCENE_SEUIL})',showinfo", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        ts = re.findall(r"pts_time:([0-9.]+)", r.stderr)
        cache.write_text("\n".join(ts), encoding="utf-8")
    brut = [float(x) for x in cache.read_text(encoding="utf-8").split()]
    fusion: list[float] = []
    for t in brut:                    # deux coupes a moins de 1,5 s = un seul changement
        if not fusion or t - fusion[-1] > 1.5:
            fusion.append(t)
    return fusion


# ------------------------------------------------------------------ choix des moments
def _json(raw: str):
    """Extrait le premier bloc JSON d'une reponse Gemini, meme bavarde."""
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    debut = min((i for i in (raw.find("["), raw.find("{")) if i >= 0), default=0)
    obj, _ = json.JSONDecoder().raw_decode(raw[debut:])
    return obj


def choisir(phr: list[dict], cuts: list[float], n: int, titre_src: str) -> list[dict]:
    from google import genai
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    lignes, ci = [], 0
    for p in phr:
        while ci < len(cuts) and cuts[ci] <= p["s"] + 0.3:
            lignes.append(f"--- nouvelle image a l'ecran ({cuts[ci]:.1f}s) ---")
            ci += 1
        lignes.append(f"[{p['s']:.1f}-{p['e']:.1f}] {p['t']}")
    prompt = (
        "Tu es monteur pour une chaine de clips YouTube Shorts qui fait des millions de vues.\n"
        f"Voici la transcription horodatee d'une video : « {titre_src} ».\n"
        "Les lignes « nouvelle image a l'ecran » indiquent quand l'image montree change "
        "(nouveau post, nouvelle photo commentee).\n\n"
        f"Choisis jusqu'a {n} extraits a publier en Shorts, du meilleur au moins bon. "
        "N'en garde pas un seul qui soit mediocre : mieux vaut moins d'extraits.\n\n"
        "Criteres :\n"
        "1. Un extrait = UN sujet complet : de preference, il commence quand l'image "
        "commentee apparait et va jusqu'a la chute de la discussion sur cette image.\n"
        "2. La PREMIERE PHRASE accroche seule : reaction forte, blague, question, "
        "affirmation choc. Jamais une transition ni une phrase d'installation.\n"
        "3. L'extrait se comprend SANS le reste de la video. Les spectateurs se "
        "plaignent en commentaire : « il parle de quoi ? », « de quelle blague parle-"
        "t-il ? ». Donc INTERDIT de commencer par une reference a ce qui a ete dit "
        "avant (« cette blague », « ce que je disais », « lui », « ca », « et donc »). "
        "Si le sujet est introduit par une question de l'intervieweur, commence a la "
        "question.\n"
        "4. Il contient la reponse ou l'information promise (on a eu « il manque l'info "
        "interessante » sur un extrait coupe avant la revelation) et il S'ARRETE "
        "JUSTE APRES LA CHUTE : la derniere phrase de l'extrait est la punchline, le "
        "rire ou la conclusion, prononcee EN ENTIER. Rien apres : ni commentaire, ni "
        "relance, ni « voila ». Les spectateurs decrochent a mesure que l'extrait "
        "s'allonge : retire tout ce qui n'amene pas a la chute (digression, repetition, "
        "exemple de trop).\n"
        "5. Un extrait qui ne remplit pas 3 et 4 est ELIMINE, meme s'il est fort.\n"
        f"6. Duree entre {MIN_S:.0f} et {MAX_S:.0f} secondes. Pas de chevauchement.\n"
        "7. EXCLUS : sponsors, codes promo, publicites, remerciements pour des dons, "
        "intro, outro, appels a s'abonner, logistique de la chaine.\n\n"
        "Utilise les horodatages exacts de la transcription.\n"
        "Titres et accroches sont affiches a l'ecran : ecris-les dans un francais "
        "impeccable, avec tous les accents et la ponctuation.\n"
        "Reponds UNIQUEMENT par un tableau JSON :\n"
        '[{"start": 12.3, "end": 48.7, '
        '"titre": "titre YouTube accrocheur, en francais", '
        '"accroche_ecran": "phrase courte affichee a l ouverture : 9 mots maximum, casse '
        'normale, sans emoji, cree la curiosite sans devoiler la chute", '
        '"etiquette": "3 a 6 mots, affiches tout le clip : une question qui donne le '
        'sujet (« Les medicaments tuent ? », « La verite sur Palantir ? ») ou une citation '
        'choc de l invite entre guillemets (« Un chomeur coute plus cher qu un mort »)", '
        '"score": 0-100}]\n\n'
        "TRANSCRIPTION :\n" + "\n".join(lignes)
    )
    for m in GEMINI_MODELS:
        try:
            resp = client.models.generate_content(model=m, contents=prompt)
            print(f"[choix] modele {m}")
            return _json(resp.text)
        except Exception as e:
            if any(x in str(e) for x in ("RESOURCE_EXHAUSTED", "429", "NOT_FOUND", "404",
                                         "UNAVAILABLE", "503", "INTERNAL", "500")):
                print(f"[choix] {m} indispo -> suivant")
                continue
            raise
    raise RuntimeError("Tous les modeles Gemini sont epuises.")


# ------------------------------------------------------------------ calage
def caler(clip: dict, words: list[dict], phr: list[dict],
          cuts: list[float]) -> tuple[float, float]:
    """Cale l'extrait sur l'IMAGE et sur la PHRASE.

    Mesures qui ont guide ce calage :
    - au mot le plus proche, un clip commencait sur "coup." (fin de la phrase
      precedente) et s'arretait sur "Alors vous avez besoin" ;
    - Opus Clip ouvre sur l'apparition de l'image mais commence souvent en plein
      mot ("'est un banc fait en acier").
    """
    s, e = float(clip["start"]), float(clip["end"])
    debuts = [p for i, p in enumerate(phr) if i == 0 or phr[i - 1]["fin"]]

    # --- debut : un vrai debut de phrase, a 4 s pres du point demande
    proches = [p for p in debuts if abs(p["s"] - s) <= 4.0]
    s = min(proches or phr, key=lambda p: abs(p["s"] - s))["s"]
    # si l'image commentee est apparue juste avant, on ouvre SUR son apparition,
    # a condition qu'aucune parole ne soit coupee entre l'image et la phrase
    # 9 s de recul : sur le micro-ondes, le post apparaissait 7,5 s avant la reaction
    # choisie par Gemini, avec la phrase qui la rend comprehensible ("On dirait mon
    # micro-ondes"). Opus commencait bien la ; avec 6 s de recul, on la ratait.
    # (la duree est verifiee sur la fin CALEE plus bas : la fin brute de Gemini
    # ecartait a tort "Moi, je me brosse les dents sous la douche")
    cut = max((c for c in cuts if s - 9.0 <= c <= s + 0.3 and e - c <= MAX_S + 4), default=None)
    if cut is not None:
        apres = [p["s"] for p in debuts if cut - 0.05 <= p["s"] <= s]
        if apres:
            s = apres[0]
        coupe = [w for w in words if w["s"] < s and w["e"] > cut]
        if not coupe and 0 <= s - cut <= 1.2:
            s = cut
        elif s < cut <= s + 0.3:
            # l'image change juste APRES le premier mot : ouvrir avant montrait le post
            # precedent pendant quelques images (le clip de l'ascenseur s'ouvrait sur
            # le pistolet a colle). On ouvre sur la nouvelle image.
            s = cut
    # Un bout de replique precedente colle au debut ("Pour la place. Moi, je me
    # brosse...", "fils de... Non, non") : 3 mots au plus, prononces sans pause apres
    # le mot d'avant. Whisper y met un point, mais c'est la fin d'une autre phrase.
    for _ in range(2):
        idx = next((i for i, p in enumerate(phr) if abs(p["s"] - s) < 0.02), None)
        if idx is None or idx + 1 >= len(phr):
            break
        p = phr[idx]
        precedent = [w["e"] for w in words if w["e"] <= p["s"] + 0.001]
        colle = bool(precedent) and p["s"] - precedent[-1] < 0.35
        if len(p["t"].split()) <= 3 and colle and phr[idx + 1]["s"] - p["e"] < 1.5:
            s = phr[idx + 1]["s"]
        else:
            break
    # un monteur coupe l'amorce : "Alors, on parle bien de..." -> "On parle bien de..."
    ws = [w for w in words if w["s"] >= s - 0.01]
    k = 0
    while k < min(2, len(ws) - 1) and ws[k]["w"].strip(" ,.!?…").lower() in FILLERS:
        k += 1
    if k and ws[k]["s"] > s:
        s = ws[k]["s"]

    # --- fin : jusqu'au point final (une pause seule ne termine pas une phrase)
    i1 = min(range(len(phr)), key=lambda i: abs(phr[i]["e"] - e))
    j = i1
    while j + 1 < len(phr) and not phr[j]["fin"] and phr[j + 1]["e"] - s <= MAX_S:
        j += 1
    if not phr[j]["fin"]:             # la minute serait depassee : on recule au dernier point
        kk = j
        while kk > 0 and not phr[kk]["fin"] and phr[kk - 1]["e"] - s >= MIN_S:
            kk -= 1
        if phr[kk]["fin"]:
            j = kk
    e = phr[j]["e"]
    # on finit la discussion de CETTE image : derniere phrase avant l'image suivante,
    # 12 s de plus au maximum (Opus allait jusqu'a "La, il m'a cloue", Gemini non)
    nxt = min((c for c in cuts if c > e - 0.3), default=None)
    if nxt is not None:
        fins = [p["e"] for p in phr
                if p["fin"] and e < p["e"] <= nxt + 0.2 and p["e"] <= e + 12 and p["e"] - s <= MAX_S]
        if fins:
            e = max(fins)
    # jamais de fin sur "Euh..." ou "Ouais, ouais." : on remonte a la phrase utile
    def _vide(p: dict) -> bool:
        mots = [m.strip(" ,.!?…").lower() for m in p["t"].split()]
        return bool(mots) and all(m in VIDES for m in mots if m)
    jf = next((i for i, p in enumerate(phr) if abs(p["e"] - e) < 0.02), None)
    while jf is not None and jf > 0 and _vide(phr[jf]) and phr[jf - 1]["e"] - s >= MIN_S:
        jf -= 1
        e = phr[jf]["e"]

    # --- garde-fou de duree : au-dela de la minute, on s'arrete au dernier point qui tient
    if e - s > MAX_S:
        tient = [p["e"] for p in phr if p["fin"] and s + MIN_S <= p["e"] <= s + MAX_S]
        e = max(tient) if tient else s + MAX_S

    # --- marges de respiration, jamais sur le mot voisin ni sur l'image suivante
    avant = [w["e"] for w in words if w["e"] <= s]
    apres_mots = [w["s"] for w in words if w["s"] >= e]
    marge_d = min(0.08, max(0.0, s - (avant[-1] if avant else 0.0) - 0.02))
    # la marge de debut ne doit pas non plus repasser avant l'apparition de l'image
    image_avant = max((c for c in cuts if c <= s + 0.001), default=None)
    if image_avant is not None:
        marge_d = min(marge_d, max(0.0, s - image_avant))
    marge_f = min(0.5, max(0.0, (apres_mots[0] if apres_mots else e + 0.5) - e - 0.03))
    fin_parole = e
    s, e = max(0.0, s - marge_d), e + marge_f
    # la marge ne doit pas laisser apparaitre l'image suivante... mais on ne coupe
    # jamais la parole pour autant : 3 images de trop genent moins qu'un mot tronque
    nxt = min((c for c in cuts if c > fin_parole - 0.05), default=None)
    if nxt is not None and nxt < e:
        # on tolere de rogner 50 ms de fin de mot : inaudible, et en deca de la
        # precision des horodatages de Whisper
        e = max(fin_parole - 0.05, nxt - 0.02)
    return s, e


# ------------------------------------------------------------------ sous-titres
def _ass_time(t: float) -> str:
    # arrondi au centieme (la resolution de l'ASS) et non troncature : tronquer
    # decalait chaque mot jusqu'a 10 ms de plus vers l'arriere, qui s'ajoutaient au
    # retard deja percu.
    cs = max(0, round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _propre(mot: str) -> str:
    return mot.strip(" ,.;:!?…\"«»")


def grouper(ws: list[dict]) -> list[list[dict]]:
    """Groupes de 3 mots au plus, qui ne chevauchent JAMAIS deux phrases : sans cette
    regle on lisait « 8 minutes Mais » ou « c'est vrai Défend ». Une pause marquee
    ferme aussi le groupe. Sert aussi a caler les clics du montage explicatif."""
    groupes, cur = [], []
    for i, w in enumerate(ws):
        cur.append(w)
        nxt = ws[i + 1] if i + 1 < len(ws) else None
        fin_phrase = w["w"].rstrip().endswith((".", "?", "!", "…"))
        pause = nxt is not None and nxt["s"] - w["e"] > 0.45
        if len(cur) == WORDS_PER_CAP or fin_phrase or pause or nxt is None:
            groupes.append(cur)
            cur = []
    return groupes


def mots_dans(words: list[dict], s: float, e: float) -> list[dict]:
    return [w for w in words if w["s"] >= s - 0.05 and w["e"] <= e + 0.05 and _propre(w["w"])]


def sous_titres(words: list[dict], s: float, e: float, dst: Path, accroche: str = "",
                etiquette: str = "") -> None:
    """Sous-titres par groupes de 3 mots, mot prononce en orange, apparition animee.
    Encadre d'accroche blanc pendant l'ouverture seulement."""
    ws = mots_dans(words, s, e)
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, "
        "Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # Ombre portee plus dense (alpha 0x50 au lieu de 0x96) et contour epaissi : sur
        # un fond clair comme une route en plein jour, le blanc se noyait.
        f"Style: Cap,{CAP_FONT},86,&H00FFFFFF,&H00FFFFFF,&H00000000,&H50000000,0,0,0,0,100,100,"
        "0,0,1,7,5,2,80,80,0,1\n"
        # BorderStyle 3 : fond opaque (couleur de contour) derriere le texte
        f"Style: Hook,{HOOK_FONT},66,&H00141414,&H00141414,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,"
        "0,0,3,22,0,8,170,170,0,1\n"
        # etiquette Instagram : meme encadre blanc, centre (alignement 5), plus petit
        f"Style: Tag,Anton,70,&H00141414,&H00141414,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,"
        "0,0,3,18,0,5,140,140,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    groupes = grouper(ws)
    evts = []
    for grp in groupes:
        for k, cur in enumerate(grp):
            t0 = cur["s"] - s - AVANCE_CAP
            t1 = (grp[k + 1]["s"] if k + 1 < len(grp) else grp[-1]["e"]) - s
            # le mot prononce grossit legerement : l'oeil est attire au rythme de la voix
            txt = " ".join(
                ("{\\c" + ACTIVE + "\\fscx112\\fscy112}" + _propre(w["w"])
                 + "{\\c&HFFFFFF&\\fscx100\\fscy100}") if j == k
                else _propre(w["w"]) for j, w in enumerate(grp))
            anim = r"\fscx86\fscy86\t(0,90,\fscx100\fscy100)" if k == 0 else ""
            evts.append([t0, t1, txt, anim])
    # Deux sous-titres affiches en meme temps se superposent au MEME endroit et le
    # texte devient illisible : l'avance ne doit jamais empieter sur le precedent,
    # qui s'efface a l'instant ou le suivant apparait.
    for a, b in zip(evts, evts[1:]):
        b[0] = max(b[0], a[0] + 0.04)
        a[1] = min(a[1], b[0])          # min : pendant un silence, le texte s'efface
    lignes = [f"Dialogue: 0,{_ass_time(t0)},{_ass_time(t1)},Cap,,0,0,0,,"
              + "{\\pos(540," + str(CAP_Y) + ")" + anim + "}" + txt
              for t0, t1, txt, anim in evts]
    if accroche:
        accroche = re.sub(r"[^\w\s'’?!,.:;-]", "", accroche).strip()
        lignes.insert(0, f"Dialogue: 1,{_ass_time(0)},{_ass_time(HOOK_DUR)},Hook,,0,0,0,,"
                      + "{\\pos(540," + str(HOOK_Y) + ")\\fad(120,220)}" + accroche)
    if etiquette:
        etiquette = re.sub(r"[^\w\s'’?!,.:;«»\"-]", "", etiquette).strip()
        lignes.insert(0, f"Dialogue: 1,{_ass_time(0)},{_ass_time(e - s + 1)},Tag,,0,0,0,,"
                      + "{\\pos(540," + str(ETI_Y) + ")\\fad(150,0)}" + etiquette)
    dst.write_text(head + "\n".join(lignes) + "\n", encoding="utf-8")


def etiquette_png(texte: str, dst: Path) -> int:
    """Dessine l'etiquette des versions Instagram : encadre blanc aux coins arrondis,
    ombre douce, texte noir tres gras, sur deux lignes au besoin. Le moteur de
    sous-titres ne sait faire ni coins arrondis ni ombre floue : on passe par une
    image incrustee. Renvoie la hauteur de l'image (pour la centrer sur ETI_Y)."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    texte = re.sub(r"\s+", " ", texte).strip()
    mesure = ImageDraw.Draw(Image.new("L", (1, 1)))
    # Une ligne si elle tient, sinon deux lignes coupees au mot le plus equilibre ;
    # et si une ligne reste trop large (« Des deputes deconnectes » sortait de
    # l'ecran le 13/10), on reduit la police jusqu'a ce que tout tienne.
    for taille in range(80, 46, -4):
        police = ImageFont.truetype(str(FONTS_DIR / "Montserrat-Black.ttf"), taille)
        larg = lambda t, p=police: mesure.textlength(t, font=p)
        lignes = [texte]
        if larg(texte) > 900:
            mots = texte.split()
            coupe = min(range(1, len(mots)), key=lambda i: max(larg(" ".join(mots[:i])),
                                                              larg(" ".join(mots[i:]))))
            lignes = [" ".join(mots[:coupe]), " ".join(mots[coupe:])]
        if max(larg(l) for l in lignes) <= 900:
            break
    asc, desc = police.getmetrics()
    interligne = asc + desc - 6
    pad_x, pad_y, rayon = 44, 26, 32
    bw = int(max(larg(l) for l in lignes)) + 2 * pad_x
    bh = interligne * len(lignes) + 2 * pad_y
    marge = 40                                  # place pour l'ombre
    img = Image.new("RGBA", (W, bh + 2 * marge), (0, 0, 0, 0))
    x0 = (W - bw) // 2
    # ombre : le meme encadre en noir, decale vers le bas et floute
    ombre = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(ombre).rounded_rectangle(
        (x0, marge + 10, x0 + bw, marge + 10 + bh), rayon, fill=(0, 0, 0, 150))
    img.alpha_composite(ombre.filter(ImageFilter.GaussianBlur(14)))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((x0, marge, x0 + bw, marge + bh), rayon, fill=(255, 255, 255, 255))
    for k, l in enumerate(lignes):
        d.text((W / 2, marge + pad_y + k * interligne + interligne / 2), l,
               font=police, fill=(17, 17, 17, 255), anchor="mm")
    img.save(dst)
    return img.height


# ------------------------------------------------------------------ rendu
def rendre(src: Path, s: float, e: float, ass: Path, dst: Path,
           etiquette: Path | None = None, haut_eti: int = 0) -> None:
    fg_h = int(round(W / (16 / 9 * SRC_CROP_W) / 2) * 2)
    ass_ff = str(ass).replace("\\", "/").replace(":", r"\:")
    fonts_ff = str(FONTS_DIR).replace("\\", "/").replace(":", r"\:")
    fc = (
        # fond : la video elle-meme, agrandie, floutee et legerement assombrie
        f"[0:v]scale=-2:{H},crop={W}:{H},boxblur=30:2,eq=brightness=-0.08:saturation=1.1[bg];"
        # premier plan : le carre central de la source, en pleine largeur
        f"[0:v]crop=iw*{SRC_CROP_W}:ih,scale={W}:{fg_h}:flags=lanczos[fg];"
        # pas de fps=30 : on garde les 60 i/s de la source, comme Opus
        f"[bg][fg]overlay=(W-w)/2:{FG_Y},subtitles='{ass_ff}':fontsdir='{fonts_ff}'"
        # pas de fondu d'entree : l'etiquette est la des la premiere image, c'est
        # elle qui retient le pouce ; seule la sortie est adoucie
        + (f"[sub];[1:v]format=rgba,fade=out:st={ETI_DUR - 0.4}:d=0.4:alpha=1[eti];"
           # eof_action=pass : l'image finie, la video continue sans elle
           f"[sub][eti]overlay=0:{ETI_Y - haut_eti // 2}:eof_action=pass[v];"
           if etiquette else "[v];") +
        # le son au volume des plateformes (dans le graphe : -af refuse de se combiner
        # avec -filter_complex)
        f"[0:a]{LOUDNORM}[a]"
    )
    cmd = ["ffmpeg", "-y", "-ss", f"{s:.2f}", "-t", f"{e - s:.2f}", "-i", str(src),
           *(["-loop", "1", "-t", f"{ETI_DUR}", "-i", str(etiquette)] if etiquette else []),
           "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
           # preset reglable : les machines a 2 coeurs de GitHub passent en "fast"
           "-c:v", "libx264", "-crf", "17",
           "-preset", os.environ.get("CLIPPER_X264_PRESET", "medium"), "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
           "-movflags", "+faststart", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        raise RuntimeError(r.stderr[-1500:])


def slug(t: str) -> str:
    t = unicodedata.normalize("NFD", t.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:50]


def publiable(info: dict) -> bool:
    """Seule une source sous licence Creative Commons peut etre republiee sans accord.
    L'Orange Pi refuse de publier un clip marque non publiable."""
    return "creative" in (info.get("licence") or "").lower()


def invite(titre_source: str) -> str:
    """Le nom de l'invite, tire du titre de l'entretien : deux mots capitalises qui
    se suivent. Il est place n'importe ou selon les videos — au debut chez Etienne
    Klein, a la fin chez Aurelien Barrau — et « EN DIRECT » ne doit pas passer pour
    un nom, d'ou le rejet des mots tout en majuscules."""
    # Format habituel de Thinkerview : « Question ? Invite [EN DIRECT] ». Le 11/10,
    # « France Corruption : Qui se partage… ? Anticor » donnait « France Corruption ».
    m = re.search(r"\?\s*([^?\[\]]{3,60}?)\s*\[", titre_source)
    if m and not m.group(1).isupper():
        return m.group(1).strip(" :-")
    # deux mots capitalises VRAIMENT voisins (un seul espace entre eux), noms a trait
    # d'union compris : « Astrophysique : Jean-Pierre Petit » donnait « Astrophysique Jean »
    mot = r"[^\W\d_][^\W\d_'’-]*(?:-[^\W\d_]+)*"
    for m in re.finditer(rf"(?<![\w-])({mot}) ({mot})(?![\w-])", titre_source):
        if all(x[:1].isupper() and not x.isupper() and len(x) > 1 for x in m.groups()):
            nom = f"{m.group(1)} {m.group(2)}"
            # particule de nom de famille : « Bruno Le Maire », pas « Bruno Le »
            suite = re.match(rf" ({mot})", titre_source[m.end():])
            if m.group(2) in {"Le", "La", "De", "Du", "Des", "Van", "Von", "Da", "Di"} and suite:
                nom += " " + suite.group(1)
            return nom
    # nom d'un seul mot en tete de titre : « Haroun : l'humour comme miroir… »
    m = re.match(r"\s*([^\W\d_][\w'’-]{2,})\s*:", titre_source)
    return m.group(1) if m and not m.group(1).isupper() else ""


def description(clip: dict, info: dict) -> str:
    """Description YouTube avec le credit exige par la licence CC BY. Sans elle, la
    licence n'est pas respectee et le createur peut faire retirer la video : les deux
    premiers clips Thinkerview ont ete publies avec une description vide.

    Format demande : trois lignes, sans accroche ni mots-dieses."""
    nom = invite(info.get("titre", ""))
    art = "d'" if nom[:1].lower() in "aeiouyéèêh" else "de "
    qui = f"l'interview {art}{nom}" if nom else f"« {info.get('titre', '')} »"
    lignes = [f"Extrait de {qui} par {info.get('chaine', '')}."]
    if info.get("url"):
        lignes.append(f"Vidéo originale : {info['url']}")
    if publiable(info):
        lignes.append("Licence Creative Commons Attribution (CC BY)")
    else:
        lignes.append("Licence YouTube standard : ne pas publier sans l'accord du créateur.")
    return "\n".join(lignes)


def produire(src: Path, choix: list[dict], n: int, suivi=None) -> list[dict]:
    """Cale et rend les extraits choisis. Ecrit l'index `out/<id>.json` lu par la page
    locale. `suivi(i, total, clip)` est appele avant chaque rendu."""
    words = recoller(json.loads(src.with_suffix(".words.json").read_text(encoding="utf-8")))
    phr = phrases(words)
    cuts = scenes(src)
    out = OUT
    out.mkdir(exist_ok=True)
    for vieux in out.glob(f"{src.stem}_*"):       # un nouveau rendu remplace l'ancien
        vieux.unlink()
    info_f = src.with_suffix(".info.json")
    info = json.loads(info_f.read_text(encoding="utf-8")) if info_f.exists() else {"id": src.stem}
    pris: list[tuple[float, float]] = []
    rendus: list[dict] = []
    retenus = choix[:n]
    for i, c in enumerate(retenus, 1):
        s, e = caler(c, words, phr, cuts)
        if e - s < MIN_S - 3:
            print(f"[{i}] ignore : {e - s:.0f}s apres calage, trop court")
            continue
        if any(min(e, b) - max(s, a) > 0.5 * (e - s) for a, b in pris):
            print(f"[{i}] ignore : chevauche un extrait deja retenu")
            continue
        pris.append((s, e))
        if suivi:
            suivi(i, len(retenus), c)
        base = out / f"{src.stem}_{i}_{slug(c.get('titre', 'clip'))}"
        ass = base.with_suffix(".ass")
        print(f"[{i}] {s:.1f}-{e:.1f}s ({e - s:.0f}s) score {c.get('score')} : {c.get('titre')}")
        # Une seule version pour toutes les plateformes (choix du 13/10) : l'etiquette en
        # gros au centre les 5 premieres secondes, a la place de l'encadre du haut.
        # Sans etiquette (ancien choix en cache), on garde l'encadre d'accroche.
        if c.get("etiquette"):
            png = base.with_suffix(".etiquette.png")
            haut = etiquette_png(c["etiquette"], png)
            sous_titres(words, s, e, ass)
            rendre(src, s, e, ass, base.with_suffix(".mp4"), png, haut)
            png.unlink(missing_ok=True)
        else:
            sous_titres(words, s, e, ass, c.get("accroche_ecran") or "")
            rendre(src, s, e, ass, base.with_suffix(".mp4"))
        ass.unlink(missing_ok=True)
        rendus.append({"fichier": base.with_suffix(".mp4").name, "titre": c.get("titre", ""),
                       "accroche": c.get("accroche_ecran", ""), "score": c.get("score"),
                       "etiquette": c.get("etiquette", ""),
                       "debut": round(s, 2), "fin": round(e, 2), "duree": round(e - s, 1),
                       "description": description(c, info), "publiable": publiable(info)})
    index ={**info, "genere": __import__("time").strftime("%Y-%m-%d %H:%M"), "clips": rendus}
    (out / f"{src.stem}.json").write_text(json.dumps(index, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    return rendus


def choix_moments(src: Path, n: int, titre_src: str, rechoisir: bool = False) -> list[dict]:
    """Choix Gemini, mis en cache a cote de la source."""
    cache = src.with_suffix(".clips.json")
    if cache.exists() and not rechoisir:
        print("[choix] relu depuis le cache")
        return json.loads(cache.read_text(encoding="utf-8"))
    words = recoller(json.loads(src.with_suffix(".words.json").read_text(encoding="utf-8")))
    choix = choisir(phrases(words), scenes(src), n, titre_src)
    cache.write_text(json.dumps(choix, ensure_ascii=False, indent=1), encoding="utf-8")
    return choix


def main() -> int:
    src = Path(sys.argv[1])
    n = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 4
    titre_src = sys.argv[sys.argv.index("--titre") + 1] if "--titre" in sys.argv else src.stem
    choix = choix_moments(src, n, titre_src, "--rechoisir" in sys.argv)
    produire(src, choix, n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
