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
import math
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
# Rythme SOUTENU (guide hook) : debit eleve = plus d'infos/seconde = meilleure retention.
# Le pitch reste faible (un gros decalage rend la voix robotique).
TONES = {
    "hook":       ("+22%", "+5Hz", "+0%"),   # attaque rapide et energique
    "tension":    ("+2%",  "-2Hz", "+0%"),   # TWIST : on ralentit et on descend -> revelation
    "body":       ("+14%", "+0Hz", "+0%"),   # debit rapide mais intelligible
    "revelation": ("-2%",  "+3Hz", "+0%"),   # chute : encore plus lent, ton qui remonte
    "loop":       ("+14%", "+0Hz", "+0%"),
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
# Modeles Gemini essayes dans l'ordre (quota gratuit = 20 requetes/jour PAR modele)
GEMINI_MODELS = ["gemini-flash-latest", "gemini-2.0-flash", "gemini-flash-lite-latest",
                 "gemini-2.0-flash-lite"]
PAUSE_BEFORE_REVELATION = 0.32       # micro-pause dramatique avant la revelation finale
PAUSE_BEFORE_TENSION = 0.26          # pause avant le TWIST (moment ou on nomme le sujet)
W, H = 1080, 1920                    # format vertical Short
FONT = "Anton"                       # police des sous-titres (fichier dans fonts/)

LEAD = 0.0                           # la voix demarre a la SECONDE 0, aucun temps mort
TAIL = 2.0                           # petite outro pour ne pas couper net
CUT_MIN, CUT_MAX = 2.2, 3.6          # duree d'un plan (guide : 1 jump cut toutes les 2-4 s)
# --- Hook VISUEL : 60% des vues sont sans son -> l'image doit accrocher seule.
HOOK_WINDOW = 3.2                    # duree de la zone "hook" traitee a part
HOOK_CUT_MIN, HOOK_CUT_MAX = 0.45, 0.7  # coupes tres rapides = pattern interrupt permanent
HOOK_PUNCH = True                    # snap zoom sur CHAQUE plan du hook
HOOK_PUNCH_FROM = 1.55               # zoom de depart du punch d'ouverture
HOOK_SHAKE = True                    # secousse camera sur le 1er plan (synchro avec le boom)
HOOK_FLASH = True                    # flash blanc bref a chaque coupe du hook
HOOK_GRADE = ("eq=contrast=1.18:saturation=1.30:brightness=0.02,"
              "unsharp=5:5:0.8")     # image plus percutante + nettete accrue
# --- VFX du hook (spectacle des 3 premieres secondes)
VFX_GLITCH = True                    # aberration chromatique RGB a 0s (effet glitch cinema)
VFX_GLITCH_AMP = 22                  # amplitude du decalage RGB en pixels
VFX_DUTCH = True                     # dutch angle : plans inclines = instabilite/urgence
VFX_DUTCH_DEG = 5.0                  # inclinaison en degres
VFX_VIGNETTE = True                  # vignettage sur le hook : focalise le regard au centre
HOOK_WORDS_PER_LINE = 2              # hook : 2 mots max a l'ecran (lecture instantanee)
# --- Images IA pour le hook (Pollinations : gratuit, sans cle, illimite)
AI_IMAGES = True                     # False -> uniquement du stock Pexels
AI_IMAGE_COUNT = 3                   # nb d'images generees pour les premiers plans
AI_IMAGE_TIMEOUT = 90
AI_PUNCH_FROM = 1.22                 # zoom d'ouverture reduit sur les images (576x1024 natif)
AI_IMAGE_STYLE = ("cinematic vertical shot, dramatic moody lighting, hyper realistic, "
                  "shallow depth of field, film grain, high contrast, 9:16")
SFX_HOOK = True                      # sound design du hook (boom + whoosh + riser)
SFX_WHOOSH_AT = 0.5                  # whoosh cale sur le snap zoom
SFX_CUT_AT, SFX_CUT_DUR = 1.5, 0.2   # coupure totale de la musique = vide auditif
SFX_RISER_AT = 1.7                   # riser d'urgence juste apres le vide
# Coupe des blancs : seuil -30dB (le "silence" du TTS n'est pas totalement muet),
# ne garde que 0.08s de respiration, agit des 0.10s de pause -> debit serre.
SILENCE_FILTER = ("silenceremove=start_periods=1:start_threshold=-30dB:start_silence=0.03:"
                  "stop_periods=-1:stop_threshold=-30dB:stop_silence=0.08:stop_duration=0.10")
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
        f"Tu es un expert des YouTube Shorts viraux. Ecris un script en francais sur : {topic}.\n"
        "\n"
        "=== LE HOOK EST LA PRIORITE ABSOLUE (0 a 3 secondes) ===\n"
        "Il doit STOPPER LE SCROLL en activant au moins 2 de ces 5 leviers psychologiques :\n"
        "  - CURIOSITE : creer un manque d'information que le cerveau VEUT combler.\n"
        "  - SURPRISE/CONTRASTE : idee contre-intuitive qui casse une croyance.\n"
        "  - URGENCE : sentiment de rater quelque chose d'important.\n"
        "  - BENEFICE : promesse concrete et immediate.\n"
        "  - PREUVE SOCIALE : chiffre, pourcentage, ce que font/ignorent les autres.\n"
        "\n"
        "*** REGLE ABSOLUE : L'INFORMATION GAP ***\n"
        f"Le hook ne doit JAMAIS nommer le sujet ({topic}). Interdiction de dire le mot.\n"
        "Utilise des POINTEURS : 'cet objet', 'ce truc', 'cette chose', 'ce detail', 'ca'.\n"
        "Le spectateur doit finir le hook SANS savoir de quoi on parle, mais en ayant "
        "desesperement besoin de le decouvrir.\n"
        "Si le cerveau devine le sujet en moins de 1,5 seconde, il swipe. La predictibilite TUE.\n"
        "  MAUVAIS (35% de retention) : 'Sans les satellites, ta carte bancaire ne marche plus.'\n"
        "  EXCELLENT (82%) : 'Les banques prient chaque matin pour qu'une boite en metal "
        "a 20 000 km ne tombe pas en panne.'\n"
        "\n"
        "*** REGLE 2 : LA CONSEQUENCE AVANT LA CAUSE ***\n"
        "Commence par le desastre, la panique, le chiffre perdu — jamais par l'explication.\n"
        "  MAUVAIS : 'Un satellite a bugue donc les banques ont ferme.'\n"
        "  EXCELLENT : 'Toutes les banques du pays se sont effondrees en 1 seconde... "
        "a cause d'une erreur a 20 000 km.'\n"
        "\n"
        "Genere 3 hooks DIFFERENTS avec 3 formules distinctes parmi :\n"
        "  1. CHIFFRE CHOC + POINTEUR : 'Cinq milliards perdus chaque heure si CE truc s'arrete.'\n"
        "  2. PARADOXE ABSURDE : 'Ton compte en banque est maintenu en vie par une horloge "
        "qui flotte dans le vide.'\n"
        "  3. MENACE IMMINENTE : 'Regarde bien ta carte. Dans 24h elle peut devenir "
        "un bout de plastique inutile.'\n"
        "  4. COMPARAISON WTF : 'Cet appareil coute 100 millions et son seul boulot, "
        "c'est de verifier si tu as 2 euros.'\n"
        "  5. SECRET INTERDIT : 'Ce que personne ne veut que tu saches sur CE truc "
        "que tu utilises tous les jours.'\n"
        "  6. HYPOTHESE APOCALYPTIQUE : 'Si on eteignait cet objet 3 secondes, "
        "la civilisation s'effondrerait.'\n"
        "  7. DESTRUCTEUR DE SWIPE : 'Arrete de scroller. Ce truc au-dessus de ta tete "
        "decide de ton argent.'\n"
        "  8. DETAIL QUOTIDIEN CACHE : 'La prochaine fois que tu entends ce BEEP "
        "au supermarche, souviens-toi de ca.'\n"
        "\n"
        "REGLES DU HOOK : 8 a 16 mots. Present. Tutoiement. Ultra concret et sensoriel. "
        "INTERDIT : nommer le sujet, 'savais-tu', 'bienvenue', 'aujourd'hui', 'dans cette video'. "
        "Choisis LE MEILLEUR des 3 (celui qui cree la plus grosse dette de curiosite).\n"
        "\n"
        "=== STRUCTURE DU RESTE (storytelling, PAS une liste) ===\n"
        "TENSION (3-8s) : 1 phrase qui AGGRAVE le mystere. Tu peux enfin nommer le sujet ici, "
        "mais tu dois immediatement relancer une question plus grosse. Ne resous RIEN.\n"
        "BODY (8-30s) : 2 a 3 phrases COURTES. Chacune donne une micro-recompense "
        "(un indice qui valide l'attente) MAIS ouvre une nouvelle sous-question. "
        "Mots sensoriels violents : fondre, pulveriser, exploser, ecraser, devorer.\n"
        "REVELATION : LA reponse finale, le fait le plus choquant. C'est SEULEMENT ici "
        "que la tension se libere — jamais avant.\n"
        "LOOP : 1 phrase finale qui s'enchaine logiquement avec le HOOK (rewatch).\n"
        "\n"
        "REGLES GLOBALES : phrases TRES courtes (max 12 mots), une idee par phrase, "
        "rythme rapide, zero mot inutile, zero remplissage. Style parle.\n"
        "\n"
        "Reponds UNIQUEMENT en JSON strict avec les cles : "
        "title (string accrocheur), "
        "hook_variants (liste des 3 hooks generes), "
        "hook (le meilleur des 3, repete tel quel), "
        "tension, body, revelation, loop (strings), "
        "hook_keywords (liste de 3 prompts ANGLAIS pour illustrer les 2 premieres secondes. "
        "REGLE : l'image doit porter la MEME TENSION que le texte, jamais une image neutre "
        "ou paisible. Ajoute toujours l'emotion du hook (unsettling, oppressive, eerie, "
        "claustrophobic, menacing, surreal, violent) au sujet filme. "
        "MAUVAIS : 'person sleeping' pour un hook sur les hallucinations (trop paisible). "
        "BON : 'sleeping person trapped in dark surreal void, eerie, oppressive'. "
        "Objets/scenes filmables uniquement. Le PREMIER prompt illustre le tout debut du hook), "
        "keywords (liste de 3 mots-cles anglais pour le reste de la video), "
        "emphasis (liste des 6 a 10 mots LES PLUS importants du script a mettre en valeur "
        "a l'ecran : chiffres, mots choc, mots sensoriels — extraits tels quels du texte), "
        "virality (entier 0-100 : potentiel viral REEL et SEVERE du hook retenu. "
        "Note haut si : parle du spectateur lui-meme (ton corps, ton argent), provoque "
        "degout/peur/colere, detruit une croyance, chiffre hallucinant, sujet universel. "
        "Note bas si : abstrait, lointain, niche, deja vu, tiede. Sois exigeant, "
        "la moyenne doit tourner autour de 55), "
        "virality_reason (1 phrase COURTE justifiant la note).\n"
        "Pas de texte hors du JSON."
    )
    # Le quota gratuit est PAR MODELE (20 req/jour) : on bascule au suivant si epuise.
    resp = None
    for m in GEMINI_MODELS:
        try:
            resp = client.models.generate_content(model=m, contents=prompt)
            break
        except Exception as e:
            if any(x in str(e) for x in ("RESOURCE_EXHAUSTED", "429", "NOT_FOUND", "404")):
                print(f"[script] quota/modele {m} indispo -> suivant")
                continue
            raise
    if resp is None:
        raise RuntimeError("Tous les modeles Gemini sont epuises pour aujourd'hui.")
    raw = resp.text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(raw)
    # Gemini renvoie parfois une LISTE de phrases au lieu d'une string -> on aplatit.
    for k in ("title", "hook", "tension", "body", "revelation", "loop"):
        v = data.get(k)
        if isinstance(v, list):
            data[k] = " ".join(str(x).strip() for x in v if str(x).strip())
        elif v is not None and not isinstance(v, str):
            data[k] = str(v)
    print(f"[script] genere : {data['title']}")
    for i, h in enumerate(data.get("hook_variants", []), 1):
        mark = ">>" if h.strip() == data.get("hook", "").strip() else "  "
        print(f"  {mark} hook {i}: {h}")
    if data.get("virality") is not None:
        print(f"[viralite] {data['virality']}/100 - {data.get('virality_reason', '')}")
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


