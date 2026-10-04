# auto_YT — Architecture technique

Fabrique de Shorts pour la chaîne **« Attends Quoi ?! »** (`@Attends_Quoi`).
Document de référence du 21 septembre 2026.

Le générateur d'origine (script Gemini → voix de synthèse → banques d'images) a
été supprimé : son plafond était atteint, le rendu « faisait généré par IA ».
Tout part désormais d'une vraie vidéo, d'une vraie voix.

---

## 0. En bref

| Élément | Valeur |
|---|---|
| Langage | Python 3.12 |
| Rendu | **FFmpeg en ligne de commande** — pas de MoviePy |
| Format | 1080×1920, cadence de la source, H.264, AAC |
| Transcription | faster-whisper `small` int8 CPU, `word_timestamps` |
| Choix éditorial | Gemini, bascule sur 8 modèles (20 req/jour **par modèle**) |
| Illustrations | Pexels (vidéos) + cartes typographiques rendues par FFmpeg |
| Rendu long | GitHub Actions, 2 000 min/mois, 2 vCPU |
| Publication | 1/jour, **depuis l'adresse de la maison** |

**Contrainte matérielle** : AMD RX 6400 en DirectML, pas de CUDA. Tout est CPU.

---

## 1. Arborescence

```
auto_YT/
├── clipper/
│   ├── clipper.py       découpe, calage, sous-titres, rendu, index par vidéo
│   ├── habiller.py      remontage explicatif d'un clip (voix conservée)
│   ├── pipeline.py      téléchargement → transcription → images → choix → rendu
│   ├── transcribe.py    transcription seule, en ligne de commande
│   ├── cloud.py         point d'entrée du workflow GitHub (2 étapes)
│   ├── github_io.py     release, envoi en tranches, lancement, suivi, récupération
│   ├── web.py + web/    page locale port 8765
│   └── requirements-cloud.txt   dépendances minimales du workflow
├── youtube/
│   ├── upload.py        envoi OAuth ; chemins ancrés sur la racine du dépôt
│   ├── analytics.py     YouTube Analytics API : rétention, APV, sources de trafic
│   └── dashboard.py     tableau de bord local
├── assets/fonts/        Montserrat Black + Bold + licence OFL
├── assets/music/bg.mp3  lit de musique de fond
├── media/               src/ out/ out_habille/ opus/ — hors dépôt (3 Go)
├── docs/                ce document, ETAT.md, opus_map.json
└── .github/workflows/clipper.yml
```

`media/` est intégralement régénérable : rien de ce qui s'y trouve n'est commité.

---

## 2. Clipper — découper une vidéo longue

```
python clipper/clipper.py media/src/<id>.mp4 --n 8 --titre "..."
```

1. `phrases()` regroupe les mots de Whisper en phrases (silence > 0,55 s).
2. `scenes()` relève les changements d'image (`ffmpeg select=scene`, seuil 0,30).
3. `choisir()` envoie la transcription à Gemini, qui rend les moments forts.
4. `caler()` **aligne le clip sur l'image ET sur la parole** : on ouvre sur un
   changement de plan qui coïncide avec un début de phrase, on ferme sur un point
   final avant le plan suivant.
5. `sous_titres()` écrit un fichier ASS, `rendre()` produit le MP4 vertical.
6. `produire()` écrit l'index `media/out/<id>.json` : titre, score, description,
   `publiable` (faux hors Creative Commons).

### Ce que la comparaison avec Opus Clip a appris

11 clips Opus de la même vidéo, relevés dans `docs/opus_map.json` :

- Opus coupe sur les changements d'image (13 frontières sur 20 à moins de 0,6 s)
  mais **ignore la parole** : ses clips démarrent en plein mot. D'où le double
  calage image + phrase, qui est notre seul vrai avantage éditorial.
- Repris d'Opus : encadré d'accroche blanc pendant 3,2 s (`HOOK_DUR`),
  sous-titres en casse normale avec le mot prononcé en couleur, 60 i/s.
- Mieux qu'Opus : pas de filigrane, sponsors et appels aux dons exclus, pas de
  recadrage automatique sur les avatars (5 plans vides sur un seul de ses clips).

### Sous-titres

