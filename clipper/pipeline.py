"""Chaine complete : lien YouTube -> clips, avec suivi d'avancement.

Utilisee par la page locale (web.py). Chaque etape deja faite pour une video est
sautee : telechargement, transcription et changements d'image restent en cache
dans clipper/src, donc relancer une video ne coute que le choix et le rendu.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Callable

import clipper as C

SRC = C.HERE / "src"
OUT = C.HERE / "out"
Suivi = Callable[[str, float, str], None]     # (etape, avancement global 0-1, detail)

# part de la duree totale attribuee a chaque etape, pour une barre qui avance regulierement
ETAPES = [("telechargement", 0.10), ("transcription", 0.55), ("images", 0.05),
          ("choix", 0.05), ("rendu", 0.25)]


def _borne(etape: str) -> tuple[float, float]:
    debut = 0.0
    for nom, part in ETAPES:
        if nom == etape:
            return debut, debut + part
        debut += part
    return 1.0, 1.0


def assurer_ffmpeg() -> None:
    """Lancee hors du terminal habituel, la page ne voit pas toujours ffmpeg dans
    le PATH : on le cherche dans le dossier d'installation de winget."""
    if shutil.which("ffmpeg"):
        return
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    for exe in base.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"):
        os.environ["PATH"] = str(exe.parent) + os.pathsep + os.environ.get("PATH", "")
        return
    raise RuntimeError("ffmpeg introuvable : installe-le avec  winget install Gyan.FFmpeg")


def telecharger(url: str, suivi: Suivi) -> tuple[Path, dict]:
    import yt_dlp
    a, b = _borne("telechargement")
    fichiers_finis = [0]

    def hook(d: dict) -> None:
        if d["status"] == "finished":
            fichiers_finis[0] += 1
        elif d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                # video puis audio : deux fichiers, chacun compte pour moitie
                frac = (fichiers_finis[0] + d.get("downloaded_bytes", 0) / total) / 2
                suivi("telechargement", a + (b - a) * min(frac, 1.0),
                      f"{d.get('downloaded_bytes', 0) / 1e6:.0f} / {total / 1e6:.0f} Mo")

    SRC.mkdir(exist_ok=True)
    opts = {"format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]",
            "merge_output_format": "mp4", "outtmpl": str(SRC / "%(id)s.%(ext)s"),
            "progress_hooks": [hook], "quiet": True, "no_warnings": True, "noprogress": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        dst = SRC / f"{info['id']}.mp4"
        meta = {"id": info["id"], "titre": info.get("title", ""),
                "chaine": info.get("uploader") or info.get("channel", ""),
                "duree_source": info.get("duration"), "url": info.get("webpage_url", url),
                "licence": info.get("license") or "Licence YouTube standard"}
        dst.with_suffix(".info.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                                 encoding="utf-8")
        if dst.exists():
            suivi("telechargement", b, "deja telechargee")
        else:
            suivi("telechargement", a, meta["titre"])
            ydl.download([meta["url"]])
    return dst, meta


_modele = None


def transcrire(src: Path, suivi: Suivi, langue: str = "fr") -> None:
    global _modele
    a, b = _borne("transcription")
    out = src.with_suffix(".words.json")
    if out.exists():
        suivi("transcription", b, "deja transcrite")
        return
    from faster_whisper import WhisperModel
    suivi("transcription", a, "chargement du modele")
    if _modele is None:                       # charge une seule fois pour toute la session
        _modele = WhisperModel("small", device="cpu", compute_type="int8")
    segs, info = _modele.transcribe(str(src), language=langue, word_timestamps=True,
                                    vad_filter=True, beam_size=5)
    words = []
    for s in segs:
        for w in s.words or []:
            words.append({"w": w.word.strip(), "s": round(w.start, 2), "e": round(w.end, 2)})
        frac = min(1.0, s.end / max(1.0, info.duration))
        suivi("transcription", a + (b - a) * frac,
              f"{int(s.end // 60)}:{int(s.end % 60):02d} / "
              f"{int(info.duration // 60)}:{int(info.duration % 60):02d}")
    out.write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")


def traiter(url: str, n: int, suivi: Suivi, rechoisir: bool = False) -> dict:
    assurer_ffmpeg()
    src, meta = telecharger(url, suivi)
    transcrire(src, suivi)

    a, b = _borne("images")
    suivi("images", a, "detection des changements d'image")
    cuts = C.scenes(src)
    suivi("images", b, f"{len(cuts)} changements d'image")

    a, b = _borne("choix")
    suivi("choix", a, "Gemini lit la transcription")
    choix = C.choix_moments(src, n, meta["titre"], rechoisir)
    suivi("choix", b, f"{min(n, len(choix))} moments retenus")

    a, b = _borne("rendu")

    def par_clip(i: int, total: int, clip: dict) -> None:
        suivi("rendu", a + (b - a) * (i - 1) / max(1, total),
              f"clip {i}/{total} : {clip.get('titre', '')}")

    clips = C.produire(src, choix, n, par_clip)
    suivi("rendu", 1.0, f"{len(clips)} clips prets")
    return json.loads((OUT / f"{src.stem}.json").read_text(encoding="utf-8"))
