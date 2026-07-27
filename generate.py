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
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
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
# --- Voix : Google Cloud TTS (Chirp 3 HD, tres naturel) si cle presente, sinon edge-tts.
VOICE = os.getenv("VOICE", "fr-CA-AntoineNeural")   # voix edge-tts (configurable via .env)
GOOGLE_VOICE = "fr-FR-Chirp3-HD-Charon"        # voix Google Chirp 3 HD (masculine, naturelle)
# Ton edge-tts (rate, pitch, volume) par segment.
# Pitch volontairement FAIBLE : de gros decalages rendent la voix robotique.
TONES = {
    "hook":       ("+9%",  "+3Hz", "+0%"),   # energie via le rythme, pas le pitch
    "tension":    ("+2%",  "+1Hz", "+0%"),
    "body":       ("+2%",  "+0Hz", "+0%"),
    "revelation": ("-4%",  "+1Hz", "+0%"),
    "loop":       ("+4%",  "+0Hz", "+0%"),
}
# Ton Google (speakingRate, volumeGainDb) par segment (Chirp 3 HD ne gere pas le pitch).
GTONES = {
    "hook":       (1.15, 4.0),
    "tension":    (1.03, 1.0),
    "body":       (1.00, 0.0),
    "revelation": (0.95, 2.0),
    "loop":       (1.06, 1.0),
}
# Kokoro (open-source local). FR natif = ff_siwis (feminine). Voix masculine configurable
# via .env KOKORO_VOICE (les voix masculines viennent d'autres langues -> accent possible).
KOKORO_VOICE = os.getenv("KOKORO_VOICE", "ff_siwis")
KOKORO_SPEED = {"hook": 1.12, "tension": 1.0, "body": 1.0, "revelation": 0.92, "loop": 1.05}
# Moteur voix : "kokoro" | "google" | "edge" (via .env TTS_ENGINE ; defaut edge)
TTS_ENGINE = os.getenv("TTS_ENGINE", "edge").lower()
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
def _load_topics():
    """Charge la grande liste de sujets depuis topics.txt (1 par ligne, # = commentaire)."""
    f = Path(__file__).with_name("topics.txt")
    if f.exists():
        t = [l.strip() for l in f.read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.strip().startswith("#")]
        if t:
            return t
    return ["les trous noirs", "le cerveau humain", "les requins", "les volcans",
            "l'Egypte antique", "les pieuvres", "l'espace", "les reves"]


DEFAULT_TOPICS = _load_topics()


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
            "emphasis": ["FAUX", "flippante", "hallucinants", "fou"],
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
        "keywords (liste de 3 mots-cles anglais pour chercher des videos stock), "
        "emphasis (liste des 6 a 10 mots LES PLUS importants du script a mettre en valeur "
        "a l'ecran : chiffres, mots choc, mots sensoriels — extraits tels quels du texte).\n"
        "Pas de texte hors du JSON."
    )
    resp = client.models.generate_content(model="gemini-flash-latest", contents=prompt)
    raw = resp.text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(raw)
    print(f"[script] genere : {data['title']}")
    return data


# ---------------------------------------------------------------- 2. voix
async def _edge_tts(text, audio_path, rate, pitch, volume):
    """edge-tts : ecrit l'audio avec un ton donne (rate/pitch/volume)."""
    import edge_tts
    comm = edge_tts.Communicate(text, VOICE, rate=rate, pitch=pitch, volume=volume)
    with open(audio_path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])


def _google_tts(text, audio_path, tone):
    """Google Cloud TTS (Chirp 3 HD) via REST + cle API. Voix tres naturelle."""
    import base64
    rate, gain = GTONES.get(tone, GTONES["body"])
    r = requests.post(
        "https://texttospeech.googleapis.com/v1/text:synthesize",
        params={"key": os.environ["GOOGLE_TTS_API_KEY"]},
        json={
            "input": {"text": text},
            "voice": {"languageCode": "fr-FR", "name": GOOGLE_VOICE},
            "audioConfig": {"audioEncoding": "LINEAR16", "speakingRate": rate, "volumeGainDb": gain},
        },
        timeout=30,
    )
    r.raise_for_status()
    audio_path.write_bytes(base64.b64decode(r.json()["audioContent"]))


