# auto_YT — Architecture technique

Pipeline de génération automatique de YouTube Shorts pour la chaîne
**« Attends Quoi ?! »** (`@Attends_Quoi`). Document de référence du 5 septembre 2026.
Les numéros de ligne renvoient à `generate.py` sauf mention contraire.

---

## 0. En bref

| Élément | Valeur |
|---|---|
| Langage | Python 3.11 — venv `uv` dans `.venv/` (le Python système est corrompu, exe de 0 octet) |
| Rendu | **FFmpeg 8.1.2 en ligne de commande** — pas de MoviePy |
| Format | 1080×1920, 30 fps, H.264 CRF 18, AAC 192k, 17 à 26 s |
| Script | Google Gemini, bascule sur 4 modèles (quota gratuit 20 req/jour/modèle) |
| Voix | edge-tts `fr-CA-AntoineNeural` (Google Chirp 3 HD et Kokoro en option) |
| Sous-titres | faster-whisper `small` int8 CPU pour le TIMING, texte réimposé depuis le script |
| Visuels | **Pexels + Pixabay** (catalogue hybride) + Pollinations pour le hook |
| Publication | 1/jour, tâche planifiée, **depuis l'IP du domicile** |

**Contrainte matérielle** : AMD RX 6400 en DirectML, **pas de CUDA**. Tout est CPU/FFmpeg.
Aucun modèle de lip-sync ni de diffusion en local.

**État chaîne** : 47 abonnés, 27 210 vues, 28 vidéos publiées, 14 en stock.

---

## 1. Arborescence

```
auto_YT/
├── generate.py        1872 l.  CŒUR : script → voix → sous-titres → visuels → montage
├── character.py        428 l.  studio mascotte (détourage, bouche animée, incrustation)
├── daily_upload.py     191 l.  publie 1 vidéo/jour (conçu pour tourner sur un Raspberry Pi)
├── batch.py            106 l.  génère en série depuis une feuille Google Sheets
├── export_queue.py      83 l.  prépare la file d'attente sur carte SD (INCOMPLET)
├── dashboard.py        407 l.  dashboard web local des stats YouTube
├── upload.py            82 l.  upload OAuth YouTube Data API v3
├── rescore.py          102 l.  renote la viralité des vidéos archivées
├── topics.txt          676 l.  réservoir de sujets (1/ligne, # = commentaire)
├── .env                        SECRETS (gitignore) — voir section 8
├── fonts/                      Anton (police des sous-titres)
├── music/                      .mp3 de fond, choix aléatoire
├── characters/<slug>/          base.png, cutout.png, char.json + active.txt
├── output/
│   ├── short.mp4               rendu courant
│   ├── meta.json               métadonnées du rendu courant
│   ├── lib/<date>_<slug>/      GALERIE : vidéo, meta.json, miniature, poster
│   └── exports/                copies .mp4 à plat, pour upload manuel
└── app/lib/
    ├── main.dart       932 l.  app Flutter Windows « YT Studio » (Générer/Perso/Upload/Stats)
    └── character_tab.dart 605 l.  onglet studio personnage (placement bouche à la souris)
```

---

## 2. Workflow — `main()` (l. 1648)

```
 1. topic          argv[1] ou random dans topics.txt
 2. make_script()  → 1 appel Gemini, renvoie TOUT le JSON (voir section 3)
 3. segments       [hook, tension, body, revelation, loop]  (ordre narratif figé)
 4. make_voice()   → voice.wav + timeline[{kind, text, tone, dur}]
                     total = LEAD(0.0) + durée_voix + TAIL(0.6)
 5. align_words()  → timings mot à mot (faster-whisper)
    _remap_words() → REMPLACE le texte de Whisper par celui du script
 6. build_subtitles() → subs.ass (karaoké + overlay plein écran)
 7. filtres contextuels : historical / negative / anchors / allow / espèces
 8. fetch_ai_images(hook_keywords)     → Pollinations, 3 images
    fetch_clips(hook_stock)            → frame 0 (require = nom du sujet)
    fetch_clips(scenes, n=12)          → corps, ORDRE NARRATIF
    fetch_clips(reveal_stock, n=1)     → plan de révélation
 9. cuts_strong / cuts_soft            → fins de phrase / de mot (calage des coupes)
10. assemble()     → output/short.mp4  (UN SEUL appel FFmpeg pour le rendu final)
11. character.burn_on_video()          → mascotte, si un perso est actif
12. meta.json + archive()              → output/lib/<date>_<slug>/
```

