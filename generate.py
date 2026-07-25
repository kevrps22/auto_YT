"""
generate.py — Sujet -> Short YouTube (.mp4) 100% gratuit.

Pipeline :
  1. Script      : Gemini (free tier) -> JSON {title, hook, facts[], keywords[]}
  2. Voix off    : edge-tts (voix FR Microsoft, gratuit) -> audio.mp3 + timings mot a mot
  3. Sous-titres : construits depuis les timings edge-tts (pas besoin de Whisper)
  4. Visuels     : clips verticaux Pexels (gratuit)
  5. Montage     : FFmpeg -> out.mp4 (1080x1920, voix + sous-titres brules)

Variables d'environnement :
  GEMINI_API_KEY  (optionnel : sans elle, un script d'exemple est utilise)
  PEXELS_API_KEY  (requis pour les visuels ; sinon fond noir)

Usage :
  python generate.py "les trous noirs"
  python generate.py            # sujet aleatoire par defaut
"""

import asyncio
import json
import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

try:                                  # console Windows -> UTF-8 (emojis, accents)
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _load_env():
    """Charge un fichier .env local (KEY=VALEUR) dans os.environ, si present."""
    env = Path(__file__).with_name(".env")
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()

# ---------------------------------------------------------------- config
# Voix "Multilingual" = les plus naturelles d'edge-tts (intonation + emotion).
# Alternatives : fr-FR-VivienneMultilingualNeural (feminine), fr-FR-HenriNeural (classique).
VOICE = "fr-FR-RemyMultilingualNeural"
# Ton (rate, pitch, volume) par segment du script -> voix vivante, pas monotone.
TONES = {
    "hook":       ("+14%", "+13Hz", "+22%"),   # fort, excite, rapide = accroche
    "tension":    ("+3%",  "+6Hz",  "+8%"),    # intrigant, on ralentit un peu
    "body":       ("+4%",  "-1Hz",  "+0%"),    # explication posee, ton "je revele un secret"
    "revelation": ("-3%",  "+4Hz",  "+12%"),   # emphase, on ralentit pour l'impact
    "loop":       ("+6%",  "+2Hz",  "+5%"),    # relance vers le debut
}
PAUSE_BEFORE_REVELATION = 0.28       # micro-pause dramatique avant la revelation
W, H = 1080, 1920                    # format vertical Short
FONT = "Anton"                       # police des sous-titres (fichier dans fonts/)

LEAD = 0.3                           # quasi pas d'intro : le hook demarre tout de suite
TAIL = 2.0                           # petite outro pour ne pas couper net
MUSIC_DIR = Path("music")            # depose des .mp3 ici (musique de fond, choix aleatoire)
MUSIC_VOL = 0.22                     # volume de la musique (0-1) ; ducking sous la voix
OUTRO_TEXT = "ABONNE-TOI POUR LA SUITE"
OUT_DIR = Path("output")
WORK = Path(tempfile.mkdtemp(prefix="short_"))
DEFAULT_TOPICS = [
    # --- Espace & univers
    "les trous noirs", "l'espace", "le soleil", "la lune", "les etoiles",
    "les galaxies", "Mars", "les trous de ver", "la vitesse de la lumiere",
    # --- Corps humain & cerveau
    "le cerveau humain", "le sommeil", "les reves", "le coeur humain",
    "l'ADN", "le systeme immunitaire", "la memoire", "les yeux",
    # --- Animaux
    "les fourmis", "les pieuvres", "les requins", "les abeilles",
    "les dauphins", "les chats", "les manchots", "les meduses immortelles",
    "les axolotls", "les corbeaux", "les tardigrades",
    # --- Nature & Terre
    "les oceans profonds", "les volcans", "les seismes", "la foudre",
    "les aurores boreales", "les deserts", "la foret amazonienne",
    "les grottes", "le noyau de la Terre",
    # --- Histoire & civilisations
    "l'Egypte antique", "les pyramides", "l'Empire romain", "les Vikings",
    "les samourais", "la Grande Muraille de Chine", "les Mayas",
    # --- Science & tech
    "la physique quantique", "l'intelligence artificielle", "les mathematiques",
    "l'or", "les diamants", "internet", "les fusees",
    # --- Mysteres & insolite
    "le triangle des Bermudes", "les illusions d'optique", "le temps",
    "les nombres premiers", "la chance",
]


