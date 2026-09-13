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
    # HOOK : on entre en criant, pas en s'installant. Un demarrage calme = swipe.
    "hook":       ("+32%", "+14Hz", "+0%"),  # attaque maximale : debit + tension vocale
    "tension":    ("+2%",  "-2Hz", "+0%"),   # TWIST : on ralentit et on descend -> revelation
    "body":       ("+14%", "+0Hz", "+0%"),   # debit rapide mais intelligible
    "revelation": ("-2%",  "+3Hz", "+0%"),   # chute : encore plus lent, ton qui remonte
    "loop":       ("+14%", "+0Hz", "+0%"),
}
# Ton Google (speakingRate, volumeGainDb) par segment (Chirp 3 HD ne gere pas le pitch).
GTONES = {
    "hook":       (1.24, 6.0),
    "tension":    (1.03, 1.0),
    "body":       (1.00, 0.0),
    "revelation": (0.95, 2.0),
    "loop":       (1.06, 1.0),
}
# Kokoro (open-source local). FR natif = ff_siwis (feminine). Voix masculine configurable
# via .env KOKORO_VOICE (les voix masculines viennent d'autres langues -> accent possible).
KOKORO_VOICE = os.getenv("KOKORO_VOICE", "ff_siwis")
KOKORO_SPEED = {"hook": 1.22, "tension": 1.0, "body": 1.0, "revelation": 0.92, "loop": 1.05}
# Moteur voix : "kokoro" | "google" | "edge" (via .env TTS_ENGINE ; defaut edge)
TTS_ENGINE = os.getenv("TTS_ENGINE", "edge").lower()
# Modeles Gemini essayes dans l'ordre (quota gratuit = 20 requetes/jour PAR modele)
# Le plus capable en tete : la qualite editoriale se joue entierement ici.
# Chaque modele a son PROPRE quota gratuit (20 req/jour), donc allonger la liste
# augmente mecaniquement le nombre de videos possibles par jour.
# Verifie le 11/09/2026 : gemini-2.0-flash et 2.0-flash-lite renvoient 404 (retires).
GEMINI_MODELS = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash",
                 "gemini-3.5-flash", "gemini-3-flash-preview", "gemini-flash-latest",
                 "gemini-3.5-flash-lite", "gemini-flash-lite-latest"]
PAUSE_BEFORE_REVELATION = 0.32       # micro-pause dramatique avant la revelation finale
PAUSE_BEFORE_TENSION = 0.26          # pause avant le TWIST (moment ou on nomme le sujet)
W, H = 1080, 1920                    # format vertical Short
FONT = "Anton"                       # police des sous-titres (fichier dans fonts/)

LEAD = 0.0                           # la voix demarre a la SECONDE 0, aucun temps mort
TAIL = 0.6                           # queue minimale : 2s de silence en fin de video
                                     # cassaient la boucle et invitaient au swipe
CUT_MIN, CUT_MAX = 1.6, 2.4          # duree d'un plan (guide : nouvelle ancre visuelle
                                     # toutes les 1,5-2 s, sinon lassitude et decrochage)
# --- Hook VISUEL : 60% des vues sont sans son -> l'image doit accrocher seule.
HOOK_WINDOW = 3.2                    # duree de la zone "hook" traitee a part
# Montage PRO (13/09/2026) : sur de la vraie video, des plans de 0,45 s ne laissent
# pas voir l'action et font clip genere. 0,8-1,1 s reste rapide mais lisible.
HOOK_CUT_MIN, HOOK_CUT_MAX = 0.8, 1.1
# --- Sortie de hook PROGRESSIVE (mesure Analytics du 12/09/2026) ---
# 21 videos sur 25 avaient leur plus forte chute d'audience entre 4.4 et 5.3 s,
# soit le premier plan lent apres la fin du hook. Le montage passait d'un coup
# de 0.45 s a 1.5 s par plan, et l'etalonnage, les flashs, l'inclinaison et le
# vignettage s'arretaient tous au meme instant : l'energie s'effondrait d'un
# bloc a 3.2 s et le spectateur partait dans la seconde qui suivait.
# On etale desormais ce retour au calme jusqu'a DECAY_UNTIL.
DECAY_UNTIL = 9.0                    # fin de la transition hook -> corps
HOOK_PUNCH = True                    # snap zoom sur CHAQUE plan du hook
HOOK_PUNCH_FROM = 1.08               # leger push-in, pas un snap zoom
HOOK_SHAKE = False                   # secousse camera sur le 1er plan (synchro avec le boom)
HOOK_FLASH = False                   # flash blanc bref a chaque coupe du hook
# Le stock est souvent trop sombre : on releve franchement les basses lumieres.
# Une frame 0 sombre se lit mal en 200 ms et fait swiper.
HOOK_GRADE = "eq=contrast=1.06:saturation=1.08:brightness=0.02"   # naturel, a peine releve
# --- VFX du hook (spectacle des 3 premieres secondes)
VFX_GLITCH = False                   # aberration chromatique RGB a 0s (effet glitch cinema)
VFX_GLITCH_AMP = 22                  # amplitude du decalage RGB en pixels
VFX_DUTCH = False                    # dutch angle : plans inclines = instabilite/urgence
VFX_DUTCH_DEG = 5.0                  # inclinaison en degres
VFX_VIGNETTE = False                 # vignettage sur le hook : focalise le regard au centre
# Sous-titres tres courts : sur mobile, un ou deux mots lus au rythme de la voix
# gardent davantage l'oeil que des phrases de trois ou quatre mots.
HOOK_WORDS_PER_LINE = 2
SUBTITLE_WORDS_PER_LINE = 2
# --- Texte d'accroche PLEIN ECRAN a la frame 0 (independant des sous-titres).
# 60% des vues demarrent sans son : ce texte doit vendre la video a lui seul.
OVERLAY_ENABLED = False              # une seule couche de texte : les sous-titres
OVERLAY_UNTIL = 2.2                  # duree d'affichage (s) — disparait avant le twist
OVERLAY_FS = 168                     # tres gros : lisible en 200 ms sur un ecran de poche
# --- Images IA pour le hook (Pollinations : gratuit, sans cle, illimite)
# Images fixes generees : on voyait trois images synthetiques revenir en boucle, et
# l'aspect IA saute aux yeux. De la vraie video uniquement.
AI_IMAGES = False                    # False -> uniquement du stock Pexels
AI_IMAGE_COUNT = 3                   # nb d'images generees pour les premiers plans
AI_IMAGE_TIMEOUT = 90
AI_PUNCH_FROM = 1.22                 # zoom d'ouverture reduit sur les images (576x1024 natif)
# LISIBILITE > AMBIANCE. Une image sombre/floue/abstraite se lit en >1s : swipe.
# On impose donc : tres lumineux, sujet net et centre, fond simple, zero flou.
AI_IMAGE_STYLE = ("bright high-key lighting, extremely high contrast, razor sharp focus, "
                  "single clear subject centered and large in frame, simple uncluttered "
                  "background, bold saturated colors, crisp details, hyper realistic, "
                  "vertical 9:16, no motion blur, no dark shadows, well lit")
SFX_HOOK = True                      # sound design du hook (boom + whoosh + riser)
SFX_WHOOSH_AT = 0.5                  # whoosh cale sur le snap zoom
SFX_CUT_AT, SFX_CUT_DUR = 1.5, 0.2   # coupure totale de la musique = vide auditif
SFX_RISER_AT = 1.7                   # riser d'urgence juste apres le vide
SFX_TRANSITION_GAP = 0.70             # evite un whoosh a chaque frame/coupe ultra-rapide
SFX_TRANSITION_MAX = 12               # garde un mix lisible sur un Short
# Coupe des blancs : seuil -30dB (le "silence" du TTS n'est pas totalement muet),
# ne garde que 0.08s de respiration, agit des 0.10s de pause -> debit serre.
SILENCE_FILTER = ("silenceremove=start_periods=1:start_threshold=-30dB:start_silence=0.03:"
                  "stop_periods=-1:stop_threshold=-30dB:stop_silence=0.08:stop_duration=0.10")
# --- Anti hors-sujet : le stock repond au mot-cle, pas au SENS de la phrase.
# "paralysie" -> pistolet de massage, "biologie" -> mannequin d'anatomie en plastique.
# On rejette ces clips sur le slug de leur URL Pexels (toujours lisible et fiable).
BANNED_VISUALS = (
    "massage", "masseur", "spa", "wellness", "physiotherapy", "chiropract",
    "gym", "fitness", "workout", "dumbbell", "treadmill", "yoga", "pilates",
    "mannequin", "dummy", "plastic-model", "anatomy-model", "figurine", "doll",
    "classroom", "school", "student", "teacher", "lecture", "whiteboard",
    "meeting", "business", "presentation", "conference", "handshake", "coworking",
    "influencer", "vlog", "selfie", "tiktok", "podcast", "green-screen",
    # atelier d'art : "plaster cast" ramene un cours de poterie, "shark" un peintre
    "pottery", "potter", "sculptor", "sculpting", "sculpture-making", "clay",
    "ceramic", "handmade", "craft", "artisan", "artist", "painting-a", "drawing",
    "art-studio", "workshop", "kiln",
    # touristes : "roman ruins" ramene une vloggeuse devant le Colisee
    "tourist", "tourism", "traveler", "traveller", "travelling", "vacation",
    "holiday", "sightseeing", "backpacker", "posing", "walking-in-the-street",
    # machines industrielles : "rayons de Marie Curie" ramenait une decoupe laser
    "laser", "cnc", "cutting", "factory", "welding", "machining", "industrial-robot",
    "3d-printer", "assembly-line", "conveyor",
    # salle de sport : "la force du gorille" ramenait de la musculation humaine
    "gym", "bodybuilder", "bodybuilding", "fitness", "biceps", "workout", "dumbbell",
    "weightlifting", "treadmill",
    # fonds verts / VFX non incrustes : inutilisables tels quels
    "green-screen", "greenscreen", "chroma-key", "chromakey", "green-background",
    # captivite : un animal sauvage derriere une vitre casse le propos
    # ("-zoo-" et non "zoo" : sinon "zoom" serait rejete)
    "aquarium", "fish-tank", "behind-glass", "zoo-enclosure", "-zoo-", "captivity",
    # CLICHES SYMBOLIQUES : le stock illustre les mots abstraits par des symboles.
    # "sans cette barriere" a ramene une serrure et un trousseau de cles ; "manque
    # d'oxygene" une pancarte "Privacy Please". Ce sont des metaphores, jamais le sujet.
    "padlock", "keychain", "keyring", "-key-", "-keys-", "-lock-", "unlock",
    "do-not-disturb", "privacy", "signboard", "door-hanger", "-sign-",
    "puzzle", "jigsaw", "lightbulb", "light-bulb", "chess", "domino",
    "target-arrow", "stopwatch-hand", "piggy-bank", "scales-of-justice",
)

# Especes animales : un sujet "gorille" ne doit jamais montrer une panthere.
# On rejette tout clip citant une espece ABSENTE de la requete.
ANIMAL_SPECIES = frozenset({
    "gorilla", "chimpanzee", "chimp", "monkey", "ape", "orangutan", "baboon", "lemur",
    "lion", "tiger", "leopard", "panther", "cheetah", "jaguar", "puma", "cougar", "lynx",
    "wolf", "fox", "bear", "elephant", "rhino", "rhinoceros", "hippo", "hippopotamus",
    "giraffe", "zebra", "buffalo", "bison", "deer", "moose", "camel", "kangaroo",
    "shark", "whale", "dolphin", "orca", "octopus", "squid", "jellyfish", "crocodile",
    "alligator", "snake", "python", "cobra", "lizard", "turtle", "frog",
    "eagle", "hawk", "owl", "penguin", "flamingo", "parrot", "crow", "seagull",
    "horse", "cow", "sheep", "goat", "pig", "dog", "cat", "rabbit", "rat", "mouse",
    "spider", "scorpion", "mosquito", "bee", "wasp", "ant", "bat", "shrimp", "crab",
})

# Les exclusions THEMATIQUES ne sont pas codees ici : elles dependent du sujet et
# sont produites par Gemini pour chaque video (`negative_keywords` / `anchor_keywords`).
# Un meme mot peut etre interdit ou obligatoire selon le sujet : "scuba" ruine un
# short sur l'apnee, mais c'est le sujet meme d'un short sur la plongee bouteille.
# Seules restent globales ci-dessus les fautes qui ne servent JAMAIS ce format
# (fond vert, mannequin, reunion d'entreprise...), et `allow_keywords` permet a
# Gemini de lever un ban global quand le sujet porte precisement dessus.

# Bans SUPPLEMENTAIRES pour les sujets historiques/antiques : toute presence
# humaine moderne y est un anachronisme (touriste au Colisee, femme au musee...).
# Compares par MOT ENTIER du slug, jamais en sous-chaine : "roman" contient "man".
HISTORICAL_TOKENS = frozenset({
    "woman", "women", "man", "men", "girl", "boy", "lady", "guy", "people",
    "person", "couple", "family", "child", "children", "kid", "kids", "model",
    "car", "cars", "phone", "smartphone", "camera", "modern", "traffic", "road",
    "visitors", "visitor", "crowd", "walkway", "boardwalk", "fence", "railing",
    "wildfire", "bonfire", "campfire", "firefighter", "forest", "barbecue",
    # images de synthese / abstrait : un cristal 3D ne dit rien d'une cite antique
    "abstract", "crystal", "neon", "3d", "render", "rendering", "digital",
    "animation", "animated", "loop", "background", "wallpaper", "particles",
})
# Palette commune a TOUS les plans : ombres froides / hautes lumieres chaudes.
# C'est ce qui donne l'illusion d'une seule direction photo malgre des sources
# de stock disparates (regle de continuite visuelle).
PALETTE = ("colorbalance=rs=-0.04:bs=0.07:rm=0.02:bm=-0.02:rh=0.05:bh=-0.05,"
           "eq=contrast=1.08:saturation=1.10")