_kokoro_pipe = None


def _kokoro_tts(text, wav_path, tone):
    """Kokoro (open-source, local) -> WAV pur (aucun re-encodage, voix propre)."""
    global _kokoro_pipe
    import numpy as np
    import soundfile as sf
    if _kokoro_pipe is None:
        from kokoro import KPipeline
        _kokoro_pipe = KPipeline(lang_code="f")     # 'f' = francais
    speed = KOKORO_SPEED.get(tone, 1.0)
    chunks = []
    for _, _, audio in _kokoro_pipe(text, voice=KOKORO_VOICE, speed=speed):
        chunks.append(audio.numpy() if hasattr(audio, "numpy") else np.asarray(audio))
    data = np.concatenate(chunks) if chunks else np.zeros(1, dtype="float32")
    sf.write(str(wav_path), data, 24000)


def _synth(text, wav_path, tone):
    """Ecrit un WAV pour un segment. Moteur selon TTS_ENGINE. Fallback edge-tts."""
    try:
        if TTS_ENGINE == "kokoro":
            _kokoro_tts(text, wav_path, tone)
            return
        if TTS_ENGINE == "google" or os.getenv("GOOGLE_TTS_API_KEY"):
            _google_tts(text, wav_path, tone)
            return
    except Exception as e:
        print(f"[voix] moteur {TTS_ENGINE} indispo ({e}) -> edge-tts")
    rate, pitch, vol = TONES.get(tone, TONES["body"])
    tmp = wav_path.with_suffix(".mp3")
    asyncio.run(_edge_tts(text, tmp, rate, pitch, vol))
    subprocess.run(["ffmpeg", "-y", "-i", str(tmp), str(wav_path)], check=True, capture_output=True)


def _silence(dur: float) -> Path:
    p = WORK / f"sil_{dur}.wav"
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
        f = WORK / f"seg_{len(files)}.wav"
        _synth(text, f, tone)
        files.append(f)
        timeline.append({"kind": "speech", "text": text, "tone": tone, "dur": _duration(f)})

    # concatenation en WAV (lossless) : gere les params differents
    inputs = []
    for f in files:
        inputs += ["-i", str(f)]
    filt = "".join(f"[{i}:a]" for i in range(len(files))) + f"concat=n={len(files)}:v=0:a=1[a]"
    audio = WORK / "voice.wav"
    subprocess.run(["ffmpeg", "-y", *inputs, "-filter_complex", filt, "-map", "[a]", str(audio)],
                   check=True, capture_output=True)
    total = sum(it["dur"] for it in timeline)
    print(f"[voix] {len([t for t in timeline if t['kind']=='speech'])} segments, {total:.1f}s")
    return audio, timeline


