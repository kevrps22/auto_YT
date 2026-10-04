# ÉTAT DU PROJET — reprise sur une autre machine

Dernière mise à jour : **21 septembre 2026**

---

## 1. Où on en est

La chaîne **« Attends Quoi ?! »** (`@Attends_Quoi`) ne génère plus de vidéos de
toutes pièces. Elle découpe des vidéos longues sous licence Creative Commons
(Clipper), puis remonte le clip en vidéo explicative en gardant la voix
d'origine (`habiller.py`). Détail technique dans [ARCHITECTURE.md](ARCHITECTURE.md).

L'ancien générateur (`generate.py`, `character.py`, `topics.txt`, `batch.py`,
`rescore.py`, `export_queue.py`) a été **supprimé**. Ne pas le ressusciter : le
rendu sur banques d'images avait atteint son plafond.

---

## 2. Installation sur une machine neuve

```bash
git clone https://github.com/kevrps22/auto_YT.git
cd auto_YT
pip install -r requirements.txt
winget install Gyan.FFmpeg      # doit être dans le PATH
```

Pour recompiler l'app : `cd app && flutter build windows --release`.

**Fichiers à recréer à la main** — jamais commités, ils contiennent des secrets.

`.env` à la racine :

```
GEMINI_API_KEY=...        # https://aistudio.google.com/apikey
PEXELS_API_KEY=...        # https://www.pexels.com/api/
YT_API_KEY=...            # YouTube Data v3, lecture des stats
YT_CHANNEL=@Attends_Quoi
```

Le parseur ne retire pas les commentaires en fin de ligne : `CLE=valeur # note`
casse la clé.

`client_secret.json` et `token.json` — OAuth YouTube :

1. Google Cloud Console → projet avec **YouTube Data API v3** activée
2. Identifiants → ID client OAuth → **Application de bureau** → `client_secret.json`
3. `python youtube/upload.py --auth` (ouvre le navigateur, écrit `token.json`)

L'application est en **Production** depuis le 12/09/2026 : le jeton ne meurt plus
au bout de sept jours.

---

## 3. Ce qui marche aujourd'hui

| Quoi | Commande | Mesure |
|---|---|---|
| Page locale, de bout en bout | `python clipper/web.py` | 8 clips en 5 min 40 (transcription en cache) |
| Rendu déporté | `python clipper/github_io.py envoyer <lien>` | 41 min par vidéo source |
| Vidéo explicative | `python clipper/habiller.py <id> --clip 7` | sortie dans `media/out_habille` |
| Vraies courbes de rétention | `python youtube/analytics.py --hook` | — |

La publication reste **manuelle, depuis le PC de la maison**, tous les soirs.

---

## 4. Ce qui reste à faire

- **Orange Pi Zero 3** commandé le 17/09/2026, livraison 25/09 → 02/10 : Debian
  sur carte SD, SSH, copie des trois fichiers de secrets, jeton GitHub à portée
  limitée, tâche du soir, veto depuis le téléphone.
- **Niveaux sonores** de l'habillage à valider à l'oreille : `VOL_MUSIQUE`,
  `VOL_SFX` en tête de `habiller.py`, et le gain 0,22 dans `piste_ambiance`.
- **Intro et conclusion dites par Kevin** — question ouverte, ça lèverait toute
  ambiguïté côté monétisation.
- **Étoffer le stock de chaînes en Creative Commons** utilisables comme sources.

---

## 5. Stratégie — ce qu'on a appris

**Publier depuis la maison, une vidéo par jour, vers 18 h.** L'envoi par l'API
depuis les serveurs GitHub avait fait brider la chaîne (vidéos à 0 vue).

**Ne jamais téléverser en lot.** Les 12 vidéos les plus basses d'août avaient
toutes été programmées à 14 h 00 pile. Le contenu n'y était pour rien.

**Ne pas juger une vidéo avant 48 h.** Les vues arrivent par vagues ; celle qui a
fini à 193 000 vues stagnait à 2 000 pendant deux jours.

**Les sujets qui marchent** : le corps, l'argent, le quotidien. Ceux qui
plafonnent : l'espace, l'histoire ancienne, tout ce qui est lointain.

---

## 6. Pièges déjà rencontrés

- **Aucun filtre sur la voix** : les compresseurs la font grésiller.
- Le fond vidéo doit être **ré-encodé** (jamais `-c copy`) : le zoompan casse les
  timestamps et l'audio se retrouve tronqué à ~7 s.
- Une entrée FFmpeg ne peut pas être consommée deux fois : dupliquer avec
  `asplit` avant d'envoyer la voix dans un `sidechaincompress`.
- Gemini renvoie parfois une liste au lieu d'une chaîne : les champs sont aplatis.
- GitHub refuse les fichiers de release envoyés d'un seul tenant au-delà de
  ~100 Mo (erreur 500 en fin de transfert) : `github_io.py` découpe en tranches.
- yt-dlp est bloqué depuis les serveurs GitHub : le téléchargement se fait ici.