L'upload n'est **pas** appelé par `main()`. Il est assuré séparément par `daily_upload.py`.

---

## 3. Le contrat JSON avec Gemini

`make_script()` (l. 246) fait **un seul appel** et attend ces clés. C'est le cœur
éditorial du projet : la quasi-totalité de la qualité se joue dans ce prompt.

| Clé | Rôle |
|---|---|
| `title` | titre YouTube |
| `description` | **explique le sujet** (dates, chiffres, noms) — surtout PAS la narration |
| `hook`, `tension`, `body`, `revelation`, `loop` | les 5 segments narrés, dans cet ordre |
| `hook_variants` | 3 hooks générés, le meilleur est repris dans `hook` |
| `overlay` | 3-5 mots MAJUSCULES, plein écran de 0 à 2,2 s |
| `sfx` | bruitage suggéré (informatif, non utilisé au montage) |
| `hook_keywords` | 3 prompts détaillés pour Pollinations |
| `hook_stock` | 3 requêtes courtes Pexels/Pixabay, **doivent contenir le nom du sujet** |
| `scenes` | **8 requêtes dans l'ordre du récit** — pilote le b-roll du corps |
| `reveal_stock` | 1 requête littérale montrant le sujet |
| `historical` | booléen → active `HISTORICAL_TOKENS` |
| `negative_keywords` | **exclusions propres à CETTE vidéo** (voisins confondables) |
| `anchor_keywords` | mots qui DOIVENT apparaître dans les plans |
| `allow_keywords` | lève un ban global quand le mot est le sujet |
| `keywords` | 3 mots-clés simples (repli + tags YouTube) |
| `emphasis` | 6-10 mots à mettre en valeur dans les sous-titres |
| `virality`, `virality_reason` | note 0-100, sert au tri de publication |

### Règles éditoriales encodées dans le prompt

- **Information Gap** : le hook ne nomme jamais le sujet, il dit « ce truc », « cette chose ».
- **Le premier mot est une gifle** : paradoxe extrême, peur commune ou comparaison choc.
  Zéro mot d'installation (« alors », « en fait », « saviez-vous »).
- **Ne rien résoudre avant la RÉVÉLATION** — la chute de rétention à 0:04 venait de là.
  Technique d'élimination pour les superlatifs (« non, ce n'est pas le wingsuit… »).
- **Seamless loop** : la dernière phrase se raccorde GRAMMATICALEMENT au hook,
  formant une seule phrase continue (vise APV > 100 %).
- **Anti-métaphore** : « de la taille d'une orange » ne doit pas ramener un fruit ;
  les mots abstraits (barrière, clé, bouclier) ramènent des symboles hors-sujet.
- **Anachronisme = faute grave** sur les sujets historiques.
- Orthographe : français parfaitement accentué et apostrophé, accords vérifiés.

---

## 4. Sélection des visuels — le point le plus travaillé

### 4.1 Sources

| Source | Fonction | État |
|---|---|---|
| **Pexels** | `fetch_clips()` l. 1165 | `orientation=portrait`, 100 % vertical |
| **Pixabay** | `fetch_pixabay_videos()` l. 1122 | fusionné dans `fetch_clips` ; **~90 % paysage**, on ne garde que le vertical |
| **Pollinations** | `fetch_ai_images()` l. 954 | images du hook ; a été payant en août, refonctionne |

Le filtrage s'appuie sur le **descriptif texte** du clip, normalisé par `_vid_text()`
(l. 1105) : slug d'URL chez Pexels, liste de tags chez Pixabay, ramenés à une forme
à tirets et encadrés, pour qu'un motif comme `fish-tank` ou `-zoo-` matche partout.

### 4.2 Les couches de filtrage