def _trim_silence(src: Path, dst: Path) -> Path:
    """Coupe les blancs (debut, fin et longues pauses internes) -> rythme serre.
    Le guide hook insiste : 'supprimer les blancs et l'inutile'."""
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-af", SILENCE_FILTER, str(dst)],
            check=True, capture_output=True)
        if dst.exists() and _duration(dst) > 0.25:      # garde-fou : on n'a pas tout coupe
            return dst
    except Exception:
        pass
    return src


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
        # beats dramatiques : avant le twist (on nomme enfin le sujet) et avant la chute
        gap = {"tension": PAUSE_BEFORE_TENSION,
               "revelation": PAUSE_BEFORE_REVELATION}.get(tone)
        if gap:
            files.append(_silence(gap))
            timeline.append({"kind": "pause", "dur": gap})
        raw = WORK / f"raw_{len(files)}.wav"
        _synth(text, raw, tone)
        f = _trim_silence(raw, WORK / f"seg_{len(files)}.wav")
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
# Couleurs ASS = &HBBGGRR (inverse du hexa web). Psychologie des couleurs du guide :
_EMPH_COLOR = r"&H006633FF&"      # rouge neon #FF3366 : mots chocs / danger
_NUM_COLOR = r"&H00CCFF00&"       # cyan #00FFCC : chiffres et dates
_SUNG_COLOR = r"&H0000FFFF&"      # jaune pur #FFFF00 : traite le plus vite par la retine


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
        if norm in emph:                       # mot choc : plus gros + rouge neon
            toks.append(rf"{{\kf{cs}\fs{emph_fs}\1c{_EMPH_COLOR}}}{disp}{{\fs{base_fs}\1c{_SUNG_COLOR}}}")
        elif re.search(r"\d", disp):           # chiffre / date : cyan (contraste max)
            toks.append(rf"{{\kf{cs}\1c{_NUM_COLOR}}}{disp}{{\1c{_SUNG_COLOR}}}")
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
Style: Def,{FONT},130,&H0000FFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,2,0,1,8,5,2,90,90,610,1
Style: Hook,{FONT},156,&H0000FFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,2,0,1,11,6,5,90,90,0,1
Style: Outro,{FONT},110,&H0000FFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,2,0,1,9,6,5,140,140,0,1

