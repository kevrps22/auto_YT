# YouTube Shorts Bot (gratuit)

Genere des Shorts "le saviez-vous" et les publie automatiquement, 100% avec des outils gratuits.

`sujet -> script (Gemini) -> voix FR (edge-tts) -> visuels (Pexels) -> montage (FFmpeg) -> upload (YouTube API)`

## 1. Tester en local (sans rien payer)

```bash
# Pre-requis : Python 3.11+ et FFmpeg installes (ffmpeg dans le PATH)
pip install -r requirements.txt

# Test montage seul (sans cles API -> script d'exemple + fond noir)
python generate.py "les trous noirs"
# -> output/short.mp4
```

Ca doit deja produire une video avec voix FR + sous-titres. Ajoute ensuite les cles :

- **GEMINI_API_KEY** : gratuit sur https://aistudio.google.com/apikey
- **PEXELS_API_KEY** : gratuit sur https://www.pexels.com/api/

```bash
# Windows PowerShell
$env:GEMINI_API_KEY="..."; $env:PEXELS_API_KEY="..."; python generate.py "les oceans"
```

## 2. Brancher l'upload YouTube (une fois)

1. https://console.cloud.google.com -> nouveau projet -> active **YouTube Data API v3**
2. Ecran de consentement OAuth -> type **Desktop** -> telecharge `client_secret.json` ici
3. `python upload.py --auth`  (autorise dans le navigateur -> cree `token.json`)
4. `python upload.py`  (publie output/short.mp4)

## 3. Automatiser avec GitHub Actions (gratuit, cron)

1. Push ce dossier sur un repo GitHub **prive**.
2. Settings -> Secrets and variables -> Actions -> ajoute :
   - `GEMINI_API_KEY`, `PEXELS_API_KEY`
   - `YT_CLIENT_SECRET` (contenu de client_secret.json)
   - `YT_TOKEN` (contenu de token.json)
3. Le workflow `.github/workflows/daily.yml` lance 1 video/jour a 9h UTC.
   Onglet **Actions** -> "Run workflow" pour tester tout de suite.

## Habillage (intro / outro / musique)

Tout se regle en haut de `generate.py` :

- **Intro animee** : le titre apparait au centre avec un zoom+fondu (`LEAD` = duree avant la voix).
- **Outro** : carte de fin `OUTRO_TEXT` + fondu, pour ne pas couper net (`TAIL` = duree de fin).
- **Sous-titres karaoke** : police Anton (`fonts/`), mot surligne en jaune (`&H0000E5FF`).
- **Musique de fond** : depose un/des `.mp3` dans `music/` -> choix aleatoire, volume `MUSIC_VOL`,
  avec *ducking* (la musique baisse quand la voix parle) + fondus. Si `music/` est vide, pas de musique.
  Sources gratuites : YouTube Audio Library, Pixabay Music, Freesound (CC0).
- **Fondus** video ouverture/fermeture automatiques.

`fonts/` et `music/` sont commites -> GitHub Actions les utilise aussi.

## Notes

- `.gitignore` protege tes cles : ne commit jamais `client_secret.json` ni `token.json`.
- Politique YouTube : vise la qualite, pas 50 videos/jour clonees (demonetisation).
- Voix : change `VOICE` (`fr-FR-VivienneMultilingualNeural` = feminine naturelle),
  `RATE`/`PITCH` pour le rythme.