1. `_anchor_queries()` (l. 1007) — sujets historiques : écarte les requêtes ambiguës
   sans mot d'ancrage. `ash dust hand` ramenait une cigarette et un cendrier.
2. `_drop_banned_queries()` (l. 1029) — écarte les REQUÊTES contenant un mot interdit
   pour ce sujet. Filtrer les clips ne suffit pas.
3. `_drop_other_species()` (l. 1049) — sujet animalier : écarte les requêtes nommant
   une AUTRE espèce (Gemini proposait « lion roaring » pour un short sur le gorille).
4. `BANNED_VISUALS` (l. 148) — bans GLOBAUX, en sous-chaîne : fond vert, mannequin,
   réunion d'entreprise, atelier d'art, touristes, aquarium/zoo, machines industrielles,
   salle de sport, et **clichés symboliques** (serrure, clé, pancarte, ampoule, puzzle).
5. `HISTORICAL_TOKENS` — bans par **mot entier** si `historical` : personnes/objets
   modernes, feu de forêt, images de synthèse.
   *Le mot entier est impératif : en sous-chaîne, `ancient-roman-ruins` matcherait « man ».*
6. `negative_keywords` — exclusions du sujet, via `_negative_hit()` (l. 1096).
7. `ANIMAL_SPECIES` — rejette tout clip citant une espèce absente de la requête.
8. **Pertinence positive** (`strict`) — phase 0 : le mot distinctif (`_key_noun`, l. 1064)
   doit être présent ; 2 recoupements exigés quand le descriptif est long (Pixabay
   liste ~15 tags, un seul mot commun ne prouve rien).
9. **Anti-doublon** — `_seen_ids` (identité du clip, absolu) + `_slug_prefix()` (l. 1081,
   signature de scène, souple : abandonnée en phase 1 pour ne pas affamer le pool).
10. **Vertical uniquement** — tout fichier plus large que haut est rejeté.

Deux phases par requête : **phase 0** exigeante, **phase 1** relâche l'inédit mais garde
la pertinence en mode strict. Sans ce repli, une banque pauvre laissait sans image.

---

## 5. Montage — `assemble()` (l. 1360)

### 5.1 Durée des plans

| Zone | Constante | Valeur |
|---|---|---|
| Hook (0 → 3,2 s) | `HOOK_CUT_MIN/MAX` | 0,45 – 0,7 s |
| Corps | `CUT_MIN/MAX` | 1,5 – 2,1 s |

Les coupes sont **calées sur la respiration de la voix** par `_snap()` (l. 1344) :
fin de PHRASE en priorité (tolérance 0,55 s), sinon fin de MOT (0,30 s). Le hook garde
son rythme haché volontaire.

**Ancrage de la révélation** : `main()` parcourt la `timeline` pour trouver l'instant du
segment `tone == "revelation"`, force une coupe à cet instant (seulement si le plan
résultant fait ≥ 0,6 s) et y épingle le plan du sujet, `start = 0` du clip.

> Piège historique : une coupe sous le seuil de rejet de 0,4 s faisait tourner la boucle
> sans avancer → garde-fou `i > 120` atteint → fond plus court que la voix, 5 s d'écran
> figé. D'où le seuil, le compteur `stalls` et le filet ci-dessous.

**Filet de sécurité** : si le fond concaténé reste plus court que `total`, les plans déjà
rendus sont rebouclés. Répéter vaut mieux qu'un écran figé.

### 5.2 Ordre des plans

`pool.reverse()` — **jamais de mélange aléatoire**. `clips` est rangé hook puis scènes
dans l'ordre du récit, donc l'image suit ce que dit la voix. Chaque clip n'est tiré
qu'une fois (log « tous differents » ou « N repetition(s) du pool »).
La **frame 0** est épinglée : c'est le plan validé contenant le nom du sujet.

### 5.3 Rendu FFmpeg

**A — segments** : un appel par plan.
`setparams=colorspace=bt709` en tête (certains clips Pixabay sont tagués `ycgco`, que
`scale` refuse de convertir → « Function not implemented »), puis
scale → crop → zoompan (Ken Burns) → PALETTE [→ HOOK_GRADE → vfx → flash] → setsar.
Un clip illisible est sauté, il ne fait plus échouer toute la vidéo.

