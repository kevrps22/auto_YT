# Clipper — des Shorts à partir d'entretiens libres

Découpe une vidéo longue sous licence Creative Commons en extraits verticaux, puis
remonte chaque extrait en vidéo explicative : **la voix de l'invité est conservée,
l'image est entièrement refabriquée**.

```
lien YouTube → transcription → choix des moments → extraits verticaux → montage explicatif → publication
```

Tout repose sur des outils gratuits : Gemini pour le choix éditorial, faster-whisper
pour la transcription, Pexels pour les illustrations, FFmpeg pour le rendu, GitHub
Actions pour le calcul. Aucun abonnement.

## Comment c'est réparti

Trois machines, chacune pour ce qu'elle seule peut faire :

| | Rôle | Pourquoi |
|---|---|---|
| **Une machine à la maison** | télécharger la source, publier | YouTube bloque les téléchargements depuis les serveurs, et les publications faites depuis un centre de données se font brider |
| **GitHub Actions** | transcrire, choisir, rendre les extraits | deux heures d'audio se transcrivent en 40 min là-bas, contre une nuit sur un nano-ordinateur |
| **Un nano-ordinateur** | orchestrer, habiller, publier chaque soir | il reste allumé, un PC non |

Le tout tourne sans intervention : deux tâches `systemd`, une le matin pour
reconstituer le stock, une le soir pour publier.

## Organisation

```
├─ clipper/
│   ├─ clipper.py       découpe : calage image + phrase, sous-titres, rendu
│   ├─ habiller.py      remonte un extrait en vidéo explicative
│   ├─ pipeline.py      téléchargement → transcription → choix → rendu
│   ├─ transcribe.py    transcription mot à mot (faster-whisper)
│   ├─ cloud.py         point d'entrée du rendu sur GitHub Actions
│   ├─ github_io.py     déposer une source, lancer, suivre, récupérer
│   └─ web.py + web/    page locale (port 8765), utilisable depuis un téléphone
├─ youtube/
│   ├─ soir.py          le pilote : approvisionne le stock, publie un clip par soir
│   ├─ publier.py       publie un extrait de l'index, avec ses garde-fous
│   ├─ upload.py        envoi OAuth (YouTube Data API v3)
│   └─ analytics.py     rétention réelle seconde par seconde
├─ assets/              polices (Montserrat, licence OFL) et musique de fond
├─ media/               sources, extraits, rendus — hors dépôt, des gigaoctets
├─ docs/                ARCHITECTURE.md · ORANGE_PI.md · ETAT.md
└─ .github/workflows/   le rendu sur GitHub Actions
```

## Installation

```bash
pip install -r requirements.txt
```

FFmpeg doit être installé et accessible dans le PATH.

Trois fichiers ne sont jamais versionnés et sont à créer : `.env` (clés Gemini,
Pexels et YouTube), `client_secret.json` et `token.json` (OAuth YouTube). Le détail
est dans [docs/ETAT.md](docs/ETAT.md), l'installation du nano-ordinateur dans
[docs/ORANGE_PI.md](docs/ORANGE_PI.md).

## Utilisation

La page locale fait tout à partir d'un lien :

```bash
python clipper/web.py
```

En ligne de commande :

```bash
python clipper/github_io.py envoyer "https://www.youtube.com/watch?v=ID"   # rendu déporté
python clipper/habiller.py ID --clip 2                                     # montage explicatif
python youtube/soir.py --etat                                              # stock et journal
python youtube/analytics.py --hook                                         # courbes de rétention
```

Chaque commande qui publie accepte `--simuler` : elle montre ce qu'elle ferait sans
rien envoyer.

## Licences

Le projet ne traite que des sources en **Creative Commons Attribution (CC BY)**.
Un extrait issu d'une autre licence est marqué non publiable et la publication est
refusée. Chaque vidéo produite crédite l'auteur et la source dans sa description —
c'est une obligation de la licence, pas une politesse.

Les polices Montserrat sont sous licence SIL Open Font.
