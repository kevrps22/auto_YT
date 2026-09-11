"""Personnage 'brainrot' incruste qui parle en rythme avec la voix off.

Pas de modele de lip-sync IA (pas de CUDA sur cette machine, poids non-commerciaux) :
la bouche est animee par l'AMPLITUDE de l'audio. Le personnage reste ENTIER --
on dessine seulement l'interieur de la bouche (ellipse sombre) sur la zone
marquee dans l'app, dont la hauteur suit la voix. A l'ecran ca se lit comme un
lip-sync, pour 0 seconde de GPU.

Chaque personnage vit dans characters/<slug>/ :
  base.png    image d'origine (generee via Pollinations ou importee)
  cutout.png  fond detoure (c'est elle qui est incrustee)
  char.json   reglages : position de la bouche, largeur, ouverture, taille...
Le slug actif est dans characters/active.txt (ecrit par l'app YT Studio).

CLI :
  python character.py --list
  python character.py --generate "un renard roux en costume" --name "Renard"
  python character.py --import C:\\chemin\\perso.png --name "Mon perso"
  python character.py --prepare <slug>            # (re)detoure
  python character.py --preview <slug>            # apercu bouche ouverte
  python character.py --set-active <slug>
  python character.py <video.mp4> [-o sortie.mp4] # incruste sur une video finie
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.parse
import wave
from pathlib import Path

import requests

HERE = Path(__file__).parent
WORK = HERE / "work_char"
CHAR_DIR = HERE / "characters"
ACTIVE_FILE = CHAR_DIR / "active.txt"

# Suffixe de prompt : impose un rendu exploitable (perso entier, face, bouche fermee).
GEN_STYLE = ("mouth closed, front facing, full body, centered, vibrant colors, "
             "studio lighting, isolated on plain white background, no text")

FPS = 30

# Reglages par defaut d'un nouveau personnage (tous editables depuis l'app).
DEFAULTS = {
    "prompt": "",
    "width": 0.26,        # largeur du perso / largeur de la video
    "margin": 0.03,       # marge par rapport aux bords
    "anchor": "bottom-left",
    "mouth_y": 0.60,      # hauteur de la bouche (0 = haut de l'image, 1 = bas)
    "mouth_x0": 0.32,     # etendue horizontale de la bouche (0-1 de la largeur)
    "mouth_x1": 0.68,
    "open": 0.055,        # ouverture max de la bouche / hauteur du perso
    "bob": 6,             # amplitude du dandinement vertical (energie 'brainrot')
    "after_hook": True,   # n'apparait qu'apres le hook (les 3s de retention)
    "enabled": True,
}

# Le perso n'apparait PAS pendant le hook : ces 3 premieres secondes portent toute
# la retention, chaque pixel compte et les sous-titres y sont prioritaires.
HOOK_WINDOW = 3.2


# ------------------------------------------------------------------ bibliotheque
def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s or "perso"


def char_path(slug: str) -> Path:
    return CHAR_DIR / slug


def load(slug: str) -> dict:
    cfg = dict(DEFAULTS)
    f = char_path(slug) / "char.json"
    if f.exists():
        try:
            cfg.update(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    cfg.setdefault("name", slug)
    cfg["slug"] = slug
    return cfg


def save(slug: str, cfg: dict) -> None:
    d = char_path(slug)
    d.mkdir(parents=True, exist_ok=True)
    (d / "char.json").write_text(
        json.dumps({k: v for k, v in cfg.items() if k != "slug"}, indent=2, ensure_ascii=False),
        encoding="utf-8")


def list_chars() -> list[dict]:
    if not CHAR_DIR.exists():
        return []
    return [load(p.name) for p in sorted(CHAR_DIR.iterdir())
            if p.is_dir() and (p / "char.json").exists()]


def get_active() -> str | None:
    if ACTIVE_FILE.exists():
        slug = ACTIVE_FILE.read_text(encoding="utf-8").strip()
        if slug and (char_path(slug) / "char.json").exists() and load(slug).get("enabled", True):
            return slug
    return None


def set_active(slug: str) -> None:
    CHAR_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_FILE.write_text(slug, encoding="utf-8")


# ------------------------------------------------------------------ personnage
def _cutout(src: Path, dst: Path) -> None:
    """Detoure le fond. rembg si dispo (marche sur photo ET dessin),
    sinon repli : suppression du blanc/vert uni."""
    from PIL import Image
    img = Image.open(src).convert("RGBA")
    cut = None
    try:
        from rembg import remove
        cut = remove(img)
    except Exception as e:
        print(f"[perso] rembg indispo ({e}) -> detourage par couleur")
    if cut is None:
        import numpy as np
        a = np.asarray(img).astype(np.int16)
        r, g, b = a[..., 0], a[..., 1], a[..., 2]
        mask = ((r > 232) & (g > 232) & (b > 232)) | ((g > 90) & (g - r > 45) & (g - b > 45))
        alpha = a[..., 3].copy()
        alpha[mask] = 0
        cut = Image.fromarray(np.dstack([r, g, b, alpha]).astype("uint8"), "RGBA")
    bbox = cut.getbbox()                   # recadre sur le personnage
    if bbox:
        cut = cut.crop(bbox)
    cut.save(dst)
    print(f"[perso] detoure -> {dst.name} ({cut.width}x{cut.height})")


def prepare(slug: str) -> Path:
    """(Re)fabrique cutout.png depuis base.png."""
    d = char_path(slug)
    base = d / "base.png"
    if not base.exists():
        sys.exit(f"base.png introuvable pour '{slug}'")
    _cutout(base, d / "cutout.png")
    return d / "cutout.png"


def generate(prompt: str, name: str | None = None, seed: int | None = None) -> str:
    """Genere le personnage via Pollinations (gratuit, sans cle) puis le detoure."""
    slug = slugify(name or prompt[:40])
    d = char_path(slug)
    d.mkdir(parents=True, exist_ok=True)
    seed = seed if seed is not None else random.randint(1, 99999)
    full = f"{prompt}, {GEN_STYLE}"
    url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(full)
           + f"?width=768&height=768&nologo=true&seed={seed}")
    print(f"[perso] generation (seed={seed})...")
    r = requests.get(url, timeout=180)
    r.raise_for_status()
    (d / "base.png").write_bytes(r.content)

    cfg = load(slug)
    cfg["name"] = name or slug
    cfg["prompt"] = prompt
    cfg["seed"] = seed
    save(slug, cfg)
    prepare(slug)
    print(f"[perso] pret : {slug}")
    return slug


def import_image(path: Path, name: str | None = None) -> str:
    """Importe une image perso fournie par l'utilisateur."""
    from PIL import Image
    if not path.exists():
        sys.exit(f"introuvable : {path}")
    slug = slugify(name or path.stem)
    d = char_path(slug)
    d.mkdir(parents=True, exist_ok=True)
    Image.open(path).convert("RGBA").save(d / "base.png")
    cfg = load(slug)
    cfg["name"] = name or path.stem
    cfg["prompt"] = ""
    save(slug, cfg)
    prepare(slug)
    print(f"[perso] importe : {slug}")
    return slug