# ---------------------------------------------------------------- 3. sous-titres
def _fmt_ass_time(t: float) -> str:
    h = int(t // 3600); m = int(t % 3600 // 60); s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


_whisper = None
_EMPH_COLOR = r"&H0000A5FF&"      # orange (ASS &HBBGGRR) pour les mots importants
_SUNG_COLOR = r"&H0000F0FF&"      # jaune (couleur "chantee" normale)


def align_words(audio_path, language="fr"):
    """Timestamps mot par mot via faster-whisper (alignement force = sync precise)."""
    global _whisper
    from faster_whisper import WhisperModel
    if _whisper is None:
        _whisper = WhisperModel("small", device="cpu", compute_type="int8")
    segments, _ = _whisper.transcribe(str(audio_path), language=language, word_timestamps=True)
    out = []
    for seg in segments:
        for w in (seg.words or []):
            t = w.word.strip()
            if t:
                out.append({"word": t, "start": float(w.start), "end": float(w.end)})
    return out


def _emph_set(emphasis):
    s = set()
    for e in (emphasis or []):
        for w in re.findall(r"\w+", str(e).lower()):
            if len(w) > 1:
                s.add(w)
    return s


def _aligned_line(g, style, base_fs, emph, offset):
    """Une ligne de sous-titres depuis des mots alignes (timing reel + mots importants)."""
    start = offset + g[0]["start"]
    end = offset + g[-1]["end"] + 0.05
    emph_fs = int(base_fs * 1.32)
    toks = []
    for j, w in enumerate(g):
        d = (g[j + 1]["start"] - w["start"]) if j < len(g) - 1 else (w["end"] - w["start"])
        cs = max(1, round(d * 100))
        disp = re.sub(r"""[.,!?;:"']""", "", w["word"]).upper()
        norm = re.sub(r"[^\w]", "", w["word"].lower())
        if norm in emph:                       # mot important : plus gros + orange
            toks.append(rf"{{\kf{cs}\fs{emph_fs}\1c{_EMPH_COLOR}}}{disp}{{\fs{base_fs}\1c{_SUNG_COLOR}}}")
        else:
            toks.append(rf"{{\kf{cs}}}{disp}")
    anim = r"{\fad(45,0)\fscx64\fscy64\t(0,95,\fscx112\fscy112)\t(95,180,\fscx100\fscy100)}"
    return f"Dialogue: 0,{_fmt_ass_time(start)},{_fmt_ass_time(end)},{style},{anim + ' '.join(toks)}"


def _karaoke_lines(text, seg_start, seg_dur, style, group=3):
    """Fallback si l'alignement echoue : timing estime au prorata des caracteres."""
    words = text.replace(",", "").replace(".", "").split()
    if not words:
        return []
    groups = [words[i:i + group] for i in range(0, len(words), group)]
    char_total = sum(len(w) + 1 for w in words) or 1
    span = seg_dur / char_total
    out, t = [], seg_start
    for g in groups:
        dur = sum(len(w) + 1 for w in g) * span
        parts = [rf"{{\kf{max(1, round((len(w) + 1) * span * 100))}}}{w.upper()}" for w in g]
        anim = r"{\fad(45,0)\fscx64\fscy64\t(0,95,\fscx112\fscy112)\t(95,180,\fscx100\fscy100)}"
        out.append(f"Dialogue: 0,{_fmt_ass_time(t)},{_fmt_ass_time(t + dur)},{style},{anim + ' '.join(parts)}")
        t += dur
    return out


def build_subtitles(timeline, words, total, ass_path, emphasis=None, offset=0.0, group=3):
    """Sous-titres karaoke. Si `words` (alignes) fournis -> sync mot par mot + mots
    importants mis en valeur. Sinon fallback sur le timing estime de la timeline."""
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Def,{FONT},130,&H0000F0FF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,2,0,1,8,5,2,90,90,610,1
Style: Hook,{FONT},156,&H0000F0FF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,2,0,1,11,6,5,90,90,0,1
Style: Outro,{FONT},110,&H0000F0FF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,2,0,1,9,6,5,140,140,0,1

[Events]
Format: Layer, Start, End, Style, Text
"""
    emph = _emph_set(emphasis)
    hook_dur = timeline[0]["dur"] if timeline and timeline[0].get("kind") == "speech" else 0.0
    lines = []

    if words:                                   # --- sync mot par mot (aligne)
        for i in range(0, len(words), group):
            g = words[i:i + group]
            style = "Hook" if g[0]["start"] < hook_dur else "Def"
            base_fs = 156 if style == "Hook" else 130
            lines.append(_aligned_line(g, style, base_fs, emph, offset))
        last_end = offset + words[-1]["end"]
        mode = "aligne"
    else:                                       # --- fallback timing estime
        t = offset
        for it in timeline:
            if it["kind"] == "pause":
                t += it["dur"]
                continue
            style = "Hook" if it["tone"] == "hook" else "Def"
            lines += _karaoke_lines(it["text"], t, it["dur"], style, group=group)
            t += it["dur"]
        last_end = t
        mode = "estime"

    outro_start = last_end + 0.15
    outro_txt = (r"{\fad(350,400)\an5\fscx70\fscy70"
                 r"\t(0,400,\fscx100\fscy100)}" + OUTRO_TEXT)
    lines.append(f"Dialogue: 1,{_fmt_ass_time(outro_start)},{_fmt_ass_time(total - 0.1)},Outro,{outro_txt}")

    ass_path.write_text(header + "\n".join(lines), encoding="utf-8")
    print(f"[subs] {len(lines) - 1} lignes ({mode}) + outro -> {ass_path.name}")


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
            params={"query": kw, "orientation": "portrait", "per_page": 5, "size": "large"},
            timeout=30,
        )
        for vid in r.json().get("videos", []):
            files = vid["video_files"]
            hd = [f for f in files if f.get("height", 0) >= 1920]
            # au moins 1920px de haut si dispo (net apres crop), sinon le plus grand
            best = min(hd, key=lambda f: f["height"]) if hd else max(files, key=lambda f: f.get("height", 0))
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
        bw, bh = int(W * 1.25), int(H * 1.25)     # source un peu plus grande pour zoomer sans perte
        parts = []
        for i, c in enumerate(clips):
            p = WORK / f"seg_{i}.mp4"
            # Ken Burns : zoom lent, alterne avant (pairs) / arriere (impairs)
            z = "min(1+0.0010*on,1.22)" if i % 2 == 0 else "max(1.22-0.0010*on,1.0)"
            vf = (f"scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},"
                  f"zoompan=z='{z}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                  f"s={W}x{H}:fps=30,setsar=1")
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(c), "-t", f"{per:.2f}",
                 "-vf", vf, "-an", "-r", "30",
                 "-c:v", "libx264", "-crf", "16", "-preset", "medium", "-pix_fmt", "yuv420p",
                 str(p)],
                check=True, capture_output=True)
            parts.append(p)
        concat = WORK / "list.txt"
        concat.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts))
        bg = WORK / "bg.mp4"
        # re-encodage (PAS -c copy) : force des timestamps continus (cfr) sinon
        # le zoompan casse les PTS et l'audio est tronque au montage final.
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
                        "-c:v", "libx264", "-crf", "16", "-preset", "medium",
                        "-pix_fmt", "yuv420p", "-r", "30", "-vsync", "cfr", str(bg)],
                       check=True, capture_output=True)
        vid_in = ["-i", str(bg)]
        # frame PROPRE (sans sous-titres) pour la miniature
        subprocess.run(["ffmpeg", "-y", "-ss", f"{total * 0.4:.2f}", "-i", str(bg),
                        "-frames:v", "1", str(OUT_DIR / "_thumbframe.jpg")], capture_output=True)
    else:
        vid_in = ["-f", "lavfi", "-t", f"{total:.2f}", "-i", f"color=c=black:s={W}x{H}:r=30"]

    # --- Chaine video : sous-titres + fondu (ouverture tres courte pour ne pas perdre le viewer)
    vchain = (f"[0:v]subtitles='{ass_esc}':fontsdir=fonts,"
              f"fade=t=in:st=0:d=0.2,fade=t=out:st={total - 0.6:.2f}:d=0.6[v]")

    # --- Chaine audio : voix decalee + compressee/boostee (elle claque) + musique
    inputs = ["-i", str(audio)]                       # input 1 = voix
    # voix BRUTE (aucun filtre) : juste decalage d'intro + longueur. YouTube normalise le volume a la lecture.
    voc = (f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo,"
           f"adelay={lead_ms}|{lead_ms},apad,atrim=0:{total:.2f}")
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
            f"[voc][mus]amix=inputs=2:duration=first:normalize=0[aout]"
        )
        print(f"[musique] {music.name}")
    else:
        achain = voc + "[aout]"
        print("[musique] aucune (depose un .mp3 dans music/)")

    OUT_DIR.mkdir(exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", *vid_in, *inputs,
         "-filter_complex", vchain + ";" + achain,
         "-map", "[v]", "-map", "[aout]", "-t", f"{total:.2f}",
         "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", str(out)],
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
    try:
        words = align_words(audio)           # sync mot par mot (faster-whisper)
        print(f"[align] {len(words)} mots alignes")
    except Exception as e:
        print(f"[align] echec ({e}) -> timing estime")
        words = None
    ass = WORK / "subs.ass"
    build_subtitles(timeline, words, total, ass, emphasis=script.get("emphasis"), offset=LEAD)
    clips = fetch_clips(script.get("keywords", [topic]), n=8)

    out = OUT_DIR / "short.mp4"
    assemble(audio, ass, clips, out, total)

    meta = {"title": script["title"] + " #Shorts",
            "description": narration + "\n\n#Shorts #shorts",
            "tags": (script.get("keywords", []) + ["shorts"]),
            "topic": topic,
            "created": datetime.now().isoformat(timespec="seconds")}
    (OUT_DIR / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    archive(out, meta)
    print("\nTermine. Verifie output/short.mp4")


def make_thumbnail(title: str, out_jpg: Path):
    """Miniature : frame PROPRE (output/_thumbframe.jpg, sans sous-titres) + titre centre."""
    words = title.replace("#Shorts", "").replace("#shorts", "").strip().upper().split()
    lines = [" ".join(words[i:i + 3]) for i in range(0, len(words), 3)][:4]  # ~3 mots/ligne, max 4
    ttxt = OUT_DIR / "_tt.txt"
    ttxt.write_text("\n".join(lines), encoding="utf-8", newline="\n")        # \n propre (pas \r\n)

    frame = OUT_DIR / "_thumbframe.jpg"
    if frame.exists():
        src = ["-i", "output/_thumbframe.jpg"]
    else:                                                                   # fallback : fond degrade sombre
        src = ["-f", "lavfi", "-i", f"color=c=0x0A1428:s={W}x{H}"]
    vf = ("eq=brightness=-0.08:contrast=1.05,"                              # assombrit -> texte lisible
          "drawtext=fontfile=fonts/Anton-Regular.ttf:textfile=output/_tt.txt:"
          "fontcolor=white:borderw=16:bordercolor=black:fontsize=132:"
          "line_spacing=18:x=(w-text_w)/2:y=(h-text_h)/2")                  # bloc centre
    subprocess.run(["ffmpeg", "-y", *src, "-frames:v", "1", "-vf", vf, str(out_jpg)],
                   capture_output=True)
    ttxt.unlink(missing_ok=True)


def archive(video: Path, meta: dict):
    """Copie la video + meta + vignette + miniature dans output/lib/<date>_<slug>/."""
    slug = re.sub(r"[^a-z0-9]+", "-", meta["topic"].lower()).strip("-")[:40] or "video"
    folder = OUT_DIR / "lib" / f"{datetime.now():%Y%m%d_%H%M%S}_{slug}"
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copy2(video, folder / "short.mp4")
    (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    # vignette (pour la galerie de l'app)
    subprocess.run(["ffmpeg", "-y", "-ss", "1", "-i", str(video), "-frames:v", "1",
                    "-vf", "scale=360:-1", str(folder / "poster.jpg")], capture_output=True)
    # miniature YouTube (frame propre + titre)
    make_thumbnail(meta.get("title", ""), folder / "thumbnail.jpg")
    print(f"[galerie] archive -> {folder}")


if __name__ == "__main__":
    main()