Style unique `Cap`, Montserrat Black 86, contour 7 et ombre 5 sur boîte
translucide — la lisibilité prime sur l'élégance, un écran de téléphone au soleil
ne pardonne rien. Le mot prononcé passe en orange `#FFB020` et grossit de 12 %
(`\fscx112\fscy112`). Trois mots à l'écran (`WORDS_PER_CAP`), jamais à cheval sur
deux phrases, ligne de base à 1540 px pour éviter l'interface Shorts.

Pièges déjà réglés : marge de fin qui mordait la phrase suivante (les
interlocuteurs enchaînent à 40 ms), apostrophes coupées par Whisper (« C 'EST »),
bouts de réplique précédente en ouverture, fins sur « Euh… » (`VIDES`, `FILLERS`).

---

## 3. Habillage — la vidéo explicative

```
python clipper/habiller.py <id> --clip 7
```

C'est la réponse au problème de monétisation (section 6) : **on garde la voix de
l'invité, on remplace l'image**. Le résultat est une vidéo explicative montée, pas
un extrait reposté.

1. `plan_visuel()` — Gemini lit le texte du clip et rend une suite de scènes :
   soit une **carte typographique** (un chiffre, un mot fort), soit une **requête
   Pexels**, avec l'instant de chaque bascule.
2. `chercher_clip()` — Pexels, noté sur les noms communs de la requête ; la liste
   `ACTIONS` écarte les mots d'action qui ne décrivent rien de visuel, et une
   requête trop longue est rejouée sur ses deux premiers mots de contenu
   (au-delà, Pexels dérive complètement).
3. `scene_carte()` — texte centré sur fond `#0E1015`, accent `#FFB020`, zoom léger
   et fondu d'entrée ; `scene_video()` — Ken Burns, sens alterné d'une scène à
   l'autre pour que deux plans voisins ne bougent jamais pareil.
4. Transitions : **flash blanc de 0,05 s** à chaque bascule (`FLASH`).
5. Une **barre de progression** orange de 7 px en haut de l'écran :
   `drawbox=x=0:y=0:w='iw*t/<durée>':h=7:color=0xFFB020@0.85:t=fill`.

### Le son, en quatre pistes sous la voix

| Piste | Rôle | Réglage |
|---|---|---|
| voix | l'original, **aucun filtre** | — |
| `piste_sfx` | impacts aux bascules, clics | `VOL_SFX = 0.55` |
| `piste_ambiance` | souffle discret sous les plans vidéo | gain 0,22 |
| `piste_basse` + `bg.mp3` | lit continu, comble les silences | `VOL_MUSIQUE = 0.10` |

Le lit (musique + basse) est **ducké par la voix** (`sidechaincompress`), ce qui
suppose de dupliquer la voix avec `asplit=2` : une branche vers le mélange, une
vers la chaîne latérale. Sans ce dédoublement FFmpeg refuse
(`Stream specifier 'voix' matches no streams`).

Les bruitages sont **synthétisés en numpy** puis écrits en WAV (`_ecrire_wav`) :
aucune banque de sons à télécharger, aucune licence à vérifier.

---

## 4. Le rendu sur GitHub

`clipper.yml` + `cloud.py`, en deux étapes (`transcription` puis `rendu`), pour
que la transcription survive à un échec du rendu.

- **Le téléchargement ne peut pas s'y faire** : yt-dlp est bloqué depuis les
  adresses de centre de données. La source part d'ici, par la release
  `clipper-<id>`, et les clips en reviennent.
- **Envoi en tranches de 100 Mo** : GitHub renvoyait une erreur 500
  « Error saving asset » *après* dix minutes de transfert. Une tranche ratée se
  refait seule ; le workflow recolle avec `cat`. Les envois interrompus laissent
  des fichiers à l'état `starter`, absents de `rel["assets"]`, qui bloquent le
  nom : `_assets()` interroge la liste complète pour les supprimer.
- `CLIPPER_X264_PRESET=fast` : deux cœurs seulement, `medium` doublerait le temps.
- Mesure : **41 min** pour une vidéo source entière, transcription comprise.
  En local avec la transcription en cache : 8 clips en 5 min 40.
- Limites gratuites : 2 000 min/mois, 6 h par job, 2 Go par fichier de release
  (les artéfacts, eux, plafonnent à 500 Mo).
- **Le workflow ne touche à aucun secret YouTube.** Il ne publie rien.

---

## 5. Publication et mesure

`youtube/upload.py` — OAuth, trois permissions : `youtube.upload`,
`yt-analytics.readonly`, `youtube.readonly`. L'API ne sait pas modifier une vidéo
déjà en ligne : titre et description se corrigent à la main dans Studio.

L'application OAuth est passée en **Production le 12/09/2026**. En mode Test,
Google révoquait le `refresh_token` tous les 7 jours — ce qui avait fait taire la
chaîne 24 jours sans le moindre message d'erreur. Écran de consentement adossé à
`https://kevrps22.github.io`, vérifié dans la Search Console.

`youtube/analytics.py` lit les vraies courbes :

```
python youtube/analytics.py               toutes les vidéos, triées par APV
python youtube/analytics.py --hook        rétention moyenne seconde par seconde
python youtube/analytics.py "fort knox"   courbe détaillée d'une vidéo
```

### Ce que les données ont dit

**Le hook n'était pas le problème** : la rétention au début dépasse 100 % (128 % à
0,3 s — les spectateurs revoient l'ouverture). C'est le corps qui perdait tout le
monde, jusqu'à 32 % en fin de vidéo, APV médian 58 %. La cause s'est révélée être
un bug : `_words(None)` renvoyait `('none',)`, le filtre exigeait ensuite ce mot
dans chaque plan, donc **100 % des clips du corps étaient rejetés** et les vidéos
tournaient sur 3-4 images répétées.

**La distribution, pas le contenu, explique août.** Hors vacances la médiane est de
1 470 vues, pendant les vacances 366. Les 12 vidéos basses ont toutes été
téléversées en lot puis programmées à 14 h 00 pile. Preuve que le contenu n'y est
pour rien : « L'homme frappé 7 fois par la foudre » affiche le meilleur taux de
like de la chaîne (2,6 %) et un APV de 104 %, pour 307 vues.
**Ne jamais reprendre le téléversement en lot.**

**La note de viralité de Gemini ne prédit rien.** Une vidéo notée 98 a fait
2 300 vues, une notée 95 en a fait 193 000. Le score sert à ordonner une file
d'attente, pas à juger une vidéo.

---

## 6. Monétisation

Le règlement des Shorts exclut le contenu d'autres créateurs reposté et les
compilations sans apport propre. **Un clip brut, même crédité, n'est pas
monétisable** : c'est pour ça que `habiller.py` existe. Un montage explicatif
construit par-dessus la voix constitue un apport éditorial — cartes,
illustrations, rythme, sound design — et non un simple extrait.

Seuils du programme partenaire : 500 abonnés + 3 M de vues Shorts sur 90 jours, ou
1 000 abonnés + 10 M. Ces seuils **doublent le 1er février 2027**.

Question ouverte : ajouter une intro et une conclusion dites par Kevin, qui
ancreraient le format du côté du commentaire à valeur ajoutée sans ambiguïté.

---

## 7. Limites connues

1. **Le filtrage Pexels lit un texte, jamais l'image.** Un plan titré
   `ancient-ruins-of-ephesus` peut montrer des touristes modernes.
2. **Pexels n'a pas certains sujets.** Aucun filtrage ne crée une image absente ;
   les cartes typographiques sont le repli.
3. **Quota Gemini** : 20 requêtes/jour par modèle, 8 modèles accessibles
   (`gemini-3.8-flash` donne les meilleurs choix). Le repli couvre 429, 404 et 503.
5. La source doit être en Creative Commons. Le stock de chaînes utilisables
   (Thinkerview et assimilés) est à étoffer à la main.

---

## 8. Prochaine étape — l'Orange Pi Zero 3

Commandé le 17/09/2026 (37,85 €), livraison entre le 25/09 et le 02/10. Il tiendra
le rôle que le PC ne peut pas tenir — rester allumé — sans jamais rien rendre :

```
Orange Pi (maison)                    GitHub Actions
  télécharge la source CC  ─────────►  transcrit, choisit, rend
  récupère les clips       ◄─────────  dépose dans la release
  habille, propose au veto
  publie un clip le soir, depuis l'adresse de la maison
```

À faire à la réception : Debian sur carte SD, SSH, copie de `.env`,
`client_secret.json` et `token.json`, jeton GitHub à portée limitée dans
`GITHUB_TOKEN`, tâche du soir, veto depuis le téléphone.