def preview(slug: str) -> Path:
    """Ecrit un apercu bouche grande ouverte (verification de la machoire)."""
    from PIL import Image
    cfg = load(slug)
    cut = char_path(slug) / "cutout.png"
    if not cut.exists():
        prepare(slug)
    img = Image.open(cut).convert("RGBA")
    frame = _frame(img, cfg, amp=1.0, index=0)
    out = char_path(slug) / "preview_open.png"
    frame.save(out)
    print(f"[perso] apercu -> {out}")
    return out


# ------------------------------------------------------------------ audio
def envelope(src: Path, fps: int = FPS, offset: float = 0.0,
             n_frames: int | None = None) -> list[float]:
    """Enveloppe d'amplitude (0..1), une valeur par frame video.

    offset = decalage (s) de la voix dans la video : avant, bouche fermee.
    """
    import numpy as np
    WORK.mkdir(exist_ok=True)
    wav = WORK / "track.wav"
    subprocess.run(["ffmpeg", "-y", "-i", str(src), "-vn",
                    "-ac", "1", "-ar", "16000", str(wav)],
                   check=True, capture_output=True)
    with wave.open(str(wav), "rb") as w:
        rate, n = w.getframerate(), w.getnframes()
        data = np.frombuffer(w.readframes(n), dtype="<i2").astype("float32") / 32768.0

    step = rate / fps
    total = n_frames if n_frames is not None else int(len(data) / step)
    off = int(round(offset * fps))
    vals = []
    for i in range(total):
        j = i - off                                  # index dans l'audio
        if j < 0:
            vals.append(0.0)
            continue
        chunk = data[int(j * step):int((j + 1) * step)]
        vals.append(float(np.sqrt((chunk ** 2).mean())) if len(chunk) else 0.0)

    env = np.array(vals)
    if env.max() > 0:
        # normalisation sur un percentile haut : la voix sature la bouche,
        # les pics isoles (boom, whoosh) ne l'ouvrent pas en grand tout seuls.
        ref = np.percentile(env[env > 0], 85) or env.max()
        env = np.clip(env / ref, 0, 1)
    # seuil : en dessous, bouche fermee (souffle, silence residuel de la musique)
    env = np.where(env < 0.12, 0.0, env)
    print(f"[perso] enveloppe : {len(env)} frames")
    return env.tolist()


