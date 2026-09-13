"""Decoupe une video longue en Shorts, facon Opus Clip.

    python clipper/clipper.py clipper/src/<id>.mp4          (transcription deja faite)
    python clipper/clipper.py clipper/src/<id>.mp4 --n 4    nombre de clips voulus

Etapes : transcription mot a mot (transcribe.py) -> Gemini choisit les meilleurs
moments -> rendu vertical 1080x1920 avec sous-titres mot a mot.

Mise en page : les sources de ce type sont des REACTIONS a des captures d'ecran,
pas des visages filmes. Un recadrage sur un visage n'aurait aucun sens : on garde
l'image centrale en grand, sur un fond flou tire de la video elle-meme.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
import generate as G  # noqa: E402  (.env, modeles Gemini, police)

W, H = 1080, 1920
FONT = ROOT / "fonts" / "Anton-Regular.ttf"
MIN_S, MAX_S = 20.0, 58.0            # un Short au-dela de 60 s n'est plus un Short
# Zone conservee de la source 16:9. Mesure sur une reaction a Reddit : a 78 % de
# la largeur, l'image commentee restait minuscule au milieu d'un ecran sombre.
# Un carre central (1080 px sur une source 1080p) la rend lisible sur mobile.
SRC_CROP_W = 0.5625
FG_Y = 330                           # haut du premier plan (carre 1080x1080)
TITLE_Y = 120                        # titre d'accroche au-dessus de l'image
CAP_Y = 1600                         # ligne de base des sous-titres, sous l'image
WORDS_PER_CAP = 3
# mots d'amorce qu'un monteur coupe en debut d'extrait
FILLERS = {"alors", "bah", "ben", "donc", "euh", "et", "mais", "ouais", "bon", "enfin", "genre"}


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
    """Regroupe les mots en phrases : coupure sur ponctuation forte ou silence."""
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


# ------------------------------------------------------------------ choix des moments
def _json(raw: str):
    """Extrait le premier bloc JSON d'une reponse Gemini, meme bavarde."""
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
    debut = min((i for i in (raw.find("["), raw.find("{")) if i >= 0), default=0)
    obj, _ = json.JSONDecoder().raw_decode(raw[debut:])
    return obj


def choisir(phr: list[dict], n: int, titre_src: str) -> list[dict]:
    from google import genai
    client = genai.Client(api_key=G.os.getenv("GEMINI_API_KEY"))
    transcript = "\n".join(f"[{p['s']:.1f}-{p['e']:.1f}] {p['t']}" for p in phr)
    prompt = (
        "Tu es monteur pour une chaine de clips YouTube Shorts qui fait des millions de vues.\n"
        f"Voici la transcription horodatee d'une video : « {titre_src} ».\n"
        f"Choisis les {n} MEILLEURS extraits a publier en Shorts.\n\n"
        "Criteres, par ordre d'importance :\n"
        "1. La PREMIERE PHRASE accroche seule : blague, reaction forte, question, "
        "affirmation choc. Jamais une phrase d'installation ou de transition.\n"
        "2. L'extrait se comprend SANS le reste de la video.\n"
        "3. Il se termine sur une CHUTE (rire, punchline, conclusion), pas au milieu d'une idee.\n"
        f"4. Duree entre {MIN_S:.0f} et {MAX_S:.0f} secondes.\n"
        "5. Les extraits ne se chevauchent pas.\n\n"
        "Utilise EXACTEMENT les horodatages de debut et de fin des phrases de la transcription.\n"
        "Reponds UNIQUEMENT par un tableau JSON, du meilleur au moins bon :\n"
        '[{"start": 12.3, "end": 48.7, "titre": "titre YouTube accrocheur en francais", '
        '"accroche": "pourquoi la 1re phrase stoppe le scroll", "score": 0-100}]\n\n'
        f"TRANSCRIPTION :\n{transcript}"
    )
    for m in G.GEMINI_MODELS:
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