[Events]
Format: Layer, Start, End, Style, Text
"""
    emph = _emph_set(emphasis)
    hook_dur = timeline[0]["dur"] if timeline and timeline[0].get("kind") == "speech" else 0.0
    lines = []

    if words:                                   # --- sync mot par mot (aligne)
        i = 0
        while i < len(words):
            in_hook = words[i]["start"] < hook_dur
            # hook : 2 mots max au centre (lecture instantanee) ; corps : 3 mots
            step = HOOK_WORDS_PER_LINE if in_hook else group
            g = words[i:i + step]
            style = "Hook" if in_hook else "Def"
            base_fs = 156 if style == "Hook" else 130
            lines.append(_aligned_line(g, style, base_fs, emph, offset))
            i += step
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
_clip_seq = 0


def fetch_ai_images(prompts: list[str], n: int = 3) -> list[Path]:
    """Genere des images IA (Pollinations : gratuit, sans cle) pour illustrer le hook.
    Le stock footage generique 'hurle PUB' au cerveau ; une image sur mesure, non.
    Les plans du hook durant ~0.5s, l'animation (zoom/glitch) les rend indiscernables
    d'un vrai plan video."""
    global _clip_seq
    if not AI_IMAGES:
        return []
    import urllib.parse
    out = []
    for i, p in enumerate(prompts[:n]):
        full = f"{p}, {AI_IMAGE_STYLE}"
        url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(full)
               + f"?width=1080&height=1920&nologo=true&seed={random.randint(1, 99999)}")
        dst = WORK / f"ai_{_clip_seq}.jpg"
        _clip_seq += 1
        try:
            r = requests.get(url, timeout=AI_IMAGE_TIMEOUT)
            if r.status_code == 200 and "image" in r.headers.get("content-type", ""):
                dst.write_bytes(r.content)
                out.append(dst)
        except Exception:
            pass
    print(f"[visuels] [hook IA] {len(out)}/{min(n, len(prompts))} images generees")
    return out