# ---------------------------------------------------------------- 1. script
def make_script(topic: str) -> dict:
    """Genere le script via Gemini, ou renvoie un exemple si pas de cle."""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        print("[script] pas de GEMINI_API_KEY -> script d'exemple")
        return {
            "title": f"La verite sur {topic}",
            "hook": f"Ce que tu crois sur {topic} est FAUX.",
            "tension": "Et la realite est bien plus flippante.",
            "body": f"En vrai, {topic}, c'est un phenomene que presque personne "
                    "ne comprend vraiment. Les chiffres derriere sont hallucinants.",
            "revelation": "Et le plus fou, c'est que ca se passe juste sous tes yeux.",
            "loop": "La prochaine fois, tu y repenseras forcement.",
            "keywords": [topic, "science", "nature"],
        }

    from google import genai
    client = genai.Client(api_key=key)
    prompt = (
        f"Ecris un script pour un YouTube Short en francais sur : {topic}.\n"
        "Structure NARRATIVE obligatoire (storytelling, PAS une liste de faits) :\n"
        "1) HOOK (0-3s) : une phrase choc qui INVERSE une croyance ou pose une question "
        "extreme. Cree un vide d'information. Ton exclamatif. Ex: "
        "'La lave n'est MEME PAS ce qui te tue en premier dans un volcan.'\n"
        "2) TENSION (3-8s) : 1 phrase qui accentue le mystere et retarde la reponse. Ex: "
        "'En realite, la plupart des victimes ne touchent jamais la lave.'\n"
        "3) BODY (8-35s) : 2 a 4 phrases qui expliquent le phenomene, avec des mots "
        "sensoriels et forts (fondre, pulveriser, explosion). Chaque phrase apporte une "
        "info nouvelle et marquante.\n"
        "4) REVELATION (35-45s) : 1 phrase avec le fait le plus fascinant/choquant.\n"
        "5) LOOP (45-50s) : 1 phrase de conclusion qui s'enchaine logiquement avec le HOOK "
        "(pour donner envie de revoir la video).\n"
        "REGLES : phrases COURTES, une idee par phrase. Tutoiement. AUCUN 'salut', "
        "'aujourd'hui', 'bienvenue'. Rentre direct dans le sujet. Style parle et rythme.\n"
        "Reponds UNIQUEMENT en JSON strict avec les cles : "
        "title (string accrocheur), hook, tension, body, revelation, loop (strings), "
        "keywords (liste de 3 mots-cles anglais pour chercher des videos stock).\n"
        "Pas de texte hors du JSON."
    )
    resp = client.models.generate_content(model="gemini-flash-latest", contents=prompt)
    raw = resp.text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(raw)
    print(f"[script] genere : {data['title']}")
    return data


# ---------------------------------------------------------------- 2. voix
async def _tts(text, audio_path, rate, pitch, volume):
    """edge-tts : ecrit l'audio avec un ton donne (rate/pitch/volume)."""
    import edge_tts
    comm = edge_tts.Communicate(text, VOICE, rate=rate, pitch=pitch, volume=volume)
    with open(audio_path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])


def _silence(dur: float) -> Path:
    p = WORK / f"sil_{dur}.mp3"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-t", f"{dur}",
                    "-i", "anullsrc=r=24000:cl=mono", str(p)],
                   check=True, capture_output=True)
    return p


def make_voice(segments: list[dict]):
    """Genere la voix segment par segment (chacun son ton), avec micro-pause
    avant la revelation. Renvoie (audio_path, timeline).

    timeline = liste ordonnee d'items :
      {"kind":"speech","text","tone","dur"} ou {"kind":"pause","dur"}
    """
    files, timeline = [], []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        tone = seg.get("tone", "body")
        if tone == "revelation":                      # beat dramatique avant la revelation
            files.append(_silence(PAUSE_BEFORE_REVELATION))
            timeline.append({"kind": "pause", "dur": PAUSE_BEFORE_REVELATION})
        f = WORK / f"seg_{len(files)}.mp3"
        rate, pitch, vol = TONES.get(tone, TONES["body"])
        asyncio.run(_tts(text, f, rate, pitch, vol))
        files.append(f)
        timeline.append({"kind": "speech", "text": text, "tone": tone, "dur": _duration(f)})

    # concatenation robuste (filtre concat, re-encode) : gere les params differents
    inputs = []
    for f in files:
        inputs += ["-i", str(f)]
    filt = "".join(f"[{i}:a]" for i in range(len(files))) + f"concat=n={len(files)}:v=0:a=1[a]"
    audio = WORK / "voice.mp3"
    subprocess.run(["ffmpeg", "-y", *inputs, "-filter_complex", filt, "-map", "[a]", str(audio)],
                   check=True, capture_output=True)
    total = sum(it["dur"] for it in timeline)
    print(f"[voix] {len([t for t in timeline if t['kind']=='speech'])} segments, {total:.1f}s")
    return audio, timeline


