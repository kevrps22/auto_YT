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

SORTIE = HERE / "out_habille"
FOND = "0x0E1015"                    # fond des cartes typographiques
ACCENT = "0xFFB020"                  # meme orange que le mot prononce des sous-titres
SCENE_MIN, SCENE_MAX = 2.2, 6.0
# verbes et etats : presents dans les requetes, inutiles pour reconnaitre le sujet
ACTIONS = {"spinning", "driving", "pouring", "falling", "moving", "flowing", "working",
           "running", "glowing", "burning", "closeup", "close", "macro", "slow", "motion",
           "aerial", "view", "shot", "modern", "large", "small", "people", "person"}
CARTE_Y = 820                        # hauteur du texte des cartes (au-dessus des sous-titres)
# eclat blanc de 1 a 2 images a chaque coupe : les changements de plan etaient secs
FLASH = ",fade=t=in:st=0:d=0.05:color=white,"
MUSIQUE = C.ROOT / "music" / "bg.mp3"
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
        "ANGLAIS tres concrets (objets, gestes, lieux, matieres). Jamais d'abstraction, "
        "jamais de metaphore, jamais de mot de cadrage. Donne AUSSI `texte`, le mot cle "
        "francais correspondant : il s'affiche si aucune image n'est trouvee.\n"
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


def chercher_clip(requete: str, dst: Path) -> bool:
    """Un plan vertical Pexels dont le descriptif contient le mot principal de la
    requete. Sans ce controle, Pexels renvoie n'importe quoi sur les requetes vagues."""
    cle = os.getenv("PEXELS_API_KEY")
    if not cle or not requete:
        return False
    # On note les candidats sur les NOMS de la requete. Exiger n'importe quel mot
    # laissait passer un disque vinyle pour « electricity meter spinning » (via
    # "spinning") ; exiger le mot le plus long rejetait presque toutes les recherches.
    mots = [m for m in re.findall(r"[a-z]+", requete.lower())
            if len(m) > 3 and m not in ACTIONS]
    try:
        r = requests.get("https://api.pexels.com/videos/search", timeout=30,
                         headers={"Authorization": cle},
                         params={"query": requete, "orientation": "portrait",
                                 "per_page": 12, "size": "medium"})
        vids = r.json().get("videos", [])
    except Exception:
        return False
    notes = []
    for v in vids:
        note = sum(1 for m in mots if m in v.get("url", "").lower())
        if v["id"] not in _pris and note:
            notes.append((note, v))
    for _, v in sorted(notes, key=lambda x: -x[0]):
        fichiers = [f for f in v["video_files"]
                    if (f.get("height") or 0) >= (f.get("width") or 1)]
        if not fichiers:
            continue
        f = max(fichiers, key=lambda f: f.get("height") or 0)
        try:
            with requests.get(f["link"], stream=True, timeout=60) as d:
                d.raise_for_status()
                with dst.open("wb") as out:
                    for bloc in d.iter_content(1 << 20):
                        out.write(bloc)
        except Exception:
            continue
        _pris.add(v["id"])
        return True
    return False


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


def scene_carte(texte: str, duree: float, dst: Path, travail: Path, flash: bool = True) -> None:
    """Carte typographique : fond sombre, mot cle en grand, filet orange."""
    txt = _decouper(texte.upper())
    fichier = travail / f"carte_{abs(hash(texte)) % 99999}.txt"
    fichier.write_text(txt, encoding="utf-8")          # textfile : zero probleme d'echappement
    police = str(C.FONTS_DIR / "Montserrat-Black.ttf").replace("\\", "/").replace(":", r"\:")
    chemin = str(fichier).replace("\\", "/").replace(":", r"\:")
    long_max = max(len(x) for x in txt.splitlines())
    taille = 132 if long_max <= 9 else (104 if long_max <= 13 else 86)
    lignes = txt.count("\n") + 1
    # `text_h` de drawtext ne mesure QU'UNE ligne : le bloc descend donc bien plus bas
    # que prevu, et le filet tombait sur la deuxieme ligne.
    bas = lignes * (taille + 16) - taille // 2 + 40
    vf = (f"drawtext=fontfile='{police}':textfile='{chemin}':fontcolor=white:"
          f"fontsize={taille}:line_spacing=16:x=(w-text_w)/2:y={CARTE_Y}-text_h/2,"
          # Filet place sous le bloc, a partir de sa hauteur REELLE : le texte est
          # centre sur CARTE_Y, donc son bas est a CARTE_Y + hauteur/2. A hauteur
          # fixe, le filet barrait la deuxieme ligne.
          f"drawbox=x=(iw-260)/2:y={CARTE_Y + bas}:"
          f"w=260:h=10:color={ACCENT}@0.95:t=fill,"
          # la carte n'est jamais figee : elle avance de 100 a 105 % pendant le plan
          f"zoompan=z='min(1+0.0009*on,1.05)':d=1:x='iw/2-(iw/zoom/2)':"
          f"y='ih/2-(ih/zoom/2)':s={C.W}x{C.H}:fps=30,"
          f"fade=t=in:st=0:d=0.12{FLASH if flash else ','}setsar=1")
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    f"color=c={FOND}:s={C.W}x{C.H}:r=30:d={duree:.2f}",
                    "-vf", vf, "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                    "-pix_fmt", "yuv420p", str(dst)], check=True, capture_output=True)