MUSIC_DIR = Path("music")            # depose des .mp3 ici (musique de fond, choix aleatoire)
MUSIC_VOL = 0.17                     # volume de la musique (0-1) ; ducking sous la voix
# Baisse d'un demi-ton environ, mais a tempo constant : plus cine sans rallonger le Short.
MUSIC_PITCH_FACTOR = 0.94
# CTA final DESACTIVE : un "abonne-toi" annonce que la video est finie et declenche
# le swipe, au lieu de laisser la boucle repartir sur le hook. Mettre un texte ici
# le reactive (au prix de la boucle).
OUTRO_TEXT = ""
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
            "facts": [f"{topic} repose sur un mecanisme rarement visible.",
                      "Les chiffres impliques sont bien plus grands qu'on l'imagine.",
                      "C'est ce detail qui change completement la facon de le voir."],
            "revelation": "Et c'est ce detail qui change tout.",
            "loop": "La prochaine fois, tu y repenseras forcement.",
            "overlay": "TU NE VAS PAS AIMER",
            "sfx": "impact sourd",
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
        "*** REGLE 3 : LE PREMIER MOT EST UNE GIFLE ***\n"
        "La voix ne doit JAMAIS s'installer ni poser un contexte. Le tout premier mot "
        "entre deja dans le vif. Commence obligatoirement par l'un de ces trois angles :\n"
        "  a) PARADOXE EXTREME : 'Ce truc inoffensif tue plus que toutes les guerres.'\n"
        "  b) PEUR COMMUNE : 'Pendant que tu dors, quelque chose s'introduit dans ta bouche.'\n"
        "  c) COMPARAISON CHOC : 'Cet animal tue 300 fois plus qu'un requin.'\n"
        "Zero mot de remplissage au demarrage : bannis 'alors', 'donc', 'en fait', 'imagine "
        "que', 'il faut savoir que', 'saviez-vous'. Le hook commence par un nom, un verbe "
        "ou un chiffre — jamais par une formule d'introduction.\n"
        "\n"
        "=== STRUCTURE DU RESTE (storytelling, PAS une liste) ===\n"
        "*** LOI DE RETENTION : la chute se produit a la seconde ou le spectateur "
        "obtient sa reponse. NE resous JAMAIS le mystere avant la REVELATION finale. "
        "La dette de curiosite du hook doit etre PLUS forte a 6s qu'a 2s. ***\n"
        "TENSION (3-8s) : NE nomme PAS le sujet, NE donne AUCUNE reponse. Une seule "
        "phrase qui AGRANDIT l'enjeu et rend le mystere plus grave (consequence pire, "
        "chiffre plus gros, menace plus proche). Interdit de reveler de quoi on parle.\n"
        "TECHNIQUE D'ELIMINATION (obligatoire si le sujet est un superlatif : 'le plus X', "
        "'le pire Y', 'le plus dangereux/mortel/cher...') : n'ENUMERE JAMAIS de facon neutre. "
        "Cite 1 ou 2 candidats evidents UNIQUEMENT pour les REJETER "
        "('Non, ce n'est pas le wingsuit. Ni la plongee.'). La vraie reponse reste CACHEE "
        "jusqu'a la revelation. Chaque rejet doit augmenter la tension, pas la baisser.\n"
        "BODY (8-30s) : 2 a 3 phrases COURTES, toutes branchees sur LA question unique du "
        "hook. INTERDIT de commencer une phrase par un nouvel exemple concret qui se suffit "
        "a lui-meme (ca transforme la video en liste et fait swiper). Chaque phrase resserre "
        "l'etau autour de la reponse sans jamais la donner. "
        "Mots sensoriels violents : fondre, pulveriser, exploser, ecraser, devorer.\n"
        "REVELATION : LA reponse finale, le fait le plus choquant, le sujet enfin nomme. "
        "C'est SEULEMENT ici que la tension se libere — jamais avant.\n"
        "LOOP : la phrase finale doit se RACCORDER GRAMMATICALEMENT au debut du hook, "
        "de sorte qu'en rebouclant la video on entende une seule phrase continue. "
        "Elle se termine donc en suspens, sans point final logique.\n"
        f"  Exemple — fin : '...et c'est exactement pour cette raison que' + hook : "
        "'ton cerveau se paralyse chaque nuit.' Lus a la suite, les deux forment UNE phrase.\n"
        "  Ce raccord fait remonter la duree vue au-dessus de 100%% : c'est le signal "
        "de viralite le plus fort pour l'algorithme. Ne termine JAMAIS sur une conclusion "
        "fermee ('voila pourquoi c'est fascinant') qui invite a swiper.\n"
        "\n"
        "=== PRIORITE DE PRODUCTION — CES REGLES REMPLACENT LES ANCIENNES CONSIGNES "
        "DE SUSPENSE CI-DESSUS ===\n"
        "HOOK (0-3 s) : question choc ou statement visuel, 8 a 14 mots.\n"
        "CORE CONTENT (3-18 s) : donne EXACTEMENT TROIS faits rapides, precis et "
        "verifiables. Chaque fait est une phrase filmeable avec un chiffre, une matiere, "
        "un lieu, une action ou un mecanisme reel. Aucun blabla, aucune metaphore, "
        "aucune liste de candidats. Le sujet peut etre nomme des le premier fait.\n"
        "REVELATION : transforme le troisieme fait ou sa consequence en chute claire, "
        "sans ajouter un quatrieme fait vague.\n"
        "OUTRO / LOOP (18-24 s) : une phrase courte ouverte qui se raccorde au SENS du "
        "hook. INTERDICTION FORMELLE de repeter le hook mot pour mot, de le paraphraser "
        "de trop pres, ou de reutiliser la meme structure. Le raccord doit etre nouveau, "
        "pas une intro recyclee.\n"
        "REGLES GLOBALES : phrases TRES courtes (max 12 mots), une idee par phrase, "
        "rythme rapide, zero mot inutile, zero remplissage. Style parle.\n"
        "ORTHOGRAPHE — REGLE STRICTE : ecris un francais PARFAITEMENT accentue et "
        "apostrophe, y compris dans le champ overlay. Les textes sont affiches en GROS "
        "a l'ecran : un accent manquant saute aux yeux et fait amateur.\n"
        "  FAUX : 'ete vitrifiees', 'de linterieur', 'leternite', 'ce nest pas'\n"
        "  JUSTE : 'été vitrifiées', \"de l'intérieur\", \"l'éternité\", \"ce n'est pas\"\n"
        "  Ne translitere JAMAIS en ASCII. Utilise é è ê à ù ç ô î et l'apostrophe.\n"
        "GRAMMAIRE : verifie les ACCORDS. 'personnes' est FEMININ PLURIEL : on ecrit "
        "'transformées en pierre vivantes', jamais 'transformés... vivants'.\n"
        "  L'OVERLAY doit s'accorder avec le sujet du HOOK, car les deux sont a l'ecran "
        "en meme temps : si le hook dit '20 000 personnes ont ete vitrifiées', l'overlay "
        "ecrit 'VITRIFIÉES' — jamais 'VITRIFIÉS'. En cas de doute, choisis pour l'overlay "
        "une formule SANS participe accorde (ex : 'MORTS EN UNE SECONDE', 'UNE SECONDE "
        "POUR MOURIR') plutot que de risquer une faute d'accord affichee en enorme.\n"
        "\n"
        "Reponds UNIQUEMENT en JSON strict avec les cles : "
        "title (string accrocheur), "
        "description (3 a 5 phrases qui EXPLIQUENT le sujet, pour la description "
        "YouTube. ATTENTION : ce n'est PAS le texte dit dans la video — le spectateur "
        "vient de l'entendre, le repeter n'apporte rien et nuit au referencement. "
        "Apporte le CONTEXTE que le short n'a pas eu le temps de donner : dates, "
        "lieux, chiffres officiels, noms propres, explication du mecanisme, ce qu'en "
        "disent les scientifiques ou les historiens. Ecris pour quelqu'un qui vient "
        "de voir la video et veut en savoir plus : ton informatif, pas d'accroche, "
        "pas de tutoiement. Place naturellement les mots que les gens taperaient dans "
        "la recherche YouTube. AUCUN hashtag, ils sont ajoutes automatiquement. "
        "EXEMPLE (Fort Knox) : 'Le United States Bullion Depository, construit en 1936 "
        "dans le Kentucky, conserve environ 4 580 tonnes d'or appartenant au Tresor "
        "americain. La porte du coffre pese plus de 20 tonnes et aucune personne seule "
        "ne connait la combinaison complete. Le site n'a ete ouvert a des visiteurs "
        "exterieurs qu'a deux reprises depuis sa construction.'), "
        "hook_variants (liste des 3 hooks generes), "
        "hook (le meilleur des 3, repete tel quel), "
        "facts (liste de EXACTEMENT 3 strings : les trois faits du core content, dans "
        "l'ordre. Ces phrases sont le texte reel de 3 a 18 secondes), "
        "tension, body, revelation (strings de compatibilite, peuvent reprendre les faits), "
        "loop (string), "
        "overlay (accroche de 3 a 5 MOTS MAXIMUM, en MAJUSCULES, affichee en grand au "
        "milieu de l'ecran des la milliseconde 0. 60% des gens regardent SANS LE SON : "
        "ce texte doit vendre la video a lui seul. Il doit choquer ou intriguer "
        "instantanement, sans nommer le sujet. Pas de ponctuation finale. "
        "MAUVAIS : 'Un fait sur le sommeil' (plat, nomme le sujet). "
        "BON : 'CA VIT DANS TA BOUCHE' / 'TU EN AVALES 8 PAR AN' / 'PIRE QUE LE REQUIN'. "
        "*** L'overlay ne doit JAMAIS CONTREDIRE le hook : il est affiche pendant que la "
        "voix prononce le hook. Reprends EXACTEMENT ses chiffres et sa duree. Si le hook "
        "dit 'en un quart de seconde', l'overlay ne peut pas dire 'EN UNE SECONDE'. ***), "
        "sfx (1 ou 2 mots : le bruitage d'impact qui colle au hook, ex 'impact sourd', "
        "'verre brise', 'coeur qui bat', 'alarme'), "
        "hook_visual_plan (liste de 3 objets pour les plans du hook) et visual_plan "
        "(liste de 8 objets pour le corps, dans l'ordre exact du recit). Chaque objet "
        "a OBLIGATOIREMENT : query (requete Pexels ANGLAISE concrete de 2 a 5 mots), "
        "concrete_subject (objet reel visible), must_include (1 a 3 mots anglais qui "
        "doivent etre visibles), must_not_include (0 a 5 voisins confondables a exclure), "
        "shot (closeup, macro, detail ou wide) et look (liste contenant cinematic, dark "
        "et high_detail). Pour un sujet abstrait, INTERDICTION de chercher l'abstraction : "
        "traduis-la en objets filmables. Exemple 'batiment le plus securise' -> query "
        "'bank vault door closeup', concrete_subject 'vault door', must_include "
        "['vault','door']. Les plans doivent privilegier gros plans, textures nettes et "
        "ambiance cinematographique sombre, jamais une icone ou une illustration), "
        "hook_keywords (liste de 3 prompts ANGLAIS pour illustrer les 2 premieres secondes. "
        "REGLE ABSOLUE DE LISIBILITE : une image sombre, floue, abstraite ou paisible fait "
        "SWIPER en moins d'une seconde. Chaque prompt doit decrire un SUJET UNIQUE, "
        "RECONNAISSABLE EN 200 ms, gros dans le cadre, en pleine lumiere. "
        "Montre quelque chose de FAMILIER place dans une situation ETRANGE ou MENACANTE "
        "(c'est le contraste familier/anormal qui stoppe le pouce, pas l'obscurite). "
        "INTERDIT dans les prompts : dark, night, shadow, blurry, silhouette, abstract, "
        "fog, dim, moody, calm, peaceful, empty. "
        "OBLIGATOIRE dans chaque prompt : bright, sharp, close-up. "
        "MAUVAIS : 'sleeping person in dark surreal void, eerie' (noir, abstrait, illisible). "
        "BON : 'extreme close-up of a human mouth on a white pillow, bright daylight, sharp, "
        "a tiny insect crawling on the lip, high contrast'. "
        "Objets/scenes filmables uniquement. Le PREMIER prompt illustre le tout debut du hook), "
        "hook_stock (liste de 3 requetes ANGLAISES TRES COURTES de 2 a 4 mots pour chercher "
        "ces memes plans dans une banque de video (Pexels), en mots-cles bruts sans "
        "adjectifs de style.\n"
        "  *** REGLE ABSOLUE : CHACUNE des 3 requetes DOIT contenir le NOM PRINCIPAL du "
        f"sujet ({topic}) en anglais. *** La toute premiere image de la video doit montrer "
        "le sujet lui-meme, pas une ambiance. "
        "MAUVAIS : 'mysterious jungle', 'dark laboratory', 'ancient mystery' (le sujet "
        "n'apparait pas -> le spectateur ne voit pas de quoi on parle et swipe). "
        "BON pour un sujet gorille : ['gorilla in jungle', 'gorilla face closeup', "
        "'silverback gorilla chest']. "
        "BON pour Marie Curie : ['marie curie laboratory', 'radium glowing tube', "
        "'old radioactive experiment']), "
        "scenes (liste de 8 requetes ANGLAISES de 2 a 4 mots pour illustrer le CORPS de la "
        "video, dans l'ordre de la narration. Elles doivent etre TOUTES DIFFERENTES les "
        "unes des autres (8 angles/lieux/echelles distincts) : chaque plan de la video "
        "est unique, aucun ne doit se repeter. C'est le point le plus important du visuel :\n"
        "  * PENSE EN SCENE, PAS EN MOT ISOLE. Ne prends jamais un mot de la phrase pour en "
        "faire une requete. Demande-toi : 'que verrait une camera pendant cette phrase ?'\n"
        "  * PIEGE A EVITER (erreurs reelles constatees) : 'paralysie du corps' a donne une "
        "femme avec un pistolet de massage ; 'processus biologique' a donne un mannequin "
        "d'anatomie en plastique. Ces plans detruisent l'immersion. La requete doit decrire "
        "une SCENE REELLE et CONCRETE liee au sens, jamais le concept abstrait lui-meme.\n"
        "  * CONTINUITE : les 5 scenes doivent sembler tournees par le meme cameraman, dans "
        "le meme registre (meme type de lieu, meme echelle de plan, meme ambiance). "
        "Reste sur des gros plans humains reels ou des scenes du quotidien.\n"
        "  * CLARTE : comprehensible en 0,5 seconde. Rien d'abstrait ni de symbolique.\n"
        "  * METAPHORES ET COMPARAISONS : ne cherche JAMAIS l'objet de comparaison. "
        "Quand le texte dit 'de la taille d'une orange', 'solide comme de l'acier', "
        "'gros comme un bus', il ne faut PAS de requete 'orange fruit', 'steel bar' ou "
        "'city bus' : le spectateur verrait un fruit au milieu d'un short sur les poumons. "
        "Illustre le PHENOMENE REEL decrit : 'compressed human lungs', "
        "'high water pressure', 'chest under pressure'.\n"
        "  * MOTS ABSTRAITS = LE PIEGE N°1. 'barriere', 'bouclier', 'cle', 'protection', "
        "'secret', 'alarme' NE se cherchent JAMAIS tels quels : la banque repond par un "
        "symbole (serrure, trousseau de cles, pancarte, ampoule) totalement hors-sujet. "
        "Traduis TOUJOURS l'abstraction en organe/matiere/geste REEL du sujet.\n"
        "  Cas vecu (short sur les ONGLES) : 'sans cette barriere' a ramene une serrure "
        "et des cles ; il fallait 'fingertip nail macro' ou 'finger touching surface'.\n"
        "  * COMPARAISON A UN AUTRE ETRE VIVANT : si le texte dit 'de la keratine, comme "
        "les cornes de rhinoceros', ne demande PAS de rhinoceros. Le spectateur croirait "
        "que la video parle de lui. Reste sur le sujet : 'human nail keratin macro'.\n"
        "  * DISTINGUE LE SUJET DE SES VOISINS. Demande-toi a chaque requete : "
        "'quelle pratique/espece/epoque RESSEMBLE au sujet sans etre lui ?', et formule "
        "la requete pour l'exclure. C'est propre a CHAQUE video, jamais une liste figee : "
        "un apneiste n'a aucun equipement (donc pas de scuba ni de piscine), mais un "
        "short sur la plongee bouteille doit AU CONTRAIRE montrer le scaphandre. "
        "Reporte ces voisins dans negative_keywords, et le sujet dans anchor_keywords.\n"
        "  * ANACHRONISME = FAUTE GRAVE. Si le sujet est historique ou antique, aucune "
        "requete ne doit pouvoir ramener un objet moderne (voiture, pick-up, route "
        "goudronnee, vetements actuels, ville contemporaine) ni un decor d'une AUTRE "
        "civilisation (un temple grec pour un sujet romain, par exemple). "
        "Erreurs reelles constatees sur un sujet Pompei : un pick-up blanc sur une piste, "
        "des colonnes de temple grec, une fumee de feu de foret moderne. "
        "Preferer alors des plans intemporels : ruines de pierre, cendre, lave, roche, "
        "poussiere, ciel, fumee volcanique, statues antiques.\n"
        "  * MONTRE LE LIEU, PAS UN DECOR NATUREL GENERIQUE. Un canyon, un desert ou une "
        "foret quelconque n'evoquent rien d'une cite antique. Si le sujet est un lieu "
        "(Pompei, une ville, un site), demande explicitement ce lieu ou son type : "
        "'roman ruins paved street', 'ancient columns forum', pas 'rocky canyon'.\n"
        "  * PIEGE ATELIER D'ART : ne demande jamais un objet en train d'etre FABRIQUE "
        "('plaster cast', 'statue carving') — la banque renvoie un cours de poterie. "
        "Demande l'objet FINI et en situation : 'pompeii victim body cast museum'.\n"
        "  * INTERDITS ABSOLUS dans les requetes : mannequin, plastic model, anatomy model, "
        "massage, massage gun, fitness, gym, workout, classroom, school, teacher, business "
        "meeting, office, presentation, handshake, diagram, illustration, animation, "
        "3d render, concept.\n"
        "  * Prefere le corps humain filme en vrai : 'man sleeping bed night', 'close up eye "
        "blinking', 'hand trembling', 'person waking up sweating', 'city street at dawn'), "
        "historical (true si le sujet est historique/antique/prehistorique — donc si "
        "toute presence humaine MODERNE a l'image serait un anachronisme ; false pour "
        "les sujets contemporains : le corps, l'argent, la vie quotidienne, les animaux). "
        "Si true, chaque requete doit contenir un mot qui ANCRE le sujet (volcanic, "
        "ancient, roman, ruins, pompeii...) : un mot generique seul ramene du moderne. "
        "Le mot 'smoke' est INTERDIT (il ramene un feu de foret) -> ecris "
        "'volcanic ash cloud'. Respecte aussi le TYPE reel du phenomene : le Vesuve est "
        "un volcan EXPLOSIF (colonne de cendres, nuee ardente), pas un volcan effusif "
        "hawaiien — donc pas de 'lava flow' en paysage desertique), "
        "negative_keywords (liste de 5 a 10 mots ou expressions ANGLAISES qui, s'ils "
        "apparaissent dans le titre/les tags d'une video de banque d'images, signalent "
        "un plan HORS-SUJET pour CE sujet PRECIS.\n"
        "  *** PRINCIPE : vise les VOISINS CONFONDABLES — ce qui ressemble au sujet mais "
        "n'est PAS lui. C'est la premiere cause de plan absurde. *** Ces exclusions sont "
        "PROPRES A CHAQUE VIDEO : un mot interdit ici peut etre obligatoire ailleurs.\n"
        "  - apnee -> ['scuba', 'oxygen tank', 'snorkeling', 'swimming pool'] "
        "(l'apneiste n'a aucun equipement)\n"
        "  - plongee BOUTEILLE -> l'inverse : ['freediving', 'breath hold'] "
        "(et surtout PAS 'scuba', qui est le sujet !)\n"
        "  - animal sauvage -> les especes voisines et la captivite : "
        "['leopard', 'tiger', 'aquarium', 'zoo enclosure']\n"
        "  - savant / epoque -> les machines modernes : "
        "['laser cutting', 'cnc machine', 'modern factory']\n"
        "  - civilisation -> les autres civilisations : "
        "['roman ruins', 'greek temple', 'medieval castle']\n"
        "  - phenomene naturel -> les phenomenes voisins : pour un volcan explosif, "
        "['hawaiian lava flow', 'wildfire', 'forest fire']\n"
        "  REGLE ABSOLUE : ne mets JAMAIS un mot qui decrit le sujet lui-meme ni un "
        "synonyme de ce que la video doit montrer.), "
        "anchor_keywords (liste de 2 a 4 mots ANGLAIS qui DOIVENT apparaitre dans les "
        "plans pour qu'on reconnaisse le sujet — l'inverse des negative_keywords. "
        "Ex apnee : ['freediver', 'underwater', 'deep']. Ex gorille : ['gorilla', "
        "'silverback']. Ex Pompei : ['pompeii', 'ancient', 'volcanic']), "
        "allow_keywords (liste EVENTUELLEMENT VIDE de mots normalement bannis par le "
        "systeme mais qui sont ici le SUJET MEME, donc a autoriser. "
        "Ex sujet 'les aquariums geants' -> ['aquarium'] ; sujet 'le fond vert au cinema' "
        "-> ['green screen'] ; sujet 'la musculation' -> ['gym', 'bodybuilder']. "
        "Laisse [] si aucun mot banni ne decrit le sujet), "
        "reveal_visual_plan (un objet avec la meme structure que visual_plan, montrant "
        "le sujet explicitement en gros plan au moment de la chute), "
        "reveal_stock (UNE requete ANGLAISE de 2 a 4 mots qui montre EXPLICITEMENT le "
        f"sujet ({topic}) en gros plan. C'est le plan de la REVELATION : au moment precis "
        "ou la voix nomme enfin le sujet, le spectateur doit le VOIR. "
        "Si le sujet est 'les requins', mets 'great white shark closeup' — surtout pas un "
        "oeil humain ou une metaphore. C'est le seul endroit du script ou le visuel doit "
        "etre 100%% litteral), "
        "keywords (liste de 3 mots-cles anglais simples, repli si scenes echoue), "
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
            # quota epuise, modele absent, ou surcharge passagere de Google (503) :
            # dans tous ces cas le modele suivant peut repondre. Sans le 503, une
            # simple pointe de charge chez Google faisait echouer toute la video.
            if any(x in str(e) for x in ("RESOURCE_EXHAUSTED", "429", "NOT_FOUND", "404",
                                         "UNAVAILABLE", "503", "INTERNAL", "500")):
                print(f"[script] modele {m} indispo ({str(e)[:50]}) -> suivant")
                continue
            raise
    if resp is None:
        raise RuntimeError("Tous les modeles Gemini sont epuises pour aujourd'hui.")
    raw = resp.text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(raw)
    # Gemini renvoie parfois une LISTE de phrases au lieu d'une string -> on aplatit.
    for k in ("title", "hook", "tension", "body", "revelation", "loop", "overlay", "sfx"):
        v = data.get(k)
        if isinstance(v, list):
            data[k] = " ".join(str(x).strip() for x in v if str(x).strip())
        elif v is not None and not isinstance(v, str):
            data[k] = str(v)
    data = _enforce_script_contract(client, data)
    print(f"[script] genere : {data['title']}")
    for i, h in enumerate(data.get("hook_variants", []), 1):
        mark = ">>" if h.strip() == data.get("hook", "").strip() else "  "
        print(f"  {mark} hook {i}: {h}")
    if data.get("overlay"):
        print(f"[overlay] plein ecran 0-{OVERLAY_UNTIL}s : « {data['overlay']} »")
    if data.get("virality") is not None:
        print(f"[viralite] {data['virality']}/100 - {data.get('virality_reason', '')}")
    return data