**B — concaténation** : `concat` demuxer puis **ré-encodage obligatoire** (`-r 30 -vsync cfr`).
`-c copy` casserait les PTS du zoompan et tronquerait l'audio à ~7 s.

**C — rendu final, un seul appel** avec `-filter_complex` :

- *vidéo* : `[0:v] → subtitles(subs.ass) → fade out 0,25 s → [v]`
  (fondu court : un long fondu au noir casse le raccord de boucle)
- *audio* : 5 sources mélangées
  1. voix — **aucun filtre** (les compresseurs la faisaient grésiller)
  2. musique — volume 0.22, **coupure totale à 1,5 s pendant 0,2 s** (vide auditif),
     `afade in 0,12 s` seulement, ducking par `sidechaincompress`
  3. `boom` — sweep 150 Hz descendant à 0 s
  4. `crack` — bruit blanc 450-5200 Hz, 180 ms, à 0 s
     *(le sub-boom seul est inaudible sur haut-parleur de téléphone)*
  5. `whoosh` à 0,5 s + `riser` à 1,7 s

**Aucune transition** : ce sont des coupes franches. Seul fondu = celui de sortie.

### 5.4 VFX du hook (0 → 3,2 s)

Snap zoom 1,55× résorbé en ~0,35 s, secousse caméra amortie, punch par plan, flash blanc
0,05 s à chaque coupe, glitch RGB par paliers, dutch angle ±5° (agrandir AVANT de
tourner, facteur `cos(a)+sin(a)·(H/W)`, sinon bandes noires), vignettage, étalonnage
renforcé.

---

## 6. Sous-titres — `build_subtitles()` (l. 861)

Format **ASS** brûlé par le filtre `subtitles`. 5 styles : `Def`, `Hook`, `HookLow`,
`Over`, `Outro`.

- Karaoké mot à mot (`\kf`) sur les timings Whisper.
- Couleurs : jaune (lu le plus vite), **rouge néon** sur `emphasis`, **cyan** sur les chiffres.
- `_remap_words()` (l. 726) — alignement `difflib` : on garde le CHRONOMÉTRAGE de Whisper
  mais on affiche le TEXTE EXACT du script. Whisper écrivait « broil à chère » pour
  « broyer la chair ».
- `_fit_group()` (l. 772) — découpage par blocs de sens : ne chevauche jamais deux phrases,
  ne finit jamais sur un mot de liaison ou un auxiliaire, plancher 2 mots, plafond 4.
