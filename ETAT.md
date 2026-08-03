# ETAT DU PROJET — reprise sur une autre machine

Dernière mise à jour : **1er août 2026**

---

## 1. Ce qu'est le projet

Chaîne YouTube Shorts **« Attends Quoi ?! »** (`@Attends_Quoi`) — vidéos de faits
fascinants générées automatiquement, publiées **manuellement** depuis le PC.

Deux briques :

| Dossier | Rôle |
|---|---|
| racine (`generate.py`, `upload.py`…) | pipeline Python : sujet → vidéo prête |
| `app/` | app Flutter Windows « YT Studio » : générer / uploader / suivre les stats |

---

## 2. Installation sur une nouvelle machine

```bash
git clone https://github.com/kevrps22/auto_YT.git
cd auto_YT
pip install -r requirements.txt
```

**Logiciels requis** (hors pip) :
- **FFmpeg** — `winget install Gyan.FFmpeg` (doit être dans le PATH)
- **eSpeak NG** — `winget install eSpeak-NG.eSpeak-NG` (seulement si TTS_ENGINE=kokoro)
- **Flutter** — pour recompiler l'app (`cd app && flutter build windows --release`)

**Fichiers à recréer à la main** (jamais commités, ils contiennent des secrets) :

`.env` à la racine :
```
GEMINI_API_KEY=...        # https://aistudio.google.com/apikey
PEXELS_API_KEY=...        # https://www.pexels.com/api/
YT_API_KEY=...            # clé API YouTube Data v3 (lecture des stats)
YT_CHANNEL=@Attends_Quoi
TTS_ENGINE=edge
VOICE=fr-CA-AntoineNeural
```

`client_secret.json` et `token.json` — OAuth YouTube :
1. Google Cloud Console → projet avec **YouTube Data API v3** activée
2. Identifiants → ID client OAuth → **Application de bureau** → télécharger en `client_secret.json`
3. `python upload.py --auth` (ouvre le navigateur)

⚠️ L'app OAuth est en mode **Test** : le token expire tous les **7 jours**, il faut
relancer `python upload.py --auth` quand l'upload API échoue.

---

## 3. Utilisation

```bash
python generate.py                    # sujet aléatoire (616 sujets dans topics.txt)
python generate.py "les requins"      # sujet imposé
python rescore.py --all               # renote la viralité de toutes les vidéos
python dashboard.py                   # dashboard web local (stats YouTube)
```

Chaque vidéo est archivée dans `output/lib/<date>_<slug>/` avec :
`<titre>.mp4`, `<titre> - miniature.jpg`, `poster.jpg`, `meta.json`.

**L'app Flutter** (raccourci « YT Studio » sur le bureau) : onglets Générer /
Upload (galerie triée par viralité) / Stats. Elle lance les scripts Python.

---

## 4. Stratégie — ce qu'on a appris

**Publier MANUELLEMENT depuis le PC.** L'upload via l'API depuis les serveurs
GitHub a fait brider la chaîne fin juillet (vidéos à 0 vue). Les uploads manuels
démarrent immédiatement. Le cron GitHub est à désactiver dans l'onglet Actions.

**1 vidéo/jour**, vers 18h. Deux par jour sur une chaîne jeune = signal spam.

**Ne pas juger une vidéo avant 24h.** Les vues arrivent par vagues, souvent
entre 4h et 8h après publication. Plusieurs vidéos données pour mortes à 2h ont
fini au-dessus de 1000 vues.

**Les sujets qui marchent** : le corps du spectateur, son argent, son quotidien.
Ceux qui plafonnent : l'espace, l'histoire ancienne, tout ce qui est lointain.

### Résultats

| Vidéo | Vues | Ont balayé |
|---|---|---|
| Le cerveau (ancien format) | 242 | 84,7 % |
| Les secrets du cœur | 1 342 | 76,9 % |
| Le Voleur Invisible (argent) | 1 836 | 76,8 % |
| Les rêves (nouveau hook) | 1 700 | **72,9 %** |

---

## 5. Le pipeline en détail

**Script** — Gemini (bascule automatique entre 4 modèles, quota gratuit = 20
requêtes/jour **par modèle**). Structure imposée : hook → tension → body →
révélation → boucle. Règles clés : **Information Gap** (le hook ne nomme jamais
le sujet, il dit « ce truc », « cette chose »), conséquence avant la cause,
la tension n'est libérée qu'à la fin. Gemini note aussi la viralité sur 100.

**Voix** — edge-tts `fr-CA-AntoineNeural`. Ton différent par segment (hook
rapide et énergique, tension ralentie pour le twist). Pauses dramatiques avant
le twist et avant la chute. Les blancs sont coupés (seuil −30 dB) : ~19 % de
temps mort en moins. Sortie en WAV pur, **aucun filtre** sur la voix — les
compresseurs/limiteurs la faisaient grésiller.

**Sous-titres** — alignement mot par mot via faster-whisper. Police Anton.
Jaune pur pour le karaoké, **rouge néon** sur les mots chocs, **cyan** sur les
chiffres. 2 mots max à l'écran pendant le hook.

**Visuels** — les 3 premiers plans sont des **images IA générées par
Pollinations** (gratuit, sans clé, illimité, mais 576×1024 en natif). Le prompt
impose que l'image porte la même tension que le texte. Le reste vient de Pexels.

**Montage** — coupes toutes les 0,45-0,7 s dans le hook, 2,2-3,6 s ensuite.
Snap zoom, secousse caméra, glitch RGB, dutch angle, flashs blancs, vignettage.
Sound design : boom à 0 s, whoosh à 0,5 s, **coupure totale de la musique à
1,5 s**, riser à 1,7 s.

---

## 6. Pièges déjà rencontrés (ne pas refaire)

- **Ne jamais mettre de compresseur/limiteur sur la voix** → grésillement.
- Le fond vidéo doit être **ré-encodé** (jamais `-c copy`) : le zoompan casse les
  timestamps et l'audio se retrouve tronqué à ~7 s.
- Le **dutch angle** doit agrandir l'image **avant** de la tourner, avec le
  facteur `cos(a) + sin(a)·(H/W)`, sinon des bandes noires apparaissent.
- Gemini renvoie parfois une **liste** au lieu d'une chaîne (champ `body`) : les
  champs sont aplatis automatiquement depuis.
- Les modèles `gemini-1.5-flash` et `gemini-2.5-flash` sont **inaccessibles** aux
  nouveaux comptes. Utiliser `gemini-flash-latest`.

---

## 7. Prochaines pistes

- **Voix locale** — tester les modèles de synthèse vocale de Hugging Face
  (`huggingface.co/spaces?category=speech-synthesis`). edge-tts reste un peu
  monocorde. Kokoro est déjà installé (`TTS_ENGINE=kokoro`) mais sa seule voix
  française est féminine. Attention : nécessite un vrai GPU pour être rapide.
- **Résolution des images IA** — Pollinations plafonne à 576×1024. Alternative :
  Cloudflare Workers AI (compte gratuit sans CB, 10 000 crédits/jour, SDXL en
  1024×1024).
- **Enrichir `topics.txt`** avec des sujets corps / argent / quotidien, qui sont
  les veines les plus performantes.
- **Vacances du 10 au 31 août** : produire le stock vers le 3-4 août, puis
  uploader en programmé ~5 vidéos/jour du 5 au 9 août (jamais plus, pour éviter
  un burst suspect).