# ---------------------------------------------------------------- 3. sous-titres
def _fmt_ass_time(t: float) -> str:
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _karaoke_lines(text, seg_start, seg_dur, style, group=3, pop_from=90):
    """Construit les Dialogue karaoke pour un segment (hook ou corps)."""
    words = text.replace(",", "").replace(".", "").split()
    if not words:
        return []
    groups = [words[i:i + group] for i in range(0, len(words), group)]
    char_total = sum(len(w) + 1 for w in words) or 1
    span = seg_dur / char_total
    out, t = [], seg_start
    for g in groups:
        dur = sum(len(w) + 1 for w in g) * span
        start, end = t, t + dur
        t = end
        parts = [rf"{{\kf{max(1, round((len(w) + 1) * span * 100))}}}{w.upper()}" for w in g]
        txt = (rf"{{\fscx{pop_from}\fscy{pop_from}\t(0,110,\fscx100\fscy100)}}"
               + " ".join(parts))
        out.append(f"Dialogue: 0,{_fmt_ass_time(start)},{_fmt_ass_time(end)},{style},{txt}")
    return out


def build_subtitles(timeline, total, ass_path, offset=0.0):
    """ASS karaoke pilote par la timeline de la voix (sync parfaite avec l'audio).

    Hook en gros/centre, reste en bas. Les pauses avancent le temps sans texte.
    """
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Def,{FONT},118,&H0000E5FF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,1,0,1,7,4,2,90,90,640,1
Style: Hook,{FONT},142,&H0000E5FF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,1,0,1,9,5,5,80,80,0,1
Style: Outro,{FONT},104,&H0000E5FF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,2,0,1,8,5,5,140,140,0,1