- **Overlay** plein écran (style `Over`, police 168, blanc à contour rouge) de 0 à 2,2 s.
  Pendant ce temps les sous-titres du hook passent en `HookLow` (bas de l'écran).
- `OUTRO_TEXT = ""` : le CTA « abonne-toi » est **désactivé** — il signale la fin et tue
  la boucle. `TAIL` réduit à 0,6 s pour la même raison.

---

## 7. Publication

`daily_upload.py` — publie 1 vidéo/jour. **Conçu pour tourner sur un Raspberry Pi** :
il ne dépend que de `requests` + client Google, ni ffmpeg ni Whisper ni torch.

- choisit la vidéo non publiée à la plus forte `virality` ;
- compare les titres en **normalisé** (YouTube retouche la ponctuation :
  « physique : L'effet » devient « physique L'effet » → doublon évité) ;
- refuse de publier deux fois le même jour ;
- options : `--lib` (file d'attente), `--delete-after`, `--notify` (Telegram), `--low N`.

**Tâche Windows** « AttendsQuoi - Upload quotidien » : 18h00, `StartWhenAvailable`
(rattrape si le PC était éteint) et `WakeToRun` (réveille le PC en veille S3).

Publication **manuelle à l'origine** : l'upload via API depuis les serveurs GitHub avait
fait brider la chaîne (vidéos à 0 vue). Depuis l'IP du domicile, pas de problème constaté.

---

## 8. Configuration (`.env`)

```
GEMINI_API_KEY=     PEXELS_API_KEY=     PIXABAY_API_KEY=     YT_API_KEY=
YT_CHANNEL=@Attends_Quoi     TTS_ENGINE=edge     VOICE=fr-CA-AntoineNeural
TELEGRAM_TOKEN=     TELEGRAM_CHAT_ID=          (optionnels, pour --notify)
```

**Attention** : le parseur `_load_env()` (l. 41) ne retire PAS les commentaires en fin de
ligne. `CLE=valeur # note` casse la clé.

**Manquants à ce jour** : `client_secret.json` et `token.json` (OAuth YouTube).
Sans eux, `daily_upload.py` s'arrête proprement sans rien publier.
L'app OAuth doit en outre passer de **Test à Production**, sinon le refresh token expire
tous les 7 jours et l'automatisation meurt en silence.

---

## 9. Limites connues

1. **Le filtrage lit un texte, jamais l'image.** Un clip titré `ancient-ruins-of-ephesus`
   peut montrer des touristes modernes : rien ne le signale. Pas d'analyse visuelle (CLIP).
2. **Anti-doublon par identité de fichier**, pas par similarité visuelle. Deux clips
   distincts peuvent se ressembler (deux vues aériennes du même amphithéâtre).
3. **Pexels/Pixabay n'ont pas certains sujets.** Aucun plan des moulages de victimes de
   Pompéi : 15 clips rejetés à chaque tentative. Aucun filtrage ne crée une image absente.
4. **Pixabay est à ~90 % en paysage** et son paramètre `orientation` n'est pas un vrai
   filtre. On ne garde que le vertical, donc son apport est faible sur certains sujets.
5. **Durées de plan décorrélées du texte** sauf la révélation ; les coupes sont seulement
   calées sur les fins de phrase/mot.
6. **Quota Gemini** : 20 requêtes/jour PAR MODÈLE, 4 modèles. Le repli couvre désormais
   les 429, 404 ET les 503/UNAVAILABLE (une pointe de charge chez Google faisait échouer
   toute la vidéo).

---

## 10. Points d'entrée pour un correctif

| Besoin | Fonction | Ligne |
|---|---|---|
| Qualité éditoriale, requêtes visuelles | `make_script()` | 246 |
| Recherche et filtrage des clips | `fetch_clips()` | 1165 |
| Ordre, durée, VFX, rendu | `assemble()` | 1360 |
| Sous-titres et overlay | `build_subtitles()` | 861 |
| Orchestration, filtres contextuels | `main()` | 1648 |

**Règle de survie** : toujours lancer les scripts avec `.venv/Scripts/python.exe`.
Le `python` du système est un exécutable de 0 octet et échoue **silencieusement**.

---

## 11. Mesures Analytics — 12 septembre 2026

`analytics.py` lit enfin les vraies courbes (permission `yt-analytics.readonly`).
Jusqu'a cette date le projet publiait sans jamais savoir ou les gens partaient.

```
python analytics.py            tableau de toutes les videos, triees par APV
python analytics.py --hook     retention moyenne seconde par seconde
python analytics.py "fort knox"  courbe detaillee d'une video
```

### Ce que les donnees disent

**Le hook n'est PAS le probleme.** La retention au debut depasse 100 % (128 % a
0,3 s) : les spectateurs revoient l'ouverture. C'est le corps qui perd tout le
monde, avec une erosion continue jusqu'a 32 % en fin de video. APV median 58 %.

**Une falaise a 4,8 s.** 21 videos sur 25 avaient leur plus forte chute entre
4,4 et 5,3 s. Cause trouvee dans le montage : a `HOOK_WINDOW` (3,2 s) les plans
passaient d'un coup de 0,45 s a 1,5 s pendant que l'etalonnage, les flashs,
l'inclinaison et le vignettage s'arretaient tous au meme instant. Corrige par
une rampe `DECAY_UNTIL` : tout s'interpole desormais jusqu'a 9 s.

**La distribution, pas le contenu, explique aout.** Hors vacances la mediane est
de 1470 vues, pendant les vacances de 366. Les 12 videos basses sont toutes
publiees a 14h00 pile : televersees en lot puis programmees. Preuve que le
contenu n'est pas en cause : « L'homme frappe 7 fois par la foudre » affiche le
MEILLEUR taux de like de la chaine (2,6 %) et un APV de 104 %, pour 307 vues.
**Ne jamais reprendre le televersement en lot.**

### Bugs majeurs corriges le meme jour

1. `_words(None)` renvoyait `('none',)`. Le filtre exigeait ensuite ce mot dans
   chaque slug, donc **100 % des clips du corps etaient rejetes** dans toutes les
   videos. Elles tournaient sur 3-4 images repetees — ce qui explique tres
   probablement l'erosion mesuree ci-dessus.
2. `_normalise_visual_plan` imposait `_key_noun` comme ancre dure sur les
   requetes en texte simple : « bee wings vibrating flower » exigeait
   « vibrating » dans le clip. La pertinence est laissee a `key_tok`, qui a un
   repli en phase 1.
3. `fetch_clips` ignorait l'espece du SUJET : une scene ne nommant pas l'animal
   (« pollen macro ») faisait rejeter tous les clips du sujet comme « autre
   espece ». Parametre `species=` ajoute.
4. Pollinations incruste un filigrane malgre `nologo=true` -> `_strip_watermark`
   rogne les 6 % du bas.

Mesure apres correctifs sur un meme sujet : corps passe de **0 clip (4 images
repetees 4 fois)** a **8 clips pertinents, 1 seule repetition**.

### OAuth

L'app est passee en **Production** le 12/09/2026 (le mode Test revoquait le
refresh_token tous les 7 jours, ce qui a fait taire la chaine 24 jours). Ecran
de consentement adosse a https://kevrps22.github.io (depot public
`kevrps22/kevrps22.github.io`), verifie dans Search Console.

