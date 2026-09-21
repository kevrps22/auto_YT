# Attends Quoi ?! — fabrique de Shorts

Découpe des vidéos longues sous licence libre en Shorts, et les remonte en vidéos
explicatives. Chaîne : [@Attends_Quoi](https://www.youtube.com/@Attends_Quoi).

`lien YouTube → transcription → choix des moments (Gemini) → clips verticaux → montage explicatif`

Tout tourne avec des outils gratuits : Gemini, Pexels, FFmpeg, faster-whisper,
GitHub Actions. Aucun abonnement, aucune carte bancaire.

## Organisation

```
auto_YT/
├─ clipper/          le cœur
│   ├─ clipper.py       découpe : calage image + phrase, sous-titres, rendu
│   ├─ habiller.py      remonte un clip en vidéo explicative (la voix est gardée)
│   ├─ pipeline.py      enchaîne téléchargement → transcription → choix → rendu
│   ├─ transcribe.py    transcription mot à mot (faster-whisper)
│   ├─ cloud.py         point d'entrée du rendu sur GitHub
│   ├─ github_io.py     déposer une source, lancer, suivre, récupérer
│   └─ web.py + web/    page locale (port 8765), utilisable depuis le téléphone
├─ youtube/          publication et mesure
│   ├─ upload.py        envoi OAuth (YouTube Data API v3)
│   ├─ daily_upload.py  publie une vidéo par jour
│   ├─ analytics.py     rétention réelle seconde par seconde, APV
│   └─ dashboard.py     tableau de bord local des statistiques
├─ assets/           polices (Montserrat, OFL) et musique de fond
├─ media/            sources, clips, rendus — hors dépôt, des gigaoctets
├─ docs/             ARCHITECTURE.md (décisions, mesures) · ETAT.md (reprise)
└─ app/              « YT Studio », application Flutter Windows
```

## Installation

```bash
pip install -r requirements.txt
```

FFmpeg doit être installé et dans le PATH : `winget install Gyan.FFmpeg`.

Trois fichiers ne sont jamais commités et sont à recréer sur une nouvelle machine
— voir [docs/ETAT.md](docs/ETAT.md) : `.env`, `client_secret.json`, `token.json`.

## Utilisation

La page locale fait tout, à partir d'un lien YouTube :

```bash
python clipper/web.py
```

En ligne de commande, sur une source déjà téléchargée dans `media/src` :

```bash
python clipper/clipper.py media/src/ID.mp4 --n 8 --titre "titre de la source"
```

Rendu sur GitHub (le téléchargement reste ici, YouTube bloque les serveurs) :

```bash
python clipper/github_io.py envoyer "https://www.youtube.com/watch?v=ID" --n 8
```

Remonter un clip en vidéo explicative :

```bash
python clipper/habiller.py ID --clip 7
```

Lire les vraies courbes de rétention :

```bash
python youtube/analytics.py --hook
```

## Trois règles apprises à la dure

- **Une vidéo par jour, publiée depuis la maison.** Les envois faits depuis les
  serveurs GitHub ont fait brider la chaîne, et le lot programmé d'août 2026 a
  divisé les vues par quatre — le contenu n'y était pour rien.
- **Sources en Creative Commons uniquement**, crédit dans la description. Les
  clips issus d'une autre licence sont marqués non publiables par `publiable()`.
- **Ne jamais juger une vidéo avant 48 h.** Les vues arrivent par vagues.