def _normalised_sentence(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text).lower()))


def _facts_from_legacy(data: dict) -> list[str]:
    """Compatibilite avec les JSON Gemini produits avant l'introduction de `facts`."""
    candidates = []
    for key in ("tension", "body", "revelation"):
        value = str(data.get(key) or "").strip()
        candidates.extend(s.strip() for s in re.split(r"(?<=[.!?])\s+", value) if s.strip())
    out = []
    for sentence in candidates:
        if _normalised_sentence(sentence) and sentence not in out:
            out.append(sentence)
        if len(out) == 3:
            break
    return out


def _script_contract_errors(data: dict) -> list[str]:
    """Regles qui ne doivent pas dependre de l'obeissance approximative du LLM."""
    errors = []
    facts = data.get("facts")
    if not isinstance(facts, list) or len([f for f in facts if str(f).strip()]) != 3:
        errors.append("facts: exactement trois phrases est requis")
    elif len({_normalised_sentence(f) for f in facts if _normalised_sentence(f)}) != 3:
        errors.append("facts: les trois faits doivent etre distincts")
    hook = _normalised_sentence(data.get("hook", ""))
    loop = _normalised_sentence(data.get("loop", ""))
    if not hook or not loop:
        errors.append("hook ou loop manquant")
    else:
        import difflib
        ratio = difflib.SequenceMatcher(a=hook.split(), b=loop.split(), autojunk=False).ratio()
        hook_ngrams = {" ".join(hook.split()[i:i + 3]) for i in range(max(0, len(hook.split()) - 2))}
        loop_ngrams = {" ".join(loop.split()[i:i + 3]) for i in range(max(0, len(loop.split()) - 2))}
        if hook == loop or ratio >= 0.72 or hook_ngrams & loop_ngrams:
            errors.append("loop trop proche du hook")
    return errors


def _repair_script_contract(client, data: dict, errors: list[str]) -> dict | None:
    """Demande une correction minimale seulement si le contrat editorial est enfreint."""
    prompt = (
        "Corrige uniquement ce JSON de script YouTube Short et reponds uniquement en JSON. "
        "Problemes detectes : " + "; ".join(errors) + ". "
        "Conserve le hook et le sujet. Retourne exactement les cles facts et loop : "
        "facts doit etre une liste de exactement 3 faits courts, precis et verifiables; "
        "loop doit se raccorder au sens du hook sans reprendre ses mots, sa structure ou "
        "une sequence de trois mots. JSON source : " + json.dumps(data, ensure_ascii=False)
    )
    for model in GEMINI_MODELS:
        try:
            raw = client.models.generate_content(model=model, contents=prompt).text.strip()
            raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            repaired = json.loads(raw)
            if isinstance(repaired, dict):
                return repaired
        except Exception as e:
            print(f"[script] correction {model} indisponible ({str(e)[:50]})")
    return None


def _enforce_script_contract(client, data: dict) -> dict:
    """Valide puis corrige le nombre de faits et l'absence de boucle repetee."""
    errors = _script_contract_errors(data)
    if errors:
        print(f"[script] contrat a corriger : {', '.join(errors)}")
        repaired = _repair_script_contract(client, data, errors)
        if repaired:
            data.update({k: v for k, v in repaired.items() if k in {"facts", "loop"}})
    if not isinstance(data.get("facts"), list):
        data["facts"] = _facts_from_legacy(data)
    data["facts"] = [str(f).strip() for f in data.get("facts", []) if str(f).strip()][:3]
    # Si Gemini est indisponible pendant une correction, le pipeline reste executable
    # sans jamais republier mot pour mot l'introduction a la fin.
    if _script_contract_errors(data):
        if len(data["facts"]) < 3:
            legacy = _facts_from_legacy(data)
            data["facts"] = (data["facts"] + [f for f in legacy if f not in data["facts"]])[:3]
        remaining = _script_contract_errors({**data, "facts": data["facts"]})
        if any("loop" in err or "hook" in err for err in remaining):
            data["loop"] = "… au moment où"
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