---

## 12. Clipper — Shorts decoupes dans une video longue (13/09/2026)

Nouvelle methode, facon Opus Clip. Le montage sur banques d'images a atteint son
plafond (des levres et un tissu orange pour un sujet « requins »).

```
python clipper/transcribe.py clipper/src/<id>.mp4           transcription mot a mot (~temps reel CPU)
python clipper/clipper.py clipper/src/<id>.mp4 --n 8 --titre "..."
```

Chaine : changements d'image (ffmpeg scene) + transcription -> Gemini choisit les
moments -> calage image ET phrase -> rendu 1080x1920 a la cadence de la source.

**Compare a 11 clips Opus Clip de la meme video** (`clipper/opus_map.json`) :
- Opus coupe sur les changements d'image (13 frontieres sur 20 a moins de 0,6 s)
  mais ignore la parole : debuts en plein mot. On ouvre sur l'image ET sur un
  debut de phrase, et on finit sur un point final avant l'image suivante.
- Repris d'Opus : encadre d'accroche blanc pendant 3,2 s, sous-titres en casse
  normale avec mot actif en couleur, 60 i/s.
- Mieux qu'Opus : pas de filigrane, sponsors et dons exclus, pas de recadrage
  automatique sur les avatars (5 plans vides sur un seul de ses clips).

Pieges regles : marge de fin qui mordait la phrase suivante (ils enchainent a
40 ms), apostrophes coupees par Whisper (« C 'EST »), groupes de sous-titres a
cheval sur deux phrases, bouts de replique precedente en ouverture, fins sur
« Euh... ». La video source est sous licence YouTube standard : ne rien publier
sans l'accord du createur.

### Page locale

```
python clipper/web.py        http://localhost:8765 (et l'adresse Wi-Fi affichee, pour le telephone)
```

On colle un lien YouTube, la page enchaine telechargement, transcription,
changements d'image, choix Gemini et rendu (`clipper/pipeline.py`), avec une
barre d'avancement et une estimation du temps restant. Les clips s'affichent avec
lecteur, score, telechargement et copie du titre. Un seul traitement a la fois.
Les etapes deja faites pour une video sont en cache dans `clipper/src`.
Mesure : 8 clips rendus en 5 min 40 quand la transcription est en cache.