# ------------------------------------------------------------------ animation
def _frame(img, cfg: dict, amp: float, index: int):
    """Compose une frame : le personnage est INTACT, seule la bouche s'ouvre.

    On dessine l'interieur de la bouche (ellipse sombre) par-dessus la zone
    marquee dans l'app. Rien n'est coupe ni deplace : le perso reste entier.
    """
    from PIL import Image, ImageDraw, ImageFilter
    w, h = img.size
    bob_px = int(cfg.get("bob", 0) or 0)
    bob = int(math.sin(index / FPS * 2 * math.pi * 1.6) * bob_px) if bob_px else 0

    canvas = Image.new("RGBA", (w, h + bob_px * 2), (0, 0, 0, 0))
    oy = bob_px + bob
    canvas.paste(img, (0, oy), img)

    max_open = max(2, int(h * float(cfg["open"])))
    dy = amp * max_open
    if dy <= 1:                       # bouche fermee : on laisse l'image d'origine
        return canvas

    # compat : les anciens personnages stockaient la position sous la cle "jaw"
    my = float(cfg.get("mouth_y", cfg.get("jaw", 0.60))) * h
    x0, x1 = w * float(cfg["mouth_x0"]), w * float(cfg["mouth_x1"])
    # la bouche s'ouvre autour de sa ligne, et se pince un peu sur les cotes
    pinch = (x1 - x0) * 0.10 * amp
    top, bot = oy + my - dy / 2, oy + my + dy / 2

    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse([x0 + pinch, top, x1 - pinch, bot], fill=(48, 18, 22, 255))
    if amp > 0.5:                     # langue : visible seulement grand ouvert
        tw, th = (x1 - x0) * 0.5, dy * 0.35
        cx = (x0 + x1) / 2
        d.ellipse([cx - tw / 2, bot - th * 1.25, cx + tw / 2, bot - th * 0.15],
                  fill=(190, 78, 92, 255))
    layer = layer.filter(ImageFilter.GaussianBlur(0.7))   # tue le bord dur
    canvas.alpha_composite(layer)
    return canvas


def render_frames(char: Path, env: list[float], out_dir: Path, char_w: int,
                  cfg: dict | None = None) -> int:
    """Ecrit la sequence PNG du personnage qui parle. Renvoie le nb de frames."""
    from PIL import Image
    cfg = cfg or dict(DEFAULTS)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.png"):
        old.unlink()

    img = Image.open(char).convert("RGBA")
    img = img.resize((char_w, int(img.height * char_w / img.width)), Image.LANCZOS)

    for i, amp in enumerate(env):
        _frame(img, cfg, amp, i).save(out_dir / f"f_{i:05d}.png")

    print(f"[perso] {len(env)} frames rendues ({img.width}x{img.height})")
    return len(env)