def _remap_words(words, narration: str):
    """Réimpose le texte EXACT du script sur les timings de Whisper.

    Whisper deforme les mots rares ou rapides ('broyer la chair' -> 'broil a chere')
    et ces fautes s'affichent en gros a l'ecran. On connait le texte reel : on ne
    garde de Whisper que ce qu'il fait bien, le CHRONOMETRAGE.
    """
    import difflib
    ref = [w for w in narration.split() if w.strip()]
    if not words or not ref:
        return words
    nrm = lambda s: re.sub(r"[^\w]", "", s.lower())
    sm = difflib.SequenceMatcher(a=[nrm(w["word"]) for w in words],
                                 b=[nrm(r) for r in ref], autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                out.append({**words[i1 + k], "word": ref[j1 + k]})
        elif tag == "replace":
            n, m = i2 - i1, j2 - j1
            for k in range(n):                    # repartit les vrais mots sur les timings
                s, e = j1 + k * m // n, j1 + (k + 1) * m // n
                if s < e:
                    out.append({**words[i1 + k], "word": " ".join(ref[s:e])})
        elif tag == "delete":
            pass                                  # Whisper a entendu un mot en trop
        # 'insert' : mot du script sans timing -> repris par le groupe voisin
    fixed = sum(1 for a, b in zip(out, words) if a["word"] != b["word"])
    if fixed:
        print(f"[subs] {fixed} mots corriges depuis le script (transcription Whisper)")
    return out or words


# Mots de liaison : une ligne qui se TERMINE par l'un d'eux casse le sens et se lit
# comme une faute ("NUEE TOXIQUE A" / "500 DEGRES A"). Ils ouvrent la ligne suivante.
_LINKERS = {"a", "à", "de", "du", "des", "la", "le", "les", "un", "une", "et", "ou",
            "qui", "que", "dont", "où", "quand", "comme", "mais", "car", "donc",
            "dans", "sur", "sous", "en", "au", "aux", "ce", "cet", "cette",
            "ses", "son", "sa", "leur", "leurs", "par", "pour", "avec", "sans",
            "ne", "ni", "plus", "tes", "ton", "ta", "mes", "mon", "d", "l", "n",
            # auxiliaires : separer "ont | explose" casse le verbe en deux lignes
            "est", "sont", "ont", "était", "étaient", "avait", "avaient",
            "sera", "seront", "va", "vont", "peut", "peuvent"}


def _fit_group(words, i, step, hard_max=2):
    """Construit un bloc de sous-titre de deux mots au plus.

    Le rythme demande ici prime sur la construction grammaticale : etendre un bloc
    pour embarquer une preposition faisait reapparaitre des lignes de 3-4 mots.
    Une ponctuation peut seulement raccourcir le bloc, jamais l'allonger.
    """
    n = len(words)
    end = min(i + step, n)
    # 1) une ligne ne melange pas la fin d'une phrase et le debut de la suivante
    #    ("DE SECONDE NON" = fin de la 1re phrase + "Non," qui ouvre la 2e)
    for k in range(i, end - 1):
        if re.search(r"[.!?]$", words[k]["word"].strip()):
            return k + 1
    def _is_linker(k):
        return re.sub(r"[^\w']", "", words[k]["word"].strip(),
                      flags=re.UNICODE).lower() in _LINKERS

    return min(end, i + hard_max)


def _emph_set(emphasis):
    s = set()
    for e in (emphasis or []):
        for w in re.findall(r"\w+", str(e).lower()):
            if len(w) > 1:
                s.add(w)
    return s


def _aligned_line(g, style, base_fs, emph, offset, next_start=None):
    """Une ligne de sous-titres depuis des mots alignes (timing reel + mots importants).

    `next_start` : debut du groupe suivant. On coupe la ligne juste avant, sinon deux
    groupes s'affichent au meme endroit en meme temps et deviennent illisibles.
    """
    start = offset + g[0]["start"]
    end = offset + g[-1]["end"] + 0.05
    if next_start is not None:
        end = min(end, offset + next_start - 0.01)
    end = max(end, start + 0.08)          # garde-fou : jamais de duree nulle/negative
    emph_fs = int(base_fs * 1.32)
    toks = []
    for j, w in enumerate(g):
        d = (g[j + 1]["start"] - w["start"]) if j < len(g) - 1 else (w["end"] - w["start"])
        cs = max(1, round(d * 100))
        # on retire la ponctuation MAIS on garde l'apostrophe : la supprimer donnait
        # "LINTERIEUR" au lieu de "L'INTERIEUR" a l'ecran.
        disp = re.sub(r"""[.,!?;:"]""", "", w["word"]).replace("’", "'").upper()
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


def build_subtitles(timeline, words, total, ass_path, emphasis=None, offset=0.0, group=2,
                    overlay: str | None = None):
    """Sous-titres karaoke. Si `words` (alignes) fournis -> sync mot par mot + mots
    importants mis en valeur. Sinon fallback sur le timing estime de la timeline.

    `overlay` : accroche de 3-5 mots affichee PLEIN ECRAN des la frame 0. Pendant
    qu'elle est visible, les sous-titres du hook descendent pour ne pas la recouvrir.
    """
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Def,{FONT},130,&H0000FFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,2,0,1,8,5,5,90,90,0,1
Style: Hook,{FONT},156,&H0000FFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,2,0,1,11,6,5,90,90,0,1
Style: HookLow,{FONT},150,&H0000FFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,2,0,1,11,6,2,90,90,300,1
Style: Over,{FONT},{OVERLAY_FS},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,4,0,1,16,8,5,70,70,0,1
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
            # Hook et corps : deux mots max, centres, pour une lecture instantanee.
            step = HOOK_WORDS_PER_LINE if in_hook else min(SUBTITLE_WORDS_PER_LINE, group)
            end = _fit_group(words, i, step)      # ne coupe pas apres un mot de liaison
            g = words[i:end]
            step = end - i
            if in_hook:
                # tant que l'accroche plein ecran est affichee, on passe dessous
                low = overlay and (offset + words[i]["start"]) < OVERLAY_UNTIL
                style = "HookLow" if low else "Hook"
            else:
                style = "Def"
            base_fs = {"HookLow": 150, "Hook": 156}.get(style, 130)
            nxt = words[i + step]["start"] if i + step < len(words) else None
            lines.append(_aligned_line(g, style, base_fs, emph, offset, nxt))
            i += step
        last_end = offset + words[-1]["end"]
        mode = "aligne"
    else:                                       # --- fallback timing estime
        t = offset
        for it in timeline:
            if it["kind"] == "pause":
                t += it["dur"]
                continue
            if it["tone"] == "hook":
                style = "HookLow" if overlay else "Hook"
            else:
                style = "Def"
            lines += _karaoke_lines(it["text"], t, it["dur"], style,
                                    group=min(SUBTITLE_WORDS_PER_LINE, group))
            t += it["dur"]
        last_end = t
        mode = "estime"

    # --- accroche PLEIN ECRAN des la frame 0 (layer 2 = au-dessus de tout)
    if overlay and OVERLAY_ENABLED:
        txt = re.sub(r"\s+", " ", str(overlay)).strip().upper().rstrip(".")
        w = txt.split()
        if len(w) > 3:                       # 2 lignes equilibrees = lecture plus rapide
            half = (len(w) + 1) // 2
            txt = " ".join(w[:half]) + r"\N" + " ".join(w[half:])
        # punch-in immediat + micro-secousse : l'oeil est capte avant la 1re syllabe
        anim = (r"{\an5\fad(0,180)\fscx58\fscy58\t(0,110,\fscx108\fscy108)"
                r"\t(110,200,\fscx100\fscy100)\1c&H00FFFFFF&\3c&H000033FF&}")
        lines.append(f"Dialogue: 2,{_fmt_ass_time(0)},{_fmt_ass_time(OVERLAY_UNTIL)},Over,{anim}{txt}")

    if OUTRO_TEXT.strip():
        outro_start = last_end + 0.15
        outro_txt = (r"{\fad(350,400)\an5\fscx70\fscy70"
                     r"\t(0,400,\fscx100\fscy100)}" + OUTRO_TEXT)
        lines.append(
            f"Dialogue: 1,{_fmt_ass_time(outro_start)},{_fmt_ass_time(total - 0.1)},Outro,{outro_txt}")

    ass_path.write_text(header + "\n".join(lines), encoding="utf-8")
    print(f"[subs] {len(lines)} lignes ({mode}) -> {ass_path.name}")


# ---------------------------------------------------------------- 4. visuels
_clip_seq = 0


# Pollinations incruste "pollinations.ai" en bas a droite. Le parametre nologo=true
# de l'API ne le retire plus. Rogner le bas est la seule option : un filigrane
# visible signale une video automatisee au premier coup d'oeil.
AI_WATERMARK_CROP = 0.06             # part basse de l'image a supprimer


def _strip_watermark(path: Path) -> None:
    try:
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
            im.crop((0, 0, w, int(h * (1 - AI_WATERMARK_CROP)))).save(path, quality=95)
    except Exception:
        pass                          # une image non rognee vaut mieux que pas d'image
def fetch_ai_images(prompts: list[str], n: int = 3) -> list[Path]:
    """Genere des images IA (Pollinations : gratuit, sans cle) pour illustrer le hook.
    Le stock footage generique 'hurle PUB' au cerveau ; une image sur mesure, non.
    Les plans du hook durant ~0.5s, l'animation (zoom/glitch) les rend indiscernables
    d'un vrai plan video."""
    global _clip_seq
    if not AI_IMAGES:
        return []
    import urllib.parse
    out, why = [], ""
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
                _strip_watermark(dst)
                out.append(dst)
            elif not why:
                why = ("credits epuises (Pollinations est devenu payant)"
                       if ("402" in r.text or "balance" in r.text.lower())
                       else f"HTTP {r.status_code}")
        except Exception as e:
            why = why or type(e).__name__
    print(f"[visuels] [hook IA] {len(out)}/{min(n, len(prompts))} images generees"
          + (f" — {why} -> repli stock" if why else ""))
    return out


# Mots trop generiques pour identifier un sujet dans un slug Pexels.
_GENERIC = {"underwater", "closeup", "close", "up", "macro", "swimming", "swims",
            "water", "ocean", "sea", "daylight", "bright", "sharp", "view", "shot",
            "person", "human", "the", "a", "of", "in", "on", "with", "and",
            # adjectifs : 'great' faisait passer 'great-crested-grebe' pour un requin
            "great", "big", "large", "huge", "tiny", "small", "giant", "wild",
            "beautiful", "serene", "gracefully", "detailed", "extreme",
            "white", "black", "blue", "red", "green", "golden"}


# Une requete historique DOIT contenir un de ces mots, sinon la banque repond
# avec du moderne : "ash dust hand" ramene un cendrier et de la poudre coloree.
_ANCHORS = ("ancient", "antique", "roman", "ruin", "pompeii", "herculaneum",
            "volcan", "eruption", "vesuvius", "lava", "archaeolog", "temple",
            "statue", "marble", "column", "forum", "fresco", "amphitheater",
            "mosaic", "skull", "bone", "excavation", "medieval", "prehistoric")
# Mots trop ambigus pour etre laisses seuls sur un sujet historique.
_AMBIGUOUS = ("ash", "dust", "hand", "smoke", "powder", "fire", "sand", "clay",
              "cast", "cloth", "figure", "body", "face", "skin")


def _anchor_queries(queries: list[str]) -> list[str]:
    """Ecarte les requetes ambigues sans ancrage historique. On prefere avoir une
    requete de moins qu'un plan de cigarette au milieu d'un short sur Pompei."""
    kept, dropped = [], []
    for q in queries:
        ql = q.lower()
        if any(a in ql for a in _ANCHORS) or not any(w in ql for w in _AMBIGUOUS):
            kept.append(q)
        else:
            dropped.append(q)
    if dropped:
        print(f"[visuels] requetes trop vagues ecartees : {', '.join(dropped)}")
    return kept or queries          # jamais tout jeter


def _neg_words(negative: tuple[str, ...], protege: frozenset[str] = frozenset()) -> frozenset[str]:
    """Mots bannis issus des exclusions DU SUJET.

    Regle : on bannit chaque mot des exclusions, SAUF ceux qui decrivent le sujet
    lui-meme (ses ancrages). Les deux extremes etaient mauvais :
    - tout decouper bannissait "card" sur un short consacre aux cartes bancaires
      (Gemini exclut a raison "playing card", "sim card", "id card") ;
    - n'accepter que les mots isoles laissait passer un plongeur en bouteilles
      malgre l'exclusion "scuba diver", car "diver-exploring-reef" ne contient
      pas "scuba" et l'expression exigeait ses deux mots.
    Proteger les ancrages tranche : "card" survit quand la video parle de cartes,
    "diver" tombe quand elle parle de requins.
    """
    mots = set()
    for neg in negative:
        for w in re.findall(r"[a-z]+", neg.lower()):
            if len(w) > 2 and w not in protege:
                mots.add(w)
    return frozenset(mots)


def _drop_banned_queries(queries: list[str], banned: frozenset[str]) -> list[str]:
    """Ecarte les REQUETES contenant un mot interdit POUR CE SUJET. Filtrer les clips
    ne suffit pas : Gemini ecrivait 'freediver floating motionless pool' malgre la
    consigne, ce qui gaspille la recherche et ramene des bassins."""
    if not banned:
        return queries
    kept, dropped = [], []
    for q in queries:
        (dropped if banned & set(re.findall(r"[a-z]+", q.lower())) else kept).append(q)
    if dropped:
        print(f"[visuels] requetes hors-theme ecartees : {', '.join(dropped)}")
    return kept or queries


def _add_anchors(queries: list[str], anchors: tuple[str, ...]) -> list[str]:
    """Injecte les requetes d'ancrage DU SUJET, sans doublonner l'existant."""
    have = " ".join(queries).lower()
    return queries + [a for a in anchors if a.split()[0].lower() not in have]


def _drop_other_species(queries: list[str], subject: frozenset[str]) -> list[str]:
    """Sur un sujet animalier, ecarte les requetes qui nomment une AUTRE espece.
    Gemini propose volontiers 'lion roaring' pour un short sur le gorille : le
    spectateur voit alors un lion et ne comprend plus de quel animal on parle."""
    if not subject:
        return queries
    kept, dropped = [], []
    for q in queries:
        others = (ANIMAL_SPECIES & set(re.findall(r"[a-z]+", q.lower()))) - subject
        (dropped if others else kept).append(q)
    if dropped:
        print(f"[visuels] autres especes ecartees : {', '.join(dropped)}")
    return kept or queries


def _key_noun(query: str) -> str | None:
    """Mot le plus distinctif d'une requete ('great white shark closeup' -> 'shark').
    Sert a verifier que le clip renvoye montre VRAIMENT le sujet.
    En anglais le nom-tete est en fin de groupe : a longueur egale, le dernier gagne."""
    words = [w for w in re.findall(r"[a-z]+", query.lower())
             if w not in _GENERIC and len(w) > 2]
    if not words:
        return None
    return max(enumerate(words), key=lambda t: (len(t[1]), t[0]))[1]


STYLE_REJECTED = frozenset({
    "animation", "animated", "cartoon", "illustration", "illustrated", "graphic",
    "graphics", "template", "slideshow", "screen", "game", "gaming",
    "low", "resolution", "blurry", "blur", "out", "focus",
})
PREFERRED_VERTICAL_HEIGHT = 2160      # priorite aux masters 4K / 2K pour le crop 9:16
MAX_VERTICAL_ASPECT = 0.70             # refuse carre et paysage recadres artificiellement
# Controle sur une vraie frame, sans GPU ni modele local : il ecarte les videos
# totalement noires/blanches ou graphiquement plates. Desactive seulement via VISUAL_QA=0.
VISUAL_QA = os.getenv("VISUAL_QA", "1").strip().lower() not in {"0", "false", "no"}
VISUAL_QA_MIN_LUMA = 15
VISUAL_QA_MAX_LUMA = 235
VISUAL_QA_MIN_CONTRAST = 28


def _words(value) -> tuple[str, ...]:
    """Mots anglais normalises pour les contrats visuels et les slugs de banques."""
    if value is None:
        # str(None) donnait "none", un mot que le filtre exigeait ensuite dans
        # chaque slug : tous les clips du corps etaient rejetes (require=None).
        return ()
    if isinstance(value, (list, tuple, set)):
        value = " ".join(str(v) for v in value)
    return tuple(w for w in re.findall(r"[a-z0-9]+", str(value).lower()) if len(w) > 2)


def _normalise_visual_plan(raw) -> dict:
    """Convertit un ancien mot-cle ou le nouveau contrat Gemini en plan exploitable.

    Le nouveau contrat distingue ce qui DOIT etre visible de la requete de recherche.
    Ainsi, une requete concrete peut etre longue sans laisser Pexels remplacer le sujet
    par une ambiance ou un symbole. Les anciens JSON restent lisibles sans migration.
    """
    raw = raw if isinstance(raw, dict) else {"query": str(raw or "")}
    query = re.sub(r"\s+", " ", str(raw.get("query") or raw.get("search") or "").strip())
    subject = str(raw.get("concrete_subject") or raw.get("subject") or "").strip()
    required = list(_words(raw.get("must_include", [])))
    if not required and subject:
        required = list(_words(subject))
    # Une requete en TEXTE SIMPLE ne se voit imposer aucune ancre dure : _key_noun
    # choisit le mot le plus rare, souvent un adjectif ("bee wings vibrating flower"
    # -> "vibrating"), qu'aucun clip ne porte. La pertinence de ces requetes est
    # deja assuree plus bas par key_tok (phase 0, relache en phase 1).
    # Seul le contrat structure de Gemini fixe des ancres non negociables.
    forbidden = list(_words(raw.get("must_not_include", [])))
    look = list(_words(raw.get("look", [])))
    shot = str(raw.get("shot") or "").lower().strip()
    # Filet deterministe pour le cas le plus frequent : Pexels interprete "secure
    # building" comme de l'immobilier ou des bureaux. Si Gemini oublie de traduire
    # cette abstraction, on force une representation visible et recherchable.
    query_tokens = set(_words(query))
    security_terms = {"secure", "secured", "security", "protected", "protection"}
    concrete_security = {"vault", "door", "gold", "guard", "bunker", "bank", "military"}
    if query_tokens & security_terms and not query_tokens & concrete_security:
        query = "bank vault door closeup"
        required = [w for w in required if w not in security_terms | {"building", "place", "site"}]
        required = list(dict.fromkeys(required + ["vault", "door"]))
        shot = "closeup"
    if shot in {"close up", "close-up", "macro", "detail"} and "closeup" not in query.lower():
        query = f"{query} closeup".strip()
    return {
        "query": query,
        "must_include": tuple(dict.fromkeys(required)),
        "must_not_include": tuple(dict.fromkeys(forbidden)),
        "look": tuple(dict.fromkeys(look)),
        "shot": shot,
    }


def _visual_plans(data: dict, structured_key: str, legacy_key: str,
                  fallback: list[str] | tuple[str, ...] = ()) -> list[dict]:
    """Lit les plans structures; retombe sans surprise sur les champs historiques."""
    raw = data.get(structured_key) or data.get(legacy_key) or fallback
    if isinstance(raw, (str, dict)):
        raw = [raw]
    return [p for p in (_normalise_visual_plan(v) for v in (raw or [])) if p["query"]]


def _plan_queries(plans: list[dict]) -> list[str]:
    return [p["query"] for p in plans]


def _filter_visual_plans(plans: list[dict], query_filter) -> list[dict]:
    """Applique les protections historiques/negatives sans perdre le contrat du plan."""
    allowed = set(query_filter(_plan_queries(plans)))
    return [plan for plan in plans if plan["query"] in allowed]


def _candidate_quality(vid: dict, plan: dict) -> int:
    """Classe les resultats Pexels avant filtrage semantique.

    Pexels ne fournit ni score cinematographique ni detection de cadrage. On exploite
    donc les signaux fiables exposes par l'API : portrait natif, hauteur, et termes
    explicites de gros plan. Le controle visuel lourd reste une etape optionnelle a
    ajouter avec CLIP/Vision, jamais une promesse basee sur le seul slug.
    """
    files = [f for f in vid.get("video_files", [])
             if f.get("height", 0) > 0 and f.get("width", 0) > 0
             and f["height"] >= f["width"] and f["width"] / f["height"] <= MAX_VERTICAL_ASPECT]
    if not files:
        return -10_000
    height = max(f["height"] for f in files)
    text = _vid_text(vid)
    score = min(height, 4320) // 120
    if height >= PREFERRED_VERTICAL_HEIGHT:
        score += 12
    if plan["shot"] in {"closeup", "close up", "close-up", "macro", "detail"}:
        score += 8 if any(w in text for w in ("closeup", "close-up", "macro", "detail")) else 0
    if any(w in text for w in ("cinematic", "dramatic", "moody", "dark")):
        score += 3
    return score


def _passes_visual_qa(path: Path) -> tuple[bool, str]:
    """Controle visuel leger sur une frame reelle avec FFmpeg signalstats.

    Les descriptifs Pexels ne suffisent pas a repérer un plan noir, surexpose ou
    presque uniforme. Ce test CPU n'affirme pas reconnaitre le sujet (seul CLIP/Vision
    le ferait), mais rejette les medias illisibles avant le montage.
    """
    if not VISUAL_QA:
        return True, "qa desactivee"
    try:
        probe = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "info", "-ss", "0.5", "-i", str(path),
             "-frames:v", "1", "-vf", "scale=160:284,signalstats,metadata=print", "-f", "null", "-"],
            capture_output=True, text=True, timeout=20)
        text = probe.stderr
        values = {}
        for key in ("YLOW", "YAVG", "YHIGH"):
            hits = re.findall(rf"lavfi\.signalstats\.{key}=([0-9.]+)", text)
            if hits:
                values[key] = float(hits[0])
        if len(values) != 3:
            return True, "telemetrie indisponible"
        contrast = values["YHIGH"] - values["YLOW"]
        if not VISUAL_QA_MIN_LUMA <= values["YAVG"] <= VISUAL_QA_MAX_LUMA:
            return False, f"luminance {values['YAVG']:.0f}"
        if contrast < VISUAL_QA_MIN_CONTRAST:
            return False, f"contraste {contrast:.0f}"
        return True, "ok"
    except Exception:
        # Une erreur de telemetrie ne doit jamais faire tomber toute une generation.
        return True, "qa indisponible"