def fetch_clips(keywords: list[str], n: int = 4, label: str = "") -> list[Path]:
    """Telecharge n clips verticaux Pexels. Prend au plus 2 clips par mot-cle pour
    que le resultat colle a TOUS les termes demandes (et pas seulement au premier)."""
    global _clip_seq
    key = os.getenv("PEXELS_API_KEY")
    if not key:
        print("[visuels] pas de PEXELS_API_KEY -> fond noir")
        return []
    clips, used = [], []
    per_kw = max(1, n // max(1, len(keywords)))
    for kw in keywords:
        if len(clips) >= n:
            break
        got = 0
        try:
            r = requests.get(
                "https://api.pexels.com/videos/search",
                headers={"Authorization": key},
                params={"query": kw, "orientation": "portrait", "per_page": 5, "size": "large"},
                timeout=30,
            )
            vids = r.json().get("videos", [])
        except Exception:
            vids = []
        for vid in vids:
            if got >= per_kw or len(clips) >= n:
                break
            files = vid["video_files"]
            hd = [f for f in files if f.get("height", 0) >= 1920]
            # au moins 1920px de haut si dispo (net apres crop), sinon le plus grand
            best = min(hd, key=lambda f: f["height"]) if hd else max(files, key=lambda f: f.get("height", 0))
            dst = WORK / f"clip_{_clip_seq}.mp4"
            _clip_seq += 1
            try:
                with requests.get(best["link"], stream=True, timeout=60) as s:
                    with open(dst, "wb") as f:
                        for c in s.iter_content(1 << 16):
                            f.write(c)
            except Exception:
                continue
            clips.append(dst)
            used.append(kw)
            got += 1
    tag = f" [{label}]" if label else ""
    print(f"[visuels]{tag} {len(clips)} clips : {', '.join(dict.fromkeys(used)) or 'aucun'}")
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
        # Jump cuts toutes les CUT_MIN..CUT_MAX secondes (guide hook : 1 coupe / 2-4 s).
        # On cycle sur les clips avec des offsets differents -> pas de repetition visible.
        bw, bh = int(W * 1.25), int(H * 1.25)     # source plus grande pour zoomer sans perte
        # une image IA n'a pas de duree : on la traitera en boucle sur la duree du plan
        is_img = [c.suffix.lower() in (".jpg", ".jpeg", ".png") for c in clips]
        durations = [(999.0 if im else _duration(c)) for c, im in zip(clips, is_img)]
        parts, i, elapsed = [], 0, 0.0
        while elapsed < total + 0.6:
            idx = i % len(clips)
            c, src_dur, img = clips[idx], durations[idx], is_img[idx]
            in_hook = elapsed < HOOK_WINDOW        # zone d'accroche : traitement a part
            cut = (random.uniform(HOOK_CUT_MIN, HOOK_CUT_MAX) if in_hook
                   else random.uniform(CUT_MIN, CUT_MAX))
            lap = i // len(clips)                  # tour de boucle -> decale la portion utilisee
            start = min(max(0.0, src_dur - cut - 0.1), lap * cut * 1.7)
            p = WORK / f"seg_{i}.mp4"

            px, py = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
            # Les images IA font 576x1024 en natif : on limite fortement le zoom sinon
            # l'agrandissement cumule (upscale x zoom) pixellise l'image.
            pf = AI_PUNCH_FROM if img else HOOK_PUNCH_FROM
            zmax = 1.16 if img else 1.30
            if i == 0 and HOOK_PUNCH:
                # SNAP ZOOM d'ouverture : zoom brutal qui se resorbe en ~0.35s
                # (les zooms rapides surpassent les plans statiques d'un facteur 2.5)
                z = f"max({pf}-{(pf - 1.0) / 10.5:.4f}*on,1.02)"
                if HOOK_SHAKE:       # secousse amortie, calee sur le boom d'ouverture
                    amp = 10 if img else 18
                    px += f"+{amp}*sin(on/1.6)*exp(-on/9)"
                    py += f"+{int(amp * 0.78)}*cos(on/1.3)*exp(-on/9)"
            elif in_hook:
                # chaque plan du hook a son propre punch (alterne avant/arriere)
                sp = 0.006 if img else 0.011
                z = (f"max({zmax}-{sp}*on,1.02)" if i % 2 == 0
                     else f"min(1.02+{sp}*on,{zmax})")
            else:
                # corps : Ken Burns lent
                z = "min(1+0.0016*on,1.25)" if i % 2 == 0 else "max(1.25-0.0016*on,1.0)"

            grade = f",{HOOK_GRADE}" if in_hook else ""   # image plus contrastee sur le hook
            # flash blanc tres bref a chaque coupe du hook (sauf la 1re) = impact visuel
            flash = (",fade=t=in:st=0:d=0.05:color=white"
                     if (in_hook and HOOK_FLASH and i > 0) else "")

            vfx = ""
            if in_hook and VFX_DUTCH and i % 2 == 1:
                # DUTCH ANGLE : plan incline (instabilite). L'ordre est critique :
                # on AGRANDIT d'abord, on tourne ensuite, puis on recadre au centre.
                # (tourner avant d'agrandir laisse les coins vides dans le cadre)
                ang = VFX_DUTCH_DEG * (1 if (i // 2) % 2 == 0 else -1)
                # facteur exact pour qu'aucun coin vide n'entre dans le cadre :
                #   k = cos(a) + sin(a) * (H/W)   (+3% de securite)
                _r = math.radians(abs(VFX_DUTCH_DEG))
                k = (math.cos(_r) + math.sin(_r) * (H / W)) * 1.03
                vfx += (f",scale={int(W * k)}:{int(H * k)},"
                        f"rotate={ang}*PI/180,crop={W}:{H}")
            if in_hook and VFX_VIGNETTE:
                vfx += ",vignette=angle=PI/5"          # assombrit les bords -> oeil au centre
            if i == 0 and VFX_GLITCH:
                # ABERRATION CHROMATIQUE : canaux R/B ecartes puis recolles par paliers
                # (rgbashift n'accepte pas d'expression -> on empile des fenetres temporelles,
                #  ce qui donne un rendu saccade typique du glitch numerique)
                a = VFX_GLITCH_AMP
                for lo, hi, amp in ((0.00, 0.10, a), (0.10, 0.20, int(a * 0.55)),
                                    (0.20, 0.32, int(a * 0.25))):
                    vfx += (f",rgbashift=rh=-{amp}:bh={amp}:gv={max(1, amp // 3)}"
                            f":enable='between(t,{lo},{hi})'")

            # Image IA basse def : upscale lanczos + faible sur-echantillonnage.
            # Video stock : marge plus large pour les mouvements de camera.
            sw, sh = (int(W * 1.10), int(H * 1.10)) if img else (bw, bh)
            flags = ":flags=lanczos" if img else ""
            sharpen = ",unsharp=3:3:0.55" if img else ""
            vf = (f"scale={sw}:{sh}:force_original_aspect_ratio=increase{flags},"
                  f"crop={sw}:{sh}{sharpen},"
                  f"zoompan=z='{z}':d=1:x='{px}':y='{py}':"
                  f"s={W}x{H}:fps=30{grade}{vfx}{flash},setsar=1")
            # image fixe -> -loop 1 (animee par le zoompan) ; video -> on coupe a `start`
            src_args = (["-loop", "1", "-i", str(c)] if img
                        else ["-ss", f"{start:.2f}", "-i", str(c)])
            subprocess.run(
                ["ffmpeg", "-y", *src_args, "-t", f"{cut:.2f}",
                 "-vf", vf, "-an", "-r", "30",
                 "-c:v", "libx264", "-crf", "16", "-preset", "medium", "-pix_fmt", "yuv420p",
                 str(p)],
                check=True, capture_output=True)
            if p.exists() and _duration(p) > 0.4:
                parts.append(p)
                elapsed += _duration(p)
            i += 1
            if i > 60:                             # garde-fou
                break
        print(f"[montage] {len(parts)} plans (~{total / max(1, len(parts)):.1f}s/plan)")
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

    # --- Chaine video : sous-titres + fondu de SORTIE seulement.
    # Aucun fondu d'ouverture : l'image doit etre pleine des la frame 1 (hook visuel).
    vchain = (f"[0:v]subtitles='{ass_esc}':fontsdir=fonts,"
              f"fade=t=out:st={total - 0.6:.2f}:d=0.6[v]")

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
            # CUT TOTAL de la musique a 1.5s pendant 0.2s : vide auditif percutant
            f"volume='if(between(t,{SFX_CUT_AT},{SFX_CUT_AT + SFX_CUT_DUR}),0,1)':eval=frame,"
            f"afade=t=in:st=0:d=0.8,afade=t=out:st={total - 1.6:.2f}:d=1.6[mus0];"
            # ducking : la musique baisse quand la voix parle
            f"[mus0][vocsc]sidechaincompress=threshold=0.03:ratio=8:attack=5:release=280[mus];"
            f"[voc][mus]amix=inputs=2:duration=first:normalize=0[mixed]"
        )
        print(f"[musique] {music.name}")
    else:
        achain = voc + "[mixed]"
        print("[musique] aucune (depose un .mp3 dans music/)")

    if SFX_HOOK:
        # Timeline sound design du hook (guide) :
        #   0.0s  sub boom 30-60Hz  -> reveil physique par le haut-parleur
        #   0.5s  whoosh            -> accompagne le snap zoom
        #   1.7s  riser             -> montee d'urgence apres le vide auditif
        wh_ms = int(SFX_WHOOSH_AT * 1000)
        ri_ms = int(SFX_RISER_AT * 1000)
        achain += (
            ";aevalsrc='0.8*exp(-4.5*t)*sin(2*PI*(150*t-95*t*t))':d=0.9:s=44100:c=stereo,"
            f"apad,atrim=0:{total:.2f}[boom];"
            # whoosh : bruit rose filtre, monte puis retombe
            "anoisesrc=d=0.5:c=pink:a=0.5:s=44100,aformat=channel_layouts=stereo,"
            "highpass=f=700,lowpass=f=7000,afade=t=in:d=0.25,afade=t=out:st=0.25:d=0.25,"
            f"adelay={wh_ms}|{wh_ms},apad,atrim=0:{total:.2f}[whoosh];"
            # riser : frequence qui monte = compte a rebours / urgence
            "aevalsrc='0.22*(t/1.2)*sin(2*PI*(260+520*t*t)*t)':d=1.2:s=44100:c=stereo,"
            f"adelay={ri_ms}|{ri_ms},apad,atrim=0:{total:.2f}[riser];"
            "[mixed][boom][whoosh][riser]amix=inputs=4:duration=first:normalize=0[aout]"
        )
    else:
        achain += ";[mixed]anull[aout]"

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
    # Le hook doit etre ILLUSTRE par ce qu'il raconte : on cherche d'abord des clips
    # colles au texte du hook, ils occuperont les tout premiers plans.
    hook_kw = [k for k in script.get("hook_keywords", []) if k]
    hook_clips = fetch_ai_images(hook_kw, n=AI_IMAGE_COUNT) if hook_kw else []
    if len(hook_clips) < 2 and hook_kw:          # repli si Pollinations ne repond pas
        hook_clips += fetch_clips(hook_kw, n=3, label="hook stock")
    body_clips = fetch_clips(script.get("keywords", [topic]), n=6, label="corps")
    clips = hook_clips + body_clips or hook_clips or body_clips

    out = OUT_DIR / "short.mp4"
    assemble(audio, ass, clips, out, total)

    tt_title = script["title"].replace("#Shorts", "").strip()
    meta = {"title": script["title"] + " #Shorts",
            "description": narration + "\n\n#Shorts #shorts",
            "tags": (script.get("keywords", []) + ["shorts"]),
            "topic": topic,
            "tiktok": f"{tt_title} 👀🤯\n\n#pourtoi #fyp #lesaviezvous #culturegenerale "
                      "#incroyable #apprendresurtiktok #wtf",
            "virality": script.get("virality"),
            "virality_reason": script.get("virality_reason", ""),
            "hook_variants": script.get("hook_variants", []),
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


def safe_filename(name: str) -> str:
    """Nom de fichier Windows valide a partir du titre de la video."""
    name = re.sub(r'[\\/:*?"<>|]', "", name).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return name[:110] or "video"


def archive(video: Path, meta: dict):
    """Copie la video + meta + vignette + miniature dans output/lib/<date>_<slug>/.
    Les fichiers portent le TITRE de la video (pas 'short')."""
    slug = re.sub(r"[^a-z0-9]+", "-", meta["topic"].lower()).strip("-")[:40] or "video"
    folder = OUT_DIR / "lib" / f"{datetime.now():%Y%m%d_%H%M%S}_{slug}"
    folder.mkdir(parents=True, exist_ok=True)

    base = safe_filename(meta.get("title", slug))
    shutil.copy2(video, folder / f"{base}.mp4")
    meta["file"] = f"{base}.mp4"
    (folder / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    # vignette interne (galerie de l'app)
    subprocess.run(["ffmpeg", "-y", "-ss", "1", "-i", str(video), "-frames:v", "1",
                    "-vf", "scale=360:-1", str(folder / "poster.jpg")], capture_output=True)
    # miniature YouTube (frame propre + titre)
    make_thumbnail(meta.get("title", ""), folder / f"{base} - miniature.jpg")
    print(f"[galerie] archive -> {folder}")


if __name__ == "__main__":
    main()