# ------------------------------------------------------------------ montage
def _probe(video: Path) -> tuple[int, int]:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height", "-of", "csv=p=0",
                        str(video)], capture_output=True, text=True, check=True)
    w, h = r.stdout.strip().split(",")[:2]
    return int(w), int(h)


def overlay_xy(cfg: dict, vw: int, vh: int) -> tuple[str, str]:
    """Position de l'incrustation (expressions ffmpeg)."""
    m = int(vw * float(cfg.get("margin", 0.03)))
    anchor = cfg.get("anchor", "bottom-left")
    x = {"bottom-left": f"{m}",
         "bottom-right": f"W-w-{m}",
         "bottom-center": "(W-w)/2"}.get(anchor, f"{m}")
    return x, f"H-h-{int(vh * float(cfg.get('margin', 0.03)))}"


def burn(video: Path, frames_dir: Path, out: Path, vw: int, vh: int,
         cfg: dict | None = None):
    """Incruste la sequence PNG sur une video deja montee."""
    cfg = cfg or dict(DEFAULTS)
    x, y = overlay_xy(cfg, vw, vh)
    enable = f":enable='gte(t,{HOOK_WINDOW})'" if cfg.get("after_hook", True) else ""
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video),
         "-framerate", str(FPS), "-i", str(frames_dir / "f_%05d.png"),
         "-filter_complex", f"[0:v][1:v]overlay={x}:{y}{enable}[v]",
         "-map", "[v]", "-map", "0:a?",
         "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
         "-c:a", "copy", str(out)],
        check=True, capture_output=True)
    print(f"[perso] incruste -> {out}")


def _duration(src: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(src)],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip() or 0)


def burn_on_video(video: Path, out: Path | None = None, slug: str | None = None,
                  audio_src: Path | None = None, offset: float = 0.0) -> Path | None:
    """Incruste le personnage actif sur une video finie. Renvoie le fichier produit.

    audio_src : piste qui pilote la bouche. Donner la VOIX seule (et son decalage
    dans la video) evite que la musique et les bruitages fassent parler le perso.
    """
    slug = slug or get_active()
    if not slug:
        print("[perso] aucun personnage actif -> ignore")
        return None
    cfg = load(slug)
    if not cfg.get("enabled", True):
        print("[perso] personnage desactive -> ignore")
        return None
    cut = char_path(slug) / "cutout.png"
    if not cut.exists():
        prepare(slug)

    WORK.mkdir(exist_ok=True)
    out = out or video.with_name(video.stem + "_perso.mp4")
    vw, vh = _probe(video)
    n_frames = max(1, int(math.ceil(_duration(video) * FPS)))
    env = envelope(audio_src or video, offset=offset if audio_src else 0.0,
                   n_frames=n_frames)
    render_frames(cut, env, WORK / "frames", int(vw * float(cfg["width"])), cfg)
    burn(video, WORK / "frames", out, vw, vh, cfg)
    return out


def main():
    ap = argparse.ArgumentParser(description="Studio personnage")
    ap.add_argument("video", type=Path, nargs="?")
    ap.add_argument("-o", "--out", type=Path)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--generate", metavar="PROMPT")
    ap.add_argument("--import", dest="imp", metavar="IMAGE")
    ap.add_argument("--name", metavar="NOM")
    ap.add_argument("--prepare", metavar="SLUG")
    ap.add_argument("--preview", metavar="SLUG")
    ap.add_argument("--set-active", dest="set_active", metavar="SLUG")
    a = ap.parse_args()

    if a.list:
        print(json.dumps(list_chars(), indent=2, ensure_ascii=False))
    elif a.generate:
        print("SLUG=" + generate(a.generate, a.name, a.seed))
    elif a.imp:
        print("SLUG=" + import_image(Path(a.imp), a.name))
    elif a.prepare:
        prepare(a.prepare)
    elif a.preview:
        preview(a.preview)
    elif a.set_active:
        set_active(a.set_active)
        print(f"[perso] actif : {a.set_active}")
    elif a.video:
        if not a.video.exists():
            sys.exit(f"introuvable : {a.video}")
        burn_on_video(a.video, a.out)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