# Prefixes de slug deja pris pour CETTE video : evite 3 angles du meme monument.
# Reinitialise au debut de main().
_seen_prefixes: set[str] = set()
_seen_ids: set[int] = set()        # id Pexels : jamais deux fois LE MEME clip


def _slug_prefix(slug: str, exclude: set[str] = frozenset(), k: int = 3) -> str:
    """Signature de scene d'un clip Pexels : 3 premiers mots significatifs du slug.
    'ancient-roman-ruins-in-hierapolis' et 'ancient-roman-ruins-at-sunset' partagent
    'ancient|roman|ruins' -> on n'en garde qu'un.

    `exclude` retire les mots de la REQUETE : sur un sujet 'gorille', tous les slugs
    contiennent 'gorilla', ce mot ne distingue donc aucune scene. Sans ca le filtre
    rejetait 103 clips par video. Renvoie "" quand il ne reste pas assez de signal
    pour juger (on ne dedoublonne alors pas).
    """
    words = [w for w in re.split(r"[^a-z]+", slug)
             if w and w not in _GENERIC and len(w) > 2 and w not in exclude]
    return "|".join(words[:k]) if len(words) >= 2 else ""


def _negative_hit(slug_tokens: set[str], negative: tuple[str, ...]) -> bool:
    """Mot-cle negatif fourni par Gemini : multi-mots = tous les mots doivent y etre."""
    for neg in negative:
        parts = [w for w in re.findall(r"[a-z]+", neg.lower()) if len(w) > 2]
        if parts and all(p in slug_tokens for p in parts):
            return True
    return False


def _vid_text(vid: dict) -> str:
    """Texte descriptif NORMALISE d'un clip, quelle que soit la banque.

    Pexels decrit par le slug de l'URL ('fish-tank-123'), Pixabay par des tags
    separes par des virgules ('fish, tank, water'). On ramene tout a une forme
    a tirets et on encadre le resultat, pour qu'un motif comme "fish-tank" ou
    "-zoo-" matche a l'identique sur les deux sources, y compris en fin de chaine.
    """
    raw = f"{vid.get('tags') or ''} {vid.get('url') or ''}".lower()
    return "-" + re.sub(r"[^a-z0-9]+", "-", raw).strip("-") + "-"


# Mots de plomberie presents dans CHAQUE descriptif : ils ne decrivent rien mais
# gonflaient la taille apparente du texte, ce qui declenchait a tort la regle
# "descriptif long -> exiger 2 recoupements" sur de simples slugs Pexels.
_VID_NOISE = frozenset({"", "https", "http", "www", "pexels", "pixabay", "com",
                        "video", "videos", "photo", "free", "stock", "download"})


def _vid_tokens(vid: dict) -> set[str]:
    """Mots entiers du descriptif — base de tous les filtres contextuels."""
    return set(re.split(r"[^a-z0-9]+", _vid_text(vid)))


def _vid_content_tokens(tokens: set[str]) -> set[str]:
    """Tokens qui DECRIVENT reellement le clip : sans plomberie ni identifiants."""
    return {t for t in tokens if t not in _VID_NOISE and not t.isdigit()}


def fetch_pixabay_videos(query: str, per_page: int = 15) -> list[dict]:
    """Cherche sur Pixabay et normalise au format Pexels (`video_files`, `url`, `id`).

    Pixabay n'expose PAS de filtre d'orientation utilisable : ~90% de son catalogue
    est en 16:9. On remonte donc les rares verticales en tete — le reste sert de
    complement, au prix d'un recadrage serre.
    """
    key = os.getenv("PIXABAY_API_KEY")
    if not key:
        return []
    try:
        r = requests.get("https://pixabay.com/api/videos/",
                         params={"key": key, "q": query, "per_page": per_page,
                                 "safesearch": "true"}, timeout=30)
        hits = r.json().get("hits", [])
    except Exception:
        return []

    out = []
    for h in hits:
        vids = h.get("videos") or {}
        files = []
        for name in ("large", "medium", "small"):
            f = vids.get(name)
            if f and f.get("url"):
                files.append({"link": f["url"], "width": f.get("width", 0),
                              "height": f.get("height", 0)})
        if not files:
            continue
        best = max(files, key=lambda f: f["height"])
        out.append({
            # prefixe : les id Pixabay et Pexels vivent dans le meme espace de noms
            "id": f"pixabay-{h.get('id')}",
            "url": h.get("pageURL") or "",
            "tags": h.get("tags") or "",
            "video_files": files,
            "vertical": best["height"] > best["width"],
        })
    # VERTICAL UNIQUEMENT : une source 16:9 recadree en 9:16 ne garde que ~32% de la
    # largeur et subit un upscale de 1.78x -> sujet decentre et image molle.
    return [v for v in out if v["vertical"]]


# Vocabulaire de cadrage : utile a Gemini pour decrire une intention, inutile —
# voire nuisible — dans une recherche de banque d'images.
_SHOT_WORDS = frozenset({"macro", "closeup", "close", "detail", "shot", "view",
                         "angle", "extreme", "slow", "motion", "footage", "scene",
                         "background", "cinematic", "dramatic", "dark", "light"})


def _short_query(kw: str) -> str:
    """Version courte et cherchable d'une requete.

    Pexels et Pixabay font de la correspondance approximative : plus la requete
    est longue, plus la reponse derive. Mesure du 12/09/2026 : "macro gold smart
    card chip detail" ramenait du tissu, une machine a coudre et de la peau —
    zero mot commun avec la requete. On garde donc les 2 derniers mots porteurs
    (l'anglais place le nom principal en fin de groupe) et on jette le cadrage.
    """
    mots = [w for w in re.findall(r"[a-z]+", kw.lower())
            if len(w) > 2 and w not in _SHOT_WORDS and w not in _GENERIC]
    # les DEUX PREMIERS : Gemini place le sujet en tete ("ants crawling ground",
    # "credit card tapping pos"). Garder la fin perdait justement le sujet.
    return " ".join(mots[:2]) if len(mots) > 2 else ""