def caler(clip: dict, words: list[dict], phr: list[dict]) -> tuple[float, float]:
    """Cale l'extrait sur des PHRASES entieres.

    Premier essai : calage au mot le plus proche. Resultat mesure : un clip qui
    commencait sur "coup." (fin de la phrase precedente) et s'arretait sur "Alors
    vous avez besoin", juste avant la chute. On part donc du debut de la phrase
    qui contient le point de depart, et on va jusqu'a la FIN de la phrase qui
    contient le point d'arrivee.
    """
    s, e = float(clip["start"]), float(clip["end"])
    # debut : une phrase qui SUIT une ponctuation forte (un vrai debut de phrase),
    # la plus proche du point demande, a 4 s pres
    debuts = [p for i, p in enumerate(phr) if i == 0 or phr[i - 1]["fin"]]
    proches = [p for p in debuts if abs(p["s"] - s) <= 4.0]
    s = min(proches or phr, key=lambda p: abs(p["s"] - s))["s"]
    # fin : on prolonge jusqu'au prochain point, ? ou ! -- une pause seule ne
    # termine pas une phrase ("Alors vous avez besoin" + silence)
    i1 = min(range(len(phr)), key=lambda i: abs(phr[i]["e"] - e))
    j = i1
    while j + 1 < len(phr) and not phr[j]["fin"] and phr[j + 1]["e"] - s <= MAX_S:
        j += 1
    if not phr[j]["fin"]:
        # impossible d'atteindre le point final sans depasser la minute : on RECULE
        # jusqu'a la derniere phrase terminee, plutot que de couper au milieu
        k = j
        while k > 0 and not phr[k]["fin"] and phr[k - 1]["e"] - s >= MIN_S:
            k -= 1
        if phr[k]["fin"]:
            j = k
    e = phr[j]["e"]
    # un monteur coupe l'amorce : "Alors, on parle bien de..." -> "On parle bien de..."
    ws = [w for w in words if w["s"] >= s - 0.01]
    k = 0
    while k < min(2, len(ws) - 1) and ws[k]["w"].strip(" ,.!?…").lower() in FILLERS:
        k += 1
    s = ws[k]["s"] if ws else s
    if e - s > MAX_S:                 # trop long : on s'arrete a la derniere phrase qui tient
        fins = [p["e"] for p in phr if s < p["e"] <= s + MAX_S]
        e = fins[-1] if fins else s + MAX_S
    # Marges de respiration, mais JAMAIS sur le mot voisin : ces createurs enchainent
    # a 40 ms pres, et une marge fixe de 0,5 s faisait entendre "Alors vous avez
    # besoin", le debut de la phrase suivante, apres le point final.
    avant = [w["e"] for w in words if w["e"] <= s]
    apres = [w["s"] for w in words if w["s"] >= e]
    marge_d = min(0.08, max(0.0, s - (avant[-1] if avant else 0.0) - 0.02))
    marge_f = min(0.5, max(0.0, (apres[0] if apres else e + 0.5) - e - 0.03))
    return max(0.0, s - marge_d), e + marge_f


