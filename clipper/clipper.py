"""Decoupe une video longue en Shorts, facon Opus Clip.

    python clipper/transcribe.py clipper/src/<id>.mp4       1. transcription (une fois)
    python clipper/clipper.py clipper/src/<id>.mp4 --n 8     2. choix + rendu
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
MIN_S, MAX_S = 20.0, 58.0            # un Short au-dela de 60 s n'est plus un Short
# Zone conservee de la source 16:9 : un carre central. A 78 % de la largeur,
# l'image commentee restait minuscule au milieu d'un ecran sombre.
SRC_CROP_W = 0.5625
FG_Y = 380                           # haut du premier plan (carre 1080x1080)
HOOK_Y = 170                         # encadre d'accroche
HOOK_DUR = 3.2                       # l'accroche disparait apres l'ouverture, comme chez Opus
CAP_Y = 1540                         # ligne de base des sous-titres (hors zone de l'interface Shorts)
WORDS_PER_CAP = 3
# Polices EMBARQUEES dans fonts/ (licence OFL) : Segoe UI n'existe que sous Windows,
# et le rendu doit etre identique sur le PC et sur les serveurs Linux de GitHub.
FONTS_DIR = ROOT / "fonts"
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
        "2. La PREMIERE PHRASE accroche seule : reaction forte, blague, question. "
        "Jamais une transition ni une phrase d'installation.\n"
        "3. L'extrait se comprend SANS le reste de la video.\n"
        "4. Il se termine sur une CHUTE (punchline, rire, conclusion).\n"
        f"5. Duree entre {MIN_S:.0f} et {MAX_S:.0f} secondes. Pas de chevauchement.\n"
        "6. EXCLUS : sponsors, codes promo, publicites, remerciements pour des dons, "
        "intro, outro, appels a s'abonner, logistique de la chaine.\n\n"
        "Utilise les horodatages exacts de la transcription.\n"
        "Titres et accroches sont affiches a l'ecran : ecris-les dans un francais "
        "impeccable, avec tous les accents et la ponctuation.\n"
        "Reponds UNIQUEMENT par un tableau JSON :\n"
        '[{"start": 12.3, "end": 48.7, '
        '"titre": "titre YouTube accrocheur, en francais", '
        '"accroche_ecran": "phrase courte affichee a l ouverture : 9 mots maximum, casse '
        'normale, sans emoji, cree la curiosite sans devoiler la chute", '
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
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{int(t % 60):02d}.{int(t * 100 % 100):02d}"


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


def sous_titres(words: list[dict], s: float, e: float, dst: Path, accroche: str = "") -> None:
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
        "0,0,3,22,0,8,170,170,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    groupes = grouper(ws)
    lignes = []
    for grp in groupes:
        for k, cur in enumerate(grp):
            t0 = cur["s"] - s
            t1 = (grp[k + 1]["s"] if k + 1 < len(grp) else grp[-1]["e"]) - s
            # le mot prononce grossit legerement : l'oeil est attire au rythme de la voix
            txt = " ".join(
                ("{\\c" + ACTIVE + "\\fscx112\\fscy112}" + _propre(w["w"])
                 + "{\\c&HFFFFFF&\\fscx100\\fscy100}") if j == k
                else _propre(w["w"]) for j, w in enumerate(grp))
            anim = r"\fscx86\fscy86\t(0,90,\fscx100\fscy100)" if k == 0 else ""
            lignes.append(f"Dialogue: 0,{_ass_time(t0)},{_ass_time(t1)},Cap,,0,0,0,,"
                          + "{\\pos(540," + str(CAP_Y) + ")" + anim + "}" + txt)
    if accroche:
        accroche = re.sub(r"[^\w\s'’?!,.:;-]", "", accroche).strip()
        lignes.insert(0, f"Dialogue: 1,{_ass_time(0)},{_ass_time(HOOK_DUR)},Hook,,0,0,0,,"
                      + "{\\pos(540," + str(HOOK_Y) + ")\\fad(120,220)}" + accroche)
    dst.write_text(head + "\n".join(lignes) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ rendu
def rendre(src: Path, s: float, e: float, ass: Path, dst: Path) -> None:
    fg_h = int(round(W / (16 / 9 * SRC_CROP_W) / 2) * 2)
    ass_ff = str(ass).replace("\\", "/").replace(":", r"\:")
    fonts_ff = str(FONTS_DIR).replace("\\", "/").replace(":", r"\:")
    fc = (
        # fond : la video elle-meme, agrandie, floutee et legerement assombrie
        f"[0:v]scale=-2:{H},crop={W}:{H},boxblur=30:2,eq=brightness=-0.08:saturation=1.1[bg];"
        # premier plan : le carre central de la source, en pleine largeur
        f"[0:v]crop=iw*{SRC_CROP_W}:ih,scale={W}:{fg_h}:flags=lanczos[fg];"
        # pas de fps=30 : on garde les 60 i/s de la source, comme Opus
        f"[bg][fg]overlay=(W-w)/2:{FG_Y},subtitles='{ass_ff}':fontsdir='{fonts_ff}'[v]"
    )
    cmd = ["ffmpeg", "-y", "-ss", f"{s:.2f}", "-t", f"{e - s:.2f}", "-i", str(src),
           "-filter_complex", fc, "-map", "[v]", "-map", "0:a",
           # preset reglable : les machines a 2 coeurs de GitHub passent en "fast"
           "-c:v", "libx264", "-crf", "17",
           "-preset", os.environ.get("CLIPPER_X264_PRESET", "medium"), "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dst)]
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


def description(clip: dict, info: dict) -> str:
    """Description YouTube avec le credit exige par la licence CC BY. Sans elle, la
    licence n'est pas respectee et le createur peut faire retirer la video : les deux
    premiers clips Thinkerview ont ete publies avec une description vide."""
    lignes = [clip.get("accroche_ecran") or clip.get("titre", ""), ""]
    lignes.append(f"Extrait de « {info.get('titre', '')} », par {info.get('chaine', '')}.")
    if info.get("url"):
        lignes.append(f"Vidéo originale : {info['url']}")
    if publiable(info):
        lignes.append("Licence : Creative Commons Attribution (CC BY)")
    else:
        lignes.append("Licence YouTube standard : ne pas publier sans l'accord du créateur.")
    return "\n".join(lignes).strip()


def produire(src: Path, choix: list[dict], n: int, suivi=None) -> list[dict]:
    """Cale et rend les extraits choisis. Ecrit l'index `out/<id>.json` lu par la page
    locale. `suivi(i, total, clip)` est appele avant chaque rendu."""
    words = recoller(json.loads(src.with_suffix(".words.json").read_text(encoding="utf-8")))
    phr = phrases(words)
    cuts = scenes(src)
    out = HERE / "out"
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
        sous_titres(words, s, e, ass, c.get("accroche_ecran") or "")
        print(f"[{i}] {s:.1f}-{e:.1f}s ({e - s:.0f}s) score {c.get('score')} : {c.get('titre')}")
        rendre(src, s, e, ass, base.with_suffix(".mp4"))
        ass.unlink(missing_ok=True)
        rendus.append({"fichier": base.with_suffix(".mp4").name, "titre": c.get("titre", ""),
                       "accroche": c.get("accroche_ecran", ""), "score": c.get("score"),
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
    n = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 8
    titre_src = sys.argv[sys.argv.index("--titre") + 1] if "--titre" in sys.argv else src.stem
    choix = choix_moments(src, n, titre_src, "--rechoisir" in sys.argv)
    produire(src, choix, n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