def fetch_clips(keywords: list[dict | str], n: int = 4, label: str = "",
                require: str | None = None,
                banned_tokens: frozenset[str] = frozenset(),
                strict: bool = False,
                negative: tuple[str, ...] = (),
                allow: tuple[str, ...] = (),
                dedup: bool = True,
                species: frozenset[str] = frozenset()) -> list[Path]:
    """Telecharge n clips verticaux Pexels, filtres sur le slug de leur URL.

    require        : mot OBLIGATOIRE dans le slug (frame 0 et revelation)
    banned_tokens  : bans contextuels par mot entier (sujets historiques)
    strict         : exige >=1 mot significatif de la requete, sans repli
    negative       : mots-cles negatifs propres au sujet, fournis par Gemini
    allow          : bans globaux a LEVER car ils decrivent le sujet meme
                     (un short sur les aquariums a le droit de montrer un aquarium)
    dedup          : interdit deux clips partageant les 3 premiers mots de slug
    species        : espece(s) du SUJET de la video. Sans elle, une requete de
                     scene qui ne nomme pas l'animal ("pollen macro") faisait
                     rejeter tous les clips du sujet comme "autre espece".
    """
    # un ban global ne s'applique pas s'il decrit precisement le sujet de la video
    bans = tuple(b for b in BANNED_VISUALS
                 if not any(a in b or b in a for a in allow))
    global _clip_seq
    key = os.getenv("PEXELS_API_KEY")
    if not key:
        print("[visuels] pas de PEXELS_API_KEY -> fond noir")
        return []
    clips, used = [], []
    src = {"pexels": 0, "pixabay": 0}
    rejected = off_topic = dup = landscape = visual_bad = 0
    # VISUAL_DEBUG=1 -> dit pour CHAQUE clip pourquoi il est ecarte. Ce filtre est
    # la piece la plus subtile du projet ; sans trace, chaque reglage se debogue
    # a l'aveugle (il a deja masque quatre bugs qui vidaient le corps des videos).
    _dbg = bool(os.getenv("VISUAL_DEBUG"))

    def _drop(reason: str, text: str) -> None:
        if _dbg:
            print(f"    rejet {reason:26} {text[36:104]}")
    # arrondi au SUPERIEUR : on veut de quoi ne jamais repeter un plan dans la video
    per_kw = max(1, -(-n // max(1, len(keywords))))
    for raw_plan in keywords:
        if len(clips) >= n:
            break
        structured = isinstance(raw_plan, dict)
        plan = _normalise_visual_plan(raw_plan)
        kw = plan["query"]
        if not kw:
            continue
        got = 0
        # Une requete longue fait DERIVER la recherche des banques : mesure du
        # 12/09/2026, "macro gold smart card chip detail" ramenait du tissu, une
        # machine a coudre et de la peau, sans un seul mot commun. Si la requete
        # complete ne donne rien, on retente avec sa version courte.
        for kw_try in (kw, _short_query(kw)):
            if got or not kw_try:
                break
            try:
                r = requests.get(
                    "https://api.pexels.com/videos/search",
                    headers={"Authorization": key},
                    # on demande large : le filtre anti hors-sujet en elimine une partie
                    params={"query": kw_try, "orientation": "portrait", "per_page": 15, "size": "large"},
                    timeout=30,
                )
                vids = r.json().get("videos", [])
            except Exception:
                vids = []
            # CATALOGUE HYBRIDE : Pixabay double le vivier. Pexels d'abord (100% vertical),
            # Pixabay ensuite (verticales en tete, paysage en complement).
            vids += fetch_pixabay_videos(kw_try)
            # Les meilleurs masters verticaux arrivent avant les variantes molles ou
            # recadrees. Cela ne pretend pas reconnaitre une image : Pexels ne fournit
            # pas ce signal; on se limite aux metadonnees reelles de l'API.
            vids.sort(key=lambda v: _candidate_quality(v, plan), reverse=True)
            # Une liste noire ne suffit pas : un incendie titre "smoke-rising-over-hill"
            # ne contient aucun mot interdit. On EXIGE donc que le clip parle du sujet
            # demande (phase 0) ; si la banque n'a rien, on relache (phase 1).
            # On exige qu'AU MOINS UN mot significatif de la requete soit dans le titre du
            # clip. Exiger le seul mot principal etait trop rigide : "ancient columns forum"
            # rejetait "ancient-roman-ruins-in-hierapolis", pourtant ideal.
            q_tokens = {w for w in re.findall(r"[a-z]+", kw_try.lower())
                        if w not in _GENERIC and len(w) > 2}
            kw_words = set(re.findall(r"[a-z]+", kw_try.lower()))
            key_tok = _key_noun(kw_try)          # mot le plus distinctif de la requete
            must_tokens = set(plan["must_include"])
            forbidden_tokens = set(plan["must_not_include"])
            require_tokens = set(_words(require)) if require else set()
            # espece(s) citee(s) par la requete : toute AUTRE espece est un hors-sujet
            q_species = (ANIMAL_SPECIES & kw_words) | species
            # phase 0 : pertinence + scenes inedites. phase 1 : on relache la seule
            # exigence d'inedit (la pertinence, elle, reste imposee en mode strict) —
            # sinon une banque pauvre en scenes variees nous laisse sans image du tout.
            for phase in (0, 1):
                if got:
                    break
                for vid in vids:
                    if got >= per_kw or len(clips) >= n:
                        break
                    # --- rejet des plans hors contexte (mannequin, massage, classe...)
                    # Pexels decrit ses clips par le slug de l'URL, Pixabay par ses tags :
                    # les deux sont fondus dans le meme texte pour un filtrage identique.
                    slug = _vid_text(vid)
                    # mot entier : "ancient-roman-ruins" ne doit pas matcher le token "man"
                    tokens = _vid_tokens(vid)
                    if any(b in slug for b in bans) or (banned_tokens & tokens):
                        _drop('ban global', slug)
                        rejected += 1
                        continue
                    # Styles graphiques / captures d'ecran : incompatibles avec le rendu
                    # organique de b-roll, quelle que soit la qualite de la resolution.
                    if STYLE_REJECTED & tokens:
                        _drop('style graphique', slug)
                        rejected += 1
                        continue
                    # mots-cles negatifs propres au sujet (fournis par Gemini)
                    if negative and _negative_hit(tokens, negative):
                        _drop('negatif du sujet', slug)
                        rejected += 1
                        continue
                    if forbidden_tokens & tokens:
                        _drop('interdit du plan', slug)
                        rejected += 1
                        continue
                    # ESPECE INCOMPATIBLE : "gorilla" ne doit pas ramener une panthere
                    other_species = (ANIMAL_SPECIES & tokens) - q_species
                    if other_species:
                        _drop(f'autre espece {sorted(other_species)}', slug)
                        rejected += 1
                        continue
                    # frame 0 / revelation : le sujet doit reellement apparaitre dans le clip
                    if require_tokens and not require_tokens.issubset(tokens):
                        _drop('mot requis absent', slug)
                        rejected += 1
                        continue
                    # Nouveau contrat : les ancres visuelles sont non negociables. Une
                    # ambiance de coffre sans porte ne peut pas illustrer "vault door".
                    # Les ancres du contrat Gemini sont imposees INTEGRALEMENT en phase 0.
                    # En phase 1 on se contente d'une seule d'entre elles : exiger les
                    # deux sans repli etait, de loin, la premiere cause de rejet (226 sur
                    # un seul sujet mesure) et laissait le corps de la video vide.
                    # Phase 1 : l'ancre n'est PLUS exigee du tout. Gemini choisit des
                    # ancres justes mais trop rares pour une banque d'images ("cells",
                    # "sparks", "eyelid", "bills") ; aucun clip ne les porte, et
                    # l'assouplissement "au moins une" ne servait a rien sur les plans
                    # a une seule ancre. La pertinence reste assuree par q_tokens.
                    if must_tokens and phase == 0:
                        if not must_tokens.issubset(tokens):
                            _drop(f'ancre {sorted(must_tokens)}', slug)
                            off_topic += 1
                            continue
                    # jamais deux fois le MEME clip, quelle que soit la phase
                    vid_id = vid.get("id")
                    if vid_id in _seen_ids:
                        dup += 1
                        continue
                    # Pertinence : imposee en phase 0, et en phase 1 si mode strict.
                    # Pixabay liste ~15 tags larges ("nature, blue, water, sky...") : un
                    # seul mot commun ne prouve rien (une Voie lactee passait pour de
                    # l'apnee via "blue"+"water"). On exige donc 2 recoupements quand le
                    # descriptif est long, 1 seul sur un slug Pexels concis.
                    # Phase 0 : on exige le mot DISTINCTIF de la requete. Compter 2 mots
                    # quelconques ne suffit pas — "freediver underwater rope line" laissait
                    # passer une slackline via "rope"+"line".
                    if phase == 0 and key_tok and key_tok not in tokens:
                        _drop(f'distinctif {key_tok!r}', slug)
                        off_topic += 1
                        continue
                    # Phase 1 = VRAI repli. Auparavant plan_strict (vrai des que Gemini
                    # renvoyait des ancrages, donc presque toujours) maintenait l'exigence
                    # des deux phases : une requete un peu abstraite ne ramenait alors
                    # aucun clip et le corps de la video restait vide.
                    need = 2 if len(_vid_content_tokens(tokens)) > 10 else 1
                    if phase == 1:
                        need = 1            # un seul mot commun suffit pour depanner
                    hits = len(q_tokens & tokens)
                    if q_tokens and hits < min(need, len(q_tokens)):
                        _drop(f'recoupements {hits}/{min(need, len(q_tokens))}', slug)
                        off_topic += 1
                        continue
                        continue
                    # meme scene qu'un clip deja pris (3 angles de la meme ruine).
                    # Contrainte SOUPLE : abandonnee en phase 1 pour ne pas finir sans plan.
                    pref = _slug_prefix(slug, exclude=kw_words)
                    if phase == 0 and dedup and pref and pref in _seen_prefixes:
                        dup += 1
                        continue
                    # JAMAIS de format horizontal : on ne garde que les fichiers dont la
                    # hauteur depasse la largeur (le carre reste tolere).
                    files = [f for f in vid["video_files"]
                             if f.get("height", 0) >= f.get("width", 0) > 0
                             and f["width"] / f["height"] <= MAX_VERTICAL_ASPECT]
                    if not files:
                        landscape += 1
                        continue
                    hd = [f for f in files if f.get("height", 0) >= PREFERRED_VERTICAL_HEIGHT]
                    # Master 4K/2K en priorite, sinon le vertical natif le plus defini.
                    best = (max(hd, key=lambda f: f["height"]) if hd
                            else max(files, key=lambda f: f.get("height", 0)))
                    dst = WORK / f"clip_{_clip_seq}.mp4"
                    _clip_seq += 1
                    try:
                        with requests.get(best["link"], stream=True, timeout=60) as s:
                            with open(dst, "wb") as f:
                                for c in s.iter_content(1 << 16):
                                    f.write(c)
                    except Exception:
                        continue
                    visual_ok, _visual_reason = _passes_visual_qa(dst)
                    if not visual_ok:
                        dst.unlink(missing_ok=True)
                        visual_bad += 1
                        continue
                    clips.append(dst)
                    used.append(kw_try)
                    src[("pixabay" if str(vid_id).startswith("pixabay") else "pexels")] += 1
                    _seen_ids.add(vid_id)
                    if dedup and pref:
                        _seen_prefixes.add(pref)
                    got += 1
    tag = f" [{label}]" if label else ""
    det = []
    if rejected:
        det.append(f"{rejected} hors-sujet")
    if off_topic:
        det.append(f"{off_topic} non pertinents")
    if dup:
        det.append(f"{dup} scenes deja vues")
    if landscape:
        det.append(f"{landscape} horizontaux")
    if visual_bad:
        det.append(f"{visual_bad} visuellement illisibles")
    rej = f" ({', '.join(det)} ecartes)" if det else ""
    mix = f" [pexels {src['pexels']} / pixabay {src['pixabay']}]" if any(src.values()) else ""
    print(f"[visuels]{tag} {len(clips)} clips{mix}{rej} : "
          f"{', '.join(dict.fromkeys(used)) or 'aucun'}")
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


def _snap(t: float, strong: list[float], soft: list[float],
          tol_strong: float = 0.55, tol_soft: float = 0.30) -> float:
    """Cale un instant de coupe sur la respiration de la voix.

    Une coupe au milieu d'un mot se voit et se sent. On cherche d'abord une fin de
    PHRASE proche, sinon une fin de MOT ; sinon on garde l'instant demande.
    """
    for pts, tol in ((strong, tol_strong), (soft, tol_soft)):
        if not pts:
            continue
        best = min(pts, key=lambda p: abs(p - t))
        if abs(best - t) <= tol:
            return best
    return t


def assemble(audio: Path, ass: Path, clips: list[Path], out: Path, total: float,
             reveal_clip: Path | None = None, reveal_at: float | None = None,
             reveal_dur: float = 0.0, cuts_strong: list[float] | None = None,
             cuts_soft: list[float] | None = None, pin_first: bool = False):
    """Monte la video.

    `reveal_clip` est epingle a `reveal_at` : au moment ou la voix nomme enfin le
    sujet, l'image doit le MONTRER (dire 'c'est le requin' sur un oeil humain casse
    la revelation).
    `pin_first` : clips[0] est le plan valide pour la frame 0, il ouvre la video.
    `cuts_strong` / `cuts_soft` : fins de phrase / de mot ou caler les jump cuts.
    """
    cuts_strong = cuts_strong or []
    cuts_soft = cuts_soft or []
    ass_esc = str(ass).replace("\\", "/").replace(":", "\\:")  # echappement filtre ffmpeg
    lead_ms = int(LEAD * 1000)
    music = pick_music()
    # Rempli avec les coupes reelles (apres ffmpeg), puis reutilise par le mixage SFX.
    transition_times: list[float] = []

    # --- Fond video (clips ou noir), couvrant tout le total
    if clips:
        # Jump cuts toutes les CUT_MIN..CUT_MAX secondes (guide hook : 1 coupe / 2-4 s).
        # On cycle sur les clips avec des offsets differents -> pas de repetition visible.
        bw, bh = int(W * 1.25), int(H * 1.25)     # source plus grande pour zoomer sans perte
        # une image IA n'a pas de duree : on la traitera en boucle sur la duree du plan
        # le plan de revelation entre dans le pool, mais il est reserve a son instant
        reveal_idx = None
        if reveal_clip is not None and reveal_at is not None:
            clips = clips + [reveal_clip]
            reveal_idx = len(clips) - 1
        is_img = [c.suffix.lower() in (".jpg", ".jpeg", ".png") for c in clips]
        durations = [(999.0 if im else _duration(c)) for c, im in zip(clips, is_img)]
        n_pool = reveal_idx if reveal_idx is not None else len(clips)
        parts, i, elapsed, stalls = [], 0, 0.0, 0
        reveal_done = False
        # ORDRE NARRATIF, pas un tirage au sort : `clips` est deja range dans l'ordre
        # du recit (hook, puis les scenes telles que Gemini les a ecrites). Melanger
        # desynchronisait l'image du texte — le plan du rhinoceros tombait pendant que
        # la voix parlait des poumons. pop() prend la fin -> on inverse la liste.
        # AUCUN plan en double : chaque clip n'est tire qu'une fois.
        pool = list(range(n_pool))
        pool.reverse()
        # FRAME 0 : le plan qui contient le sujet ouvre la video (deja en fin de pile)
        if pin_first and n_pool and pool[-1] != 0:
            pool.remove(0)
            pool.append(0)
        recycled = 0
        # PAS DE PLAN EN DOUBLE : si le pool ne couvre pas la voix au rythme nominal,
        # on ALLONGE les plans au lieu de reboucler. Revoir un meme clip trois fois
        # est le signe le plus visible d'une video generee.
        _t, _n = 0.0, 0
        while _t < total:
            _r = 0.0 if _t < HOOK_WINDOW else min(1.0, (_t - HOOK_WINDOW)
                                                  / max(0.1, DECAY_UNTIL - HOOK_WINDOW))
            _t += ((HOOK_CUT_MIN + HOOK_CUT_MAX) / 2) * (1 - _r) + ((CUT_MIN + CUT_MAX) / 2) * _r
            _n += 1
        stretch = min(2.5, max(1.0, _n / max(1, n_pool)))
        if stretch > 1.0:
            print(f"[montage] {n_pool} clips pour ~{_n} plans -> plans allonges x{stretch:.2f}")
        while elapsed < total + 0.6:
            # le plan de revelation part des que la voix l'atteint, et une seule fois
            on_reveal = (reveal_idx is not None and not reveal_done
                         and elapsed >= reveal_at - 0.35)
            if on_reveal:
                idx = reveal_idx
            else:
                if not pool:                   # pool epuise -> on repart du debut
                    pool = list(range(n_pool))
                    pool.reverse()
                    recycled += 1
                idx = pool.pop()
            c, src_dur, img = clips[idx], durations[idx], is_img[idx]
            in_hook = elapsed < HOOK_WINDOW        # zone d'accroche : traitement a part
            # ramp : 0 pendant le hook, 1 une fois le regime de croisiere atteint.
            # Tout ce qui distingue le hook du corps s'interpole sur cette valeur,
            # pour qu'aucun parametre ne bascule d'un seul coup (cf. DECAY_UNTIL).
            ramp = 0.0 if in_hook else min(1.0, (elapsed - HOOK_WINDOW)
                                           / max(0.1, DECAY_UNTIL - HOOK_WINDOW))
            lo = (HOOK_CUT_MIN + (CUT_MIN - HOOK_CUT_MIN) * ramp) * stretch
            hi = (HOOK_CUT_MAX + (CUT_MAX - HOOK_CUT_MAX) * ramp) * stretch
            cut = random.uniform(lo, hi)
            if on_reveal:
                # la revelation reste a l'ecran tant que la voix la prononce
                cut = max(cut, min(reveal_dur, src_dur - 0.1) if not img else reveal_dur)
            elif reveal_idx is not None and elapsed < reveal_at < elapsed + cut - 0.15:
                # coupe calee sur la revelation — mais JAMAIS un plan si court qu'il
                # serait rejete plus bas : la boucle tournerait alors sans avancer.
                trim = reveal_at - elapsed
                if trim >= 0.6:
                    cut = trim
            else:
                # coupe calee sur une fin de phrase / de mot : on ne coupe plus au
                # milieu d'un mot-cle. (Le hook garde son rythme haché volontaire.)
                if ramp > 0.45 and (cuts_strong or cuts_soft):
                    snapped = _snap(elapsed + cut, cuts_strong, cuts_soft) - elapsed
                    if CUT_MIN * 0.6 <= snapped <= CUT_MAX * 1.6:
                        cut = snapped
            if not img and not on_reveal:
                cut = min(cut, max(0.6, src_dur - 0.15))
            lap = i // len(clips)                  # tour de boucle -> decale la portion utilisee
            # Les premieres secondes d'un clip de banque sont souvent les plus plates
            # (cadre qui s'installe, fondu d'entree) : on part vers le quart du clip.
            start = min(max(0.0, src_dur - cut - 0.1), src_dur * 0.25 + lap * cut * 1.7)
            if on_reveal:
                start = 0.0                        # le sujet est cadre des les 1res images
            p = WORK / f"seg_{i}.mp4"

            px, py = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
            # Les images IA font 576x1024 en natif : on limite fortement le zoom sinon
            # l'agrandissement cumule (upscale x zoom) pixellise l'image.
            pf = AI_PUNCH_FROM if img else HOOK_PUNCH_FROM
            zmax = 1.06 if img else 1.10
            if i == 0 and HOOK_PUNCH:
                # SNAP ZOOM d'ouverture : zoom brutal qui se resorbe en ~0.35s
                # (les zooms rapides surpassent les plans statiques d'un facteur 2.5)
                z = f"max({pf}-{(pf - 1.0) / 10.5:.4f}*on,1.02)"
                if HOOK_SHAKE:       # secousse amortie, calee sur le boom d'ouverture
                    amp = 10 if img else 18
                    px += f"+{amp}*sin(on/1.6)*exp(-on/9)"
                    py += f"+{int(amp * 0.78)}*cos(on/1.3)*exp(-on/9)"
            else:
                # Vitesse de zoom interpolee : punch du hook -> Ken Burns lent du
                # corps. Sans interpolation, le mouvement chutait d'un facteur 7
                # au meme instant que les coupes et que l'etalonnage.
                sp_hook = 0.002 if img else 0.003
                sp = sp_hook + (0.0007 - sp_hook) * ramp
                zm = zmax + (1.08 - zmax) * ramp
                z = (f"max({zm:.3f}-{sp:.5f}*on,1.02)" if i % 2 == 0
                     else f"min(1.02+{sp:.5f}*on,{zm:.3f})")

            # palette commune a tous les plans (continuite). L'etalonnage punchy du
            # hook ne s'arrete plus net : il tient toute la premiere moitie de la
            # transition, sinon la couleur change a vue d'oeil en pleine phrase.
            grade = (f",{HOOK_GRADE},{PALETTE}" if ramp < 0.55 else f",{PALETTE}")
            # flash blanc a chaque coupe : il s'affine puis disparait avec la rampe
            flash = (f",fade=t=in:st=0:d={0.05 * (1 - ramp) + 0.01:.3f}:color=white"
                     if (HOOK_FLASH and i > 0 and ramp < 0.75) else "")

            vfx = ""
            if VFX_DUTCH and ramp < 0.8 and i % 2 == 1:
                # DUTCH ANGLE : plan incline (instabilite). L'ordre est critique :
                # on AGRANDIT d'abord, on tourne ensuite, puis on recadre au centre.
                # (tourner avant d'agrandir laisse les coins vides dans le cadre)
                ang = VFX_DUTCH_DEG * (1 - ramp) * (1 if (i // 2) % 2 == 0 else -1)
                # facteur exact pour qu'aucun coin vide n'entre dans le cadre :
                #   k = cos(a) + sin(a) * (H/W)   (+3% de securite)
                _r = math.radians(abs(ang))
                k = (math.cos(_r) + math.sin(_r) * (H / W)) * 1.03
                vfx += (f",scale={int(W * k)}:{int(H * k)},"
                        f"rotate={ang}*PI/180,crop={W}:{H}")
            if VFX_VIGNETTE and ramp < 1.0:
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
            # setparams : certains clips Pixabay sont tagues dans un espace colorimetrique
            # exotique (ycgco) que `scale` refuse de convertir -> "Function not
            # implemented" et plantage du montage. On normalise avant toute chose.
            vf = (f"setparams=colorspace=bt709,"
                  f"scale={sw}:{sh}:force_original_aspect_ratio=increase{flags},"
                  f"crop={sw}:{sh}{sharpen},"
                  f"zoompan=z='{z}':d=1:x='{px}':y='{py}':"
                  f"s={W}x{H}:fps=30{grade}{vfx}{flash},setsar=1")
            # image fixe -> -loop 1 (animee par le zoompan) ; video -> on coupe a `start`
            src_args = (["-loop", "1", "-i", str(c)] if img
                        else ["-ss", f"{start:.2f}", "-i", str(c)])
            # un clip illisible ne doit JAMAIS faire echouer toute la video :
            # on le saute et on continue avec les suivants.
            try:
                subprocess.run(
                    ["ffmpeg", "-y", *src_args, "-t", f"{cut:.2f}",
                     "-vf", vf, "-an", "-r", "30",
                     "-c:v", "libx264", "-crf", "16", "-preset", "medium",
                     "-pix_fmt", "yuv420p", str(p)],
                    check=True, capture_output=True)
            except subprocess.CalledProcessError:
                print(f"[montage] clip illisible ignore : {c.name}")
                stalls += 1
                i += 1
                if stalls >= 4:
                    break
                continue
            d = _duration(p) if p.exists() else 0.0
            if d > 0.4:
                # Un whoosh suit une vraie coupe de plan, pas un timestamp theorique.
                # Le cooldown evite de transformer le hook en bruit blanc permanent.
                if parts and elapsed >= 0.25 and (not transition_times
                                                   or elapsed - transition_times[-1] >= SFX_TRANSITION_GAP):
                    transition_times.append(elapsed)
                parts.append(p)
                elapsed += d
                stalls = 0
                if on_reveal:
                    reveal_done = True
            else:
                # segment inutilisable : on ne doit pas boucler indefiniment dessus
                stalls += 1
                if stalls >= 4:
                    print("[montage] plans trop courts -> arret de la construction")
                    break
            i += 1
            if i > 120:                            # garde-fou
                break
        # FILET DE SECURITE : le fond DOIT couvrir toute la voix. S'il manque des
        # secondes (plans rejetes, clips trop courts), on reboucle sur les plans deja
        # rendus. Repeter une image vaut infiniment mieux qu'un ecran fige et muet.
        if parts and elapsed < total + 0.3:
            missing = total + 0.3 - elapsed
            j = 0
            while elapsed < total + 0.3:
                p = parts[j % len(parts)]
                parts.append(p)
                elapsed += _duration(p)
                j += 1
            print(f"[montage] fond trop court de {missing:.1f}s -> plans reboucles")

        uniq = "tous differents" if not recycled else f"{recycled} repetition(s) du pool"
        print(f"[montage] {len(parts)} plans (~{total / max(1, len(parts)):.1f}s/plan, {uniq})")
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
    # fondu de sortie TRES court : un long fondu au noir casse le raccord de boucle
    # (la video doit repartir sur le hook sans temps mort visuel)
    vchain = (f"[0:v]subtitles='{ass_esc}':fontsdir=fonts,"
              f"fade=t=out:st={total - 0.25:.2f}:d=0.25[v]")

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
            # asetrate baisse la tonalite; atempo compense exactement la duree.
            f"asetrate=44100*{MUSIC_PITCH_FACTOR:.5f},aresample=44100,"
            f"atempo={1 / MUSIC_PITCH_FACTOR:.5f},atrim=0:{total:.2f},volume={MUSIC_VOL},"
            # CUT TOTAL de la musique a 1.5s pendant 0.2s : vide auditif percutant
            f"volume='if(between(t,{SFX_CUT_AT},{SFX_CUT_AT + SFX_CUT_DUR}),0,1)':eval=frame,"
            # fondu d'entree quasi nul : le son doit ATTAQUER des la milliseconde 0
            # (0.12s suffit a eviter le clic numerique, sans adoucir l'ouverture)
            f"afade=t=in:st=0:d=0.12,afade=t=out:st={total - 1.6:.2f}:d=1.6[mus0];"
            # ducking : la musique baisse quand la voix parle
            f"[mus0][vocsc]sidechaincompress=threshold=0.03:ratio=8:attack=5:release=280[mus];"
            f"[voc][mus]amix=inputs=2:duration=first:normalize=0[mixed]"
        )
        print(f"[musique] {music.name}")
    else:
        achain = voc + "[mixed]"
        print("[musique] aucune (depose un .mp3 dans music/)")

    if SFX_HOOK:
        # Les whooshes sont cales sur les coupes effectivement rendues. Si aucun plan
        # n'a pu etre monte, le marqueur historique conserve un minimum de dynamique.
        whoosh_times = transition_times[:SFX_TRANSITION_MAX] or [SFX_WHOOSH_AT]
        whoosh_defs, whoosh_labels = [], []
        for i, when in enumerate(whoosh_times):
            delay = int(when * 1000)
            label = f"whoosh{i}"
            whoosh_defs.append(
                "anoisesrc=d=0.42:c=pink:a=0.42:s=44100,"
                "aformat=channel_layouts=stereo,highpass=f=700,lowpass=f=7000,"
                "afade=t=in:d=0.18,afade=t=out:st=0.18:d=0.24,"
                f"adelay={delay}|{delay},apad,atrim=0:{total:.2f}[{label}];")
            whoosh_labels.append(f"[{label}]")
        ri_ms = int(SFX_RISER_AT * 1000)
        achain += (
            ";aevalsrc='0.8*exp(-4.5*t)*sin(2*PI*(150*t-95*t*t))':d=0.9:s=44100:c=stereo,"
            f"apad,atrim=0:{total:.2f}[boom];"
            # CLAQUE MEDIUM a 0s : le sub-boom seul est inaudible sur un haut-parleur
            # de telephone (qui coupe sous ~500Hz). Ce transitoire large bande porte
            # l'impact la ou le mobile reproduit vraiment.
            "anoisesrc=d=0.18:c=white:a=0.9:s=44100,aformat=channel_layouts=stereo,"
            "highpass=f=450,lowpass=f=5200,afade=t=out:st=0:d=0.18,"
            f"apad,atrim=0:{total:.2f}[crack];"
            + "".join(whoosh_defs)
            # riser : frequence qui monte = compte a rebours / urgence
            + "aevalsrc='0.22*(t/1.2)*sin(2*PI*(260+520*t*t)*t)':d=1.2:s=44100:c=stereo,"
            f"adelay={ri_ms}|{ri_ms},apad,atrim=0:{total:.2f}[riser];"
            + "[mixed][boom][crack]" + "".join(whoosh_labels) + "[riser]"
            + f"amix=inputs={4 + len(whoosh_labels)}:duration=first:normalize=0[aout]"
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
def select_visuals(script: dict, topic: str) -> dict:
    """Choisit TOUS les visuels d'une video : images IA du hook, frame 0, corps.

    Extrait de main() pour qu'un banc d'essai puisse mesurer la selection sans
    produire de video. Les tests qui re-implementaient ce chemin divergeaient de
    la vraie chaine et donnaient de faux succes.
    """
    extra_ban = HISTORICAL_TOKENS if script.get("historical") else frozenset()
    if extra_ban:
        print("[visuels] sujet historique -> presence humaine moderne exclue")

    negative = tuple(k for k in script.get("negative_keywords", []) if k)
    if negative:
        print(f"[visuels] exclusions du sujet : {', '.join(negative)}")
    # Les nouveaux champs sont des contrats visuels. Les champs `*_stock` / `scenes`
    # restent des replis pour les anciennes videos et les reponses Gemini partielles.
    hook_plans = _visual_plans(script, "hook_visual_plan", "hook_stock")
    body_plans = _visual_plans(script, "visual_plan", "scenes",
                               fallback=script.get("keywords", [topic]))
    reveal_plan = _normalise_visual_plan(
        script.get("reveal_visual_plan") or script.get("reveal_stock") or topic)
    # espece du sujet, deduite des requetes anglaises (le topic est en francais)
    _en = " ".join(_plan_queries(hook_plans + body_plans + [reveal_plan])).lower()
    subject_species = frozenset(ANIMAL_SPECIES & set(re.findall(r"[a-z]+", _en)))
    if subject_species:
        print(f"[visuels] sujet animalier : {'/'.join(sorted(subject_species))}")
    # --- exclusions et ancrages PROPRES A CE SUJET (produits par Gemini)
    _anchor_words = frozenset(w for a in script.get("anchor_keywords", []) if a
                              for w in re.findall(r"[a-z]+", str(a).lower()) if len(w) > 2)
    neg_words = _neg_words(negative, _anchor_words)   # pour filtrer les REQUETES
    extra_ban = frozenset(extra_ban) | neg_words   # ...et les clips, par mot entier
    anchors = tuple(a for a in script.get("anchor_keywords", []) if a)
    if anchors:
        print(f"[visuels] ancrages du sujet : {', '.join(anchors)}")
    # un mot normalement banni peut etre LE sujet ("les aquariums geants") -> on le leve
    allow = tuple(a.lower() for a in script.get("allow_keywords", []) if a)
    if allow:
        print(f"[visuels] bans globaux leves (sujet) : {', '.join(allow)}")
    strict = bool(script.get("historical") or anchors)

    hook_kw = [k for k in script.get("hook_keywords", []) if k]
    hook_clips = fetch_ai_images(hook_kw, n=AI_IMAGE_COUNT) if hook_kw else []
    first: list[Path] = []
    if len(hook_clips) < 2:                      # repli si le generateur d'images est HS
        # Les requetes sont courtes, mais leurs ancres `must_include` restent strictes.
        stock_plans = hook_plans or [_normalise_visual_plan(k) for k in hook_kw]
        if script.get("historical"):
            stock_plans = _filter_visual_plans(stock_plans, _anchor_queries)
        stock_plans = _filter_visual_plans(
            stock_plans, lambda qs: _drop_banned_queries(qs, neg_words))
        # FRAME 0 : on exige le NOM DU SUJET dans le clip d'ouverture. Une ambiance
        # ("mysterious jungle") ne dit pas au spectateur de quoi parle la video.
        first_plan = stock_plans[0] if stock_plans else None
        subject = (" ".join(first_plan["must_include"]) if first_plan else None)
        first = fetch_clips([first_plan], n=1, label="frame 0", require=subject,
                            banned_tokens=extra_ban, negative=negative,
                            allow=allow) if first_plan else []
        if not first and subject:                # le sujet seul, sans le decor
            first = fetch_clips([subject], n=1, label="frame 0 (repli)", require=subject,
                                banned_tokens=extra_ban, negative=negative, allow=allow)
        hook_clips += first
        hook_clips += fetch_clips(stock_plans, n=3 - len(first), label="hook stock",
                                  banned_tokens=extra_ban, strict=strict,
                                  negative=negative, allow=allow,
                                  species=subject_species)
    # Le corps suit le plan structure dans l'ordre du recit, pas des mots isoles.
    if script.get("historical"):
        body_plans = _filter_visual_plans(body_plans, _anchor_queries)
    body_plans = _filter_visual_plans(
        body_plans, lambda qs: _drop_banned_queries(qs, neg_words))
    body_plans = _filter_visual_plans(
        body_plans, lambda qs: _drop_other_species(qs, subject_species))
    if anchors:
        # Gemini ecrit des scenes volontairement indirectes ("rayons de supermarche
        # vides" pour les abeilles) : c'est le propos meme de la video et il faut les
        # garder. Mais illustrees au premier degre elles derivent — un short sur les
        # requins recevait une foret, un pont et une medecin. On INTERCALE donc des
        # plans du sujet toutes les ~3 scenes, pour qu'il reste present a l'ecran
        # sans effacer le recit.
        variantes = [" ".join(anchors[:2]), anchors[0],
                     " ".join(anchors[1:3]) or anchors[0]]
        fusion = []
        for pos, plan in enumerate(body_plans):
            fusion.append(plan)
            if pos % 3 == 2:
                q = variantes[(pos // 3) % len(variantes)]
                fusion.append(_normalise_visual_plan(
                    {"query": q, "must_include": [anchors[0]], "shot": "detail",
                     "look": ["cinematic", "dark", "high_detail"]}))
        body_plans = fusion + [_normalise_visual_plan(
            {"query": " ".join(anchors[:2]), "must_include": list(anchors[:2]),
             "shot": "detail", "look": ["cinematic", "dark", "high_detail"]})]
    body_clips = fetch_clips(body_plans, n=12, label="corps", banned_tokens=extra_ban,
                             strict=strict, negative=negative, allow=allow,
                             species=subject_species)
    clips = hook_clips + body_clips or hook_clips or body_clips
    return {"clips": clips, "hook_clips": hook_clips, "body_clips": body_clips,
            "first": first, "pinned_first": bool(first),
            "hook_plans": hook_plans, "body_plans": body_plans,
            "reveal_plan": reveal_plan, "anchors": anchors, "allow": allow,
            "strict": strict, "negative": negative, "extra_ban": extra_ban,
            "subject_species": subject_species}


def main():
    topic = sys.argv[1] if len(sys.argv) > 1 else random.choice(DEFAULT_TOPICS)
    print(f"=== Sujet : {topic} ===")
    _seen_prefixes.clear()        # dedoublonnage de scenes propre a cette video
    _seen_ids.clear()
    script = make_script(topic)
    # Nouveau contrat : hook -> trois faits concrets -> chute/raccord.
    # Les anciennes archives restent generables si Gemini renvoie encore le schema precedent.
    facts = script.get("facts")
    if isinstance(facts, list) and len([f for f in facts if str(f).strip()]) == 3:
        fact_tones = ("tension", "body", "revelation")
        segments = ([{"text": script.get("hook", ""), "tone": "hook"}]
                    + [{"text": str(f).strip(), "tone": tone}
                       for f, tone in zip(facts, fact_tones)]
                    + [{"text": script.get("loop", ""), "tone": "loop"}])
    else:
        segments = [{"text": script.get(k, ""), "tone": k}
                    for k in ("hook", "tension", "body", "revelation", "loop")]
    narration = " ".join(s["text"] for s in segments if s["text"])

    audio, timeline = make_voice(segments)
    voice_dur = sum(it["dur"] for it in timeline)
    total = LEAD + voice_dur + TAIL          # ~0.3s + voix + outro
    try:
        words = align_words(audio)           # sync mot par mot (faster-whisper)
        words = _remap_words(words, narration)   # ...mais on affiche le texte du script
        print(f"[align] {len(words)} mots alignes")
    except Exception as e:
        print(f"[align] echec ({e}) -> timing estime")
        words = None
    ass = WORK / "subs.ass"
    build_subtitles(timeline, words, total, ass, emphasis=script.get("emphasis"), offset=LEAD,
                    overlay=script.get("overlay"))
    # Le hook doit etre ILLUSTRE par ce qu'il raconte : on cherche d'abord des clips
    # colles au texte du hook, ils occuperont les tout premiers plans.
    # sujet historique -> toute personne/objet moderne a l'image est un anachronisme
    V = select_visuals(script, topic)
    clips = V["clips"]
    hook_plans, body_plans = V["hook_plans"], V["body_plans"]
    reveal_plan, anchors, allow = V["reveal_plan"], V["anchors"], V["allow"]
    strict, negative = V["strict"], V["negative"]
    extra_ban, subject_species = V["extra_ban"], V["subject_species"]
    first, pinned_first = V["first"], V["pinned_first"]

    # --- plan de REVELATION : quand la voix nomme le sujet, on doit le VOIR.
    reveal_at, reveal_dur, t = None, 0.0, LEAD
    for it in timeline:
        if it["kind"] == "speech" and it.get("tone") == "revelation":
            reveal_at, reveal_dur = t, it["dur"]
            break
        t += it["dur"]
    reveal_clip = None
    if reveal_at is not None:
        rk = reveal_plan["query"]
        noun = " ".join(reveal_plan["must_include"]) or _key_noun(rk)
        # 1er essai : on EXIGE que le sujet soit dans le clip (sinon Pexels renvoie
        # un recif quelconque pour 'great white shark'). Sinon on relache la contrainte.
        got = fetch_clips([reveal_plan], n=1, label="revelation", require=noun,
                          banned_tokens=extra_ban, negative=negative, dedup=False,
                          species=subject_species)
        if not got and noun:
            got = fetch_clips([noun], n=1, label="revelation (repli)", require=noun,
                              banned_tokens=extra_ban, negative=negative, dedup=False,
                              species=subject_species)
        if not got:
            got = fetch_clips([rk], n=1, label="revelation (large)",
                              banned_tokens=extra_ban, dedup=False)
        if got:
            reveal_clip = got[0]
            print(f"[montage] revelation epinglee a {reveal_at:.1f}s ({reveal_dur:.1f}s)")

    # --- points de coupe : on change de plan sur une RESPIRATION de la voix,
    # jamais au milieu d'un mot. Fin de phrase prioritaire, sinon fin de mot.
    cuts_strong, cuts_soft = [], []
    if words:
        for w in words:
            t_end = LEAD + w["end"]
            (cuts_strong if re.search(r"[.!?]$", w["word"].strip())
             else cuts_soft).append(t_end)
    else:                                    # pas d'alignement -> bornes de segments
        t = LEAD
        for it in timeline:
            t += it["dur"]
            cuts_strong.append(t)

    out = OUT_DIR / "short.mp4"
    assemble(audio, ass, clips, out, total,
             reveal_clip=reveal_clip, reveal_at=reveal_at, reveal_dur=reveal_dur,
             cuts_strong=sorted(cuts_strong), cuts_soft=sorted(cuts_soft),
             pin_first=pinned_first)

    # --- mascotte incrustee : le perso actif du studio parle sur la voix off.
    # La bouche suit la VOIX seule (pas la musique/SFX, qui la feraient bavarder).
    try:
        import character
        if character.get_active():
            burned = character.burn_on_video(out, OUT_DIR / "short_perso.mp4",
                                             audio_src=audio, offset=LEAD)
            if burned and burned.exists():
                burned.replace(out)
    except Exception as e:
        print(f"[perso] incrustation ignoree ({e})")

    tt_title = script["title"].replace("#Shorts", "").strip()
    meta = {"title": script["title"] + " #Shorts",
            # la description EXPLIQUE le sujet ; elle ne repete pas la narration
            # (le spectateur vient de l'entendre, et YouTube n'y trouve aucun
            #  mot-cle nouveau pour le referencement)
            "description": (script.get("description") or narration).strip()
                           + "\n\n#Shorts #shorts",
            "tags": (script.get("keywords", []) + ["shorts"]),
            "topic": topic,
            "tiktok": f"{tt_title} 👀🤯\n\n#pourtoi #fyp #lesaviezvous #culturegenerale "
                      "#incroyable #apprendresurtiktok #wtf",
            "virality": script.get("virality"),
            "virality_reason": script.get("virality_reason", ""),
            "hook_variants": script.get("hook_variants", []),
            "facts": script.get("facts", []),
            "overlay": script.get("overlay", ""),
            "sfx": script.get("sfx", ""),
            # requetes reellement utilisees : indispensable pour diagnostiquer
            # un plan hors-sujet apres coup (elles n'etaient nulle part archivees)
            "queries": {"hook": _plan_queries(hook_plans),
                        "scenes": _plan_queries(body_plans),
                        "reveal": reveal_plan["query"],
                        "negative": script.get("negative_keywords", []),
                        "anchors": script.get("anchor_keywords", [])},
            "visual_plan": {"hook": hook_plans, "scenes": body_plans,
                            "reveal": reveal_plan},
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