# ------------------------------------------------------------------ sous-titres
def _ass_time(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{int(t % 60):02d}.{int(t * 100 % 100):02d}"


def _propre(mot: str) -> str:
    return mot.strip(" ,.;:!?…\"«»").upper()


def sous_titres(words: list[dict], s: float, e: float, dst: Path, titre: str = "") -> None:
    """Groupes de 3 mots, le mot prononce passe en jaune. Blanc, contour noir epais :
    le style des clips pros, lisible sur n'importe quel fond."""
    ws = [w for w in words if w["s"] >= s - 0.05 and w["e"] <= e + 0.05 and _propre(w["w"])]
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, "
        "Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Cap,Anton,92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,"
        "1,0,1,7,3,2,60,60,0,1\n"
        "Style: Titre,Anton,70,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,0,0,0,0,100,100,"
        "1,0,1,6,2,8,70,70,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lignes = []
    for g in range(0, len(ws), WORDS_PER_CAP):
        grp = ws[g:g + WORDS_PER_CAP]
        for k, cur in enumerate(grp):
            t0 = cur["s"] - s
            t1 = (grp[k + 1]["s"] if k + 1 < len(grp) else grp[-1]["e"]) - s
            txt = " ".join(
                (r"{\c&H00FFFF&}" + _propre(w["w"]) + r"{\c&HFFFFFF&}") if j == k
                else _propre(w["w"]) for j, w in enumerate(grp))
            lignes.append(f"Dialogue: 0,{_ass_time(t0)},{_ass_time(t1)},Cap,,0,0,0,,"
                          + r"{\pos(540," + str(CAP_Y) + r")}" + txt)
    if titre:
        # titre d'accroche fixe au-dessus de l'image, emojis retires (Anton ne les a pas)
        titre = re.sub(r"[^\w\s'’?!,.-]", "", titre).strip().upper()
        lignes.insert(0, f"Dialogue: 1,{_ass_time(0)},{_ass_time(e - s + 1)},Titre,,0,0,0,,"
                      + r"{\pos(540," + str(TITLE_Y) + r")\q0}" + titre)
    dst.write_text(head + "\n".join(lignes) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ rendu
def rendre(src: Path, s: float, e: float, ass: Path, dst: Path) -> None:
    fg_w = W
    cw = f"iw*{SRC_CROP_W}"
    fg_h = int(round(W / (16 / 9 * SRC_CROP_W) / 2) * 2)
    ass_ff = str(ass).replace("\\", "/").replace(":", r"\:")
    fonts_ff = str(FONT.parent).replace("\\", "/").replace(":", r"\:")
    fc = (
        # fond : la video elle-meme, agrandie, floutee et assombrie
        f"[0:v]scale=-2:{H},crop={W}:{H},boxblur=30:2,eq=brightness=-0.08:saturation=1.1[bg];"
        # premier plan : la zone utile de la source, en pleine largeur
        f"[0:v]crop={cw}:ih,scale={fg_w}:{fg_h}:flags=lanczos[fg];"
        f"[bg][fg]overlay=(W-w)/2:{FG_Y},fps=30,"
        f"subtitles='{ass_ff}':fontsdir='{fonts_ff}'[v]"
    )
    cmd = ["ffmpeg", "-y", "-ss", f"{s:.2f}", "-t", f"{e - s:.2f}", "-i", str(src),
           "-filter_complex", fc, "-map", "[v]", "-map", "0:a",
           "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dst)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode:
        raise RuntimeError(r.stderr[-1500:])


def slug(t: str) -> str:
    t = unicodedata.normalize("NFD", t.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:50]


def main() -> int:
    src = Path(sys.argv[1])
    n = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 4
    words = recoller(json.loads(src.with_suffix(".words.json").read_text(encoding="utf-8")))
    titre_src = sys.argv[sys.argv.index("--titre") + 1] if "--titre" in sys.argv else src.stem
    phr = phrases(words)
    print(f"[transcription] {len(words)} mots, {len(phr)} phrases")

    cache = src.with_suffix(".clips.json")
    if cache.exists() and "--rechoisir" not in sys.argv:
        choix = json.loads(cache.read_text(encoding="utf-8"))
        print("[choix] relu depuis le cache")
    else:
        choix = choisir(phr, n, titre_src)
        cache.write_text(json.dumps(choix, ensure_ascii=False, indent=1), encoding="utf-8")

    out = HERE / "out"
    out.mkdir(exist_ok=True)
    for i, c in enumerate(choix[:n], 1):
        s, e = caler(c, words, phr)
        if e - s < MIN_S - 3:
            print(f"[{i}] ignore : {e - s:.0f}s, trop court")
            continue
        base = out / f"{src.stem}_{i}_{slug(c.get('titre', 'clip'))}"
        ass = base.with_suffix(".ass")
        sous_titres(words, s, e, ass, c.get("titre", ""))
        print(f"[{i}] {s:.1f}-{e:.1f}s ({e - s:.0f}s) score {c.get('score')} : {c.get('titre')}")
        print(f"     accroche : {c.get('accroche')}")
        rendre(src, s, e, ass, base.with_suffix(".mp4"))
        print(f"     -> {base.with_suffix('.mp4').name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