def scene_video(source: Path, duree: float, dst: Path, sens: int, flash: bool = True) -> None:
    """Plan d'illustration recadre en vertical, avec un lent mouvement de camera."""
    # mouvement plus franc : a 0,0009 par image, un plan de 4 s paraissait fige
    z = ("min(1.00+0.0016*on,1.16)" if sens else "max(1.16-0.0016*on,1.00)")
    vf = (f"scale={int(C.W * 1.2)}:{int(C.H * 1.2)}:force_original_aspect_ratio=increase,"
          f"crop={int(C.W * 1.2)}:{int(C.H * 1.2)},"
          f"zoompan=z='{z}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
          f"s={C.W}x{C.H}:fps=30,eq=contrast=1.06:saturation=1.05,"
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


# ------------------------------------------------------------------ assemblage
def habiller(vid: str, numero: int, replan: bool = False) -> Path:
    src = HERE / "src" / f"{vid}.mp4"
    index = json.loads((HERE / "out" / f"{vid}.json").read_text(encoding="utf-8"))
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
        segments, impacts, t = [], [], 0.0
        for i, sc in enumerate(plan):
            fin = min(float(sc.get("fin", t + 4)), duree)
            d = max(1.2, fin - t)
            if t >= duree - 0.3:
                break
            seg = travail / f"seg{i:02d}.mp4"
            fait = False
            if sc.get("type") == "video":
                brut = travail / f"src{i:02d}.mp4"
                if chercher_clip(sc.get("requete", ""), brut):
                    scene_video(brut, d, seg, i % 2, flash=bool(segments))
                    fait = True
                    impacts.append((t, "souffle"))
                    print(f"  {t:5.1f}s  video  {sc.get('requete')}")
            if not fait:                       # pas d'image trouvee -> carte de repli
                # jamais la requete anglaise a l'ecran : elle n'est qu'une recherche
                txt = sc.get("texte") or clip["titre"].split(":")[0]
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
        sfx = travail / "sfx.wav"
        piste_sfx(impacts, duree, sfx)
        # Trois pistes : la voix intacte, les impacts, et la musique qui s'efface sous
        # la voix (sidechaincompress). Aucun filtre sur la voix : ils la faisaient
        # gresiller sur l'ancien generateur.
        # asplit : un label ffmpeg ne se consomme qu'UNE fois, or la voix sert deux
        # fois, au mixage et comme declencheur de l'attenuation de la musique.
        melange = (f"[1:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                   f"asplit=2[voix][voix_duck];"
                   f"[2:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                   f"volume={VOL_SFX}[sfx];"
                   f"[3:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                   f"volume={VOL_MUSIQUE},afade=t=in:st=0:d=0.8,"
                   f"afade=t=out:st={max(0.0, duree - 1.2):.2f}:d=1.2[mus];"
                   f"[mus][voix_duck]sidechaincompress=threshold=0.02:ratio=12:attack=15:"
                   f"release=350[musd];"
                   f"[voix][sfx][musd]amix=inputs=3:duration=first:normalize=0[a]")
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(fond), "-ss", f"{s:.2f}", "-t", f"{duree:.2f}",
             "-i", str(src), "-i", str(sfx), "-stream_loop", "-1", "-i", str(MUSIQUE),
             "-filter_complex",
             f"[0:v]subtitles='{ass_ff}':fontsdir='{fonts_ff}'[v];{melange}",
             "-map", "[v]", "-map", "[a]", "-shortest",
             "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-pix_fmt", "yuv420p",
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