[Events]
Format: Layer, Start, End, Style, Text
"""
    lines, t = [], offset
    for it in timeline:
        if it["kind"] == "pause":
            t += it["dur"]
            continue
        # le hook s'affiche en gros et centre ; le reste en bas
        style = "Hook" if it["tone"] == "hook" else "Def"
        pop = 75 if style == "Hook" else 90
        lines += _karaoke_lines(it["text"], t, it["dur"], style, group=3, pop_from=pop)
        t += it["dur"]

    # Outro
    outro_start = t + 0.15
    outro_txt = (r"{\fad(350,400)\an5\fscx70\fscy70"
                 r"\t(0,400,\fscx100\fscy100)}" + OUTRO_TEXT)
    lines.append(f"Dialogue: 1,{_fmt_ass_time(outro_start)},{_fmt_ass_time(total - 0.1)},Outro,{outro_txt}")

    ass_path.write_text(header + "\n".join(lines), encoding="utf-8")
    print(f"[subs] {sum(1 for i in timeline if i['kind']=='speech')} segments karaoke + outro -> {ass_path.name}")


# ---------------------------------------------------------------- 4. visuels
def fetch_clips(keywords: list[str], n: int = 4) -> list[Path]:
    key = os.getenv("PEXELS_API_KEY")
    if not key:
        print("[visuels] pas de PEXELS_API_KEY -> fond noir")
        return []
    clips = []
    for kw in keywords:
        if len(clips) >= n:
            break
        r = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": key},
            params={"query": kw, "orientation": "portrait", "per_page": 3, "size": "medium"},
            timeout=30,
        )
        for vid in r.json().get("videos", []):
            files = [f for f in vid["video_files"] if f.get("height", 0) >= 1080]
            best = min(files or vid["video_files"], key=lambda f: abs(f.get("height", 0) - 1920))
            dst = WORK / f"clip_{len(clips)}.mp4"
            with requests.get(best["link"], stream=True, timeout=60) as s:
                with open(dst, "wb") as f:
                    for c in s.iter_content(1 << 16):
                        f.write(c)
            clips.append(dst)
            if len(clips) >= n:
                break
    print(f"[visuels] {len(clips)} clips telecharges")
    return clips


# ---------------------------------------------------------------- 5. montage
def _duration(path: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nk=1:nw=1", str(path)])
    return float(out.strip())


def pick_music() -> Path | None:
    if not MUSIC_DIR.exists():
        return None
    tracks = [p for p in MUSIC_DIR.iterdir() if p.suffix.lower() in (".mp3", ".m4a", ".wav")]
    if not tracks:
        return None
    return random.choice(tracks)


def assemble(audio: Path, ass: Path, clips: list[Path], out: Path, total: float):
    ass_esc = str(ass).replace("\\", "/").replace(":", "\\:")  # echappement filtre ffmpeg
    lead_ms = int(LEAD * 1000)
    music = pick_music()

    # --- Fond video (clips ou noir), couvrant tout le total
    if clips:
        per = total / len(clips) + 0.6
        parts = []
        for i, c in enumerate(clips):
            p = WORK / f"seg_{i}.mp4"
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(c), "-t", f"{per:.2f}",
                 "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1",
                 "-an", "-r", "30", str(p)],
                check=True, capture_output=True)
            parts.append(p)
        concat = WORK / "list.txt"
        concat.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
        bg = WORK / "bg.mp4"
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
                        "-c", "copy", str(bg)], check=True, capture_output=True)
        vid_in = ["-i", str(bg)]
    else:
        vid_in = ["-f", "lavfi", "-t", f"{total:.2f}", "-i", f"color=c=black:s={W}x{H}:r=30"]

    # --- Chaine video : sous-titres + fondu (ouverture tres courte pour ne pas perdre le viewer)
    vchain = (f"[0:v]subtitles='{ass_esc}':fontsdir=fonts,"
              f"fade=t=in:st=0:d=0.2,fade=t=out:st={total - 0.6:.2f}:d=0.6[v]")

    # --- Chaine audio : voix decalee + compressee/boostee (elle claque) + musique
    inputs = ["-i", str(audio)]                       # input 1 = voix
    voc = (f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo,"
           f"adelay={lead_ms}|{lead_ms},apad,atrim=0:{total:.2f},"
           f"acompressor=threshold=0.08:ratio=4:attack=5:release=180,"
           f"volume=1.6,alimiter=limit=0.95")
    if music:
        inputs += ["-stream_loop", "-1", "-i", str(music)]   # input 2 = musique (bouclee)
        achain = (
            # la voix est dupliquee : une copie pour le mix, une pour le sidechain
            voc + ",asplit=2[voc][vocsc];"
            f"[2:a]aformat=sample_rates=44100:channel_layouts=stereo,"
            f"atrim=0:{total:.2f},volume={MUSIC_VOL},"
            f"afade=t=in:st=0:d=1.2,afade=t=out:st={total - 1.6:.2f}:d=1.6[mus0];"
            # ducking : la musique baisse quand la voix parle
            f"[mus0][vocsc]sidechaincompress=threshold=0.03:ratio=8:attack=5:release=280[mus];"
            f"[voc][mus]amix=inputs=2:duration=first:normalize=0[mix];"
            # loudness au niveau des plateformes (~-14 LUFS) = ca sonne fort et pro
            f"[mix]loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
        )
        print(f"[musique] {music.name}")
    else:
        achain = voc + ",loudnorm=I=-14:TP=-1.5:LRA=11[aout]"
        print("[musique] aucune (depose un .mp3 dans music/)")

    OUT_DIR.mkdir(exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", *vid_in, *inputs,
         "-filter_complex", vchain + ";" + achain,
         "-map", "[v]", "-map", "[aout]", "-t", f"{total:.2f}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)],
        check=True, capture_output=True)
    print(f"[montage] OK ({total:.1f}s) -> {out}")


# ---------------------------------------------------------------- main
def main():
    topic = sys.argv[1] if len(sys.argv) > 1 else random.choice(DEFAULT_TOPICS)
    print(f"=== Sujet : {topic} ===")
    script = make_script(topic)
    # ordre narratif : hook -> tension -> body -> revelation -> loop
    segments = [{"text": script.get(k, ""), "tone": k}
                for k in ("hook", "tension", "body", "revelation", "loop")]
    narration = " ".join(s["text"] for s in segments if s["text"])

    audio, timeline = make_voice(segments)
    voice_dur = sum(it["dur"] for it in timeline)
    total = LEAD + voice_dur + TAIL          # ~0.3s + voix + outro
    ass = WORK / "subs.ass"
    build_subtitles(timeline, total, ass, offset=LEAD)
    clips = fetch_clips(script.get("keywords", [topic]))

    out = OUT_DIR / "short.mp4"
    assemble(audio, ass, clips, out, total)

    # Sauve les metadonnees pour l'upload
    (OUT_DIR / "meta.json").write_text(
        json.dumps({"title": script["title"] + " #Shorts",
                    "description": narration + "\n\n#Shorts #shorts",
                    "tags": (script.get("keywords", []) + ["shorts"])}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print("\nTermine. Verifie output/short.mp4")


if __name__ == "__main__":
    main()
