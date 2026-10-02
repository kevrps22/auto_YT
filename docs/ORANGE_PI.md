# Orange Pi Zero 3 — installation et exploitation

Le Pi fait les deux choses que GitHub ne peut pas faire : **télécharger** (YouTube
bloque les serveurs de centre de données) et **publier depuis la maison** (les
envois faits depuis GitHub avaient fait brider la chaîne). Tout le reste —
transcription, choix des moments, rendu des clips — part sur GitHub Actions.

```
09 h   soir.py --approvisionner    s'il reste ≤ 1 clip : nouvelle source → GitHub rend → récupère
19 h   soir.py --publier           habille le meilleur clip et le publie
```

---

## 1. Première connexion

Carte SD flashée, câble Ethernet branché, alimentation. Trouver l'adresse depuis
le PC :

```bash
ping orangepizero3.local
```

Si ça ne répond pas, scanner le réseau à la recherche du port 22 (PowerShell) :

```powershell
1..254 | % { $c=New-Object Net.Sockets.TcpClient; $t=$c.ConnectAsync("192.168.0.$_",22);
  Start-Sleep -m 20; if($t.Status -eq 'RanToCompletion'){"192.168.0.$_"}; $c.Close() }
```

Puis :

```bash
ssh root@<adresse>
```

Mot de passe par défaut : `1234` sur Armbian, `orangepi` sur l'image officielle.
Le système force un changement de mot de passe et la création d'un utilisateur.

**Fixer l'adresse** dans l'interface de la box (bail DHCP statique) : sans ça
l'adresse changera au prochain redémarrage et les scripts de secours ne
retrouveront plus la machine.

---

## 2. Le socle

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y python3-pip python3-venv ffmpeg git
```

**Mémoire** : le Zero 3 a peu de RAM et ffmpeg se fait tuer en plein rendu sans
espace d'échange. `sudo armbian-config` → System → activer zram, ou :

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

**Carte SD** : les écritures de journaux usent la carte. `log2ram` est installé
par défaut sur Armbian ; sur une autre image, l'ajouter.

---

## 3. Le projet

```bash
git clone https://github.com/kevrps22/auto_YT.git && cd auto_YT
python3 -m venv .venv
.venv/bin/pip install requests google-genai yt-dlp \
    google-api-python-client google-auth-oauthlib google-auth-httplib2
```

**Ne pas installer `requirements.txt`** : il embarque faster-whisper, que le Pi ne
doit jamais faire tourner. Deux heures d'entretien transcrites sur ce processeur
prendraient la nuit, alors que GitHub le fait en 40 minutes.

### Les secrets, depuis le PC

```bash
scp .env client_secret.json token.json kevin@<adresse>:~/auto_YT/
```

Ajouter dans le `.env` du Pi un jeton GitHub à portée limitée (Settings →
Developer settings → fine-grained token, permissions *Contents* et *Actions* en
écriture sur `kevrps22/auto_YT`) :

```
GITHUB_TOKEN=github_pat_...
```

Sans lui, `github_io.py` cherche l'accès enregistré par Git, qui n'existe pas sur
le Pi.

---

## 4. Recoller le journal — à faire UNE FOIS

```bash
.venv/bin/python youtube/soir.py --recoler
```

Les vidéos publiées à la main ne sont pas dans `media/publies.json`. Sans ce
recollement, le Pi republierait toute la série depuis le premier clip. La commande
lit les 100 dernières vidéos de la chaîne et marque comme publiés tous les clips
dont le titre correspond.

Vérifier ensuite :

```bash
.venv/bin/python youtube/soir.py --etat
```

---

## 5. Essais à blanc

Rien n'est envoyé à YouTube tant que `--simuler` est là :

```bash
.venv/bin/python youtube/soir.py --approvisionner --simuler
.venv/bin/python youtube/soir.py --publier --simuler
```

Puis un vrai essai de bout en bout, en commençant par l'habillage seul :

```bash
.venv/bin/python clipper/habiller.py <id> --clip 2     # compter 20 à 30 min
```

---

## 6. Les deux tâches automatiques

Quatre fichiers à créer. `Persistent=true` est le point important : une tâche
manquée pendant une coupure de courant se rattrape au redémarrage, ce que `cron`
ne sait pas faire.

`/etc/systemd/system/clipper-appro.service` :

```ini
[Unit]
Description=Clipper — approvisionnement du stock
After=network-online.target

[Service]
Type=oneshot
User=kevin
WorkingDirectory=/home/kevin/auto_YT
ExecStart=/home/kevin/auto_YT/.venv/bin/python youtube/soir.py --approvisionner
TimeoutStartSec=7200
```

`/etc/systemd/system/clipper-appro.timer` :

```ini
[Unit]
Description=Approvisionnement tous les matins

[Timer]
OnCalendar=*-*-* 09:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

`/etc/systemd/system/clipper-soir.service` : identique au premier, avec
`--publier` et `Description=Clipper — publication du soir`.

`/etc/systemd/system/clipper-soir.timer` : identique, avec `OnCalendar=*-*-* 19:00:00`.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now clipper-appro.timer clipper-soir.timer
systemctl list-timers clipper-*
```

---

## 7. Surveiller

```bash
.venv/bin/python youtube/soir.py --etat     # stock + derniers évènements
tail -f ~/auto_YT/media/soir.log            # le journal du pilote
journalctl -u clipper-soir -n 50            # ce que systemd a vu
```

Le verrou `media/soir.lock` empêche deux exécutions simultanées. Il est ignoré
au-delà de 6 h, pour qu'une coupure en plein travail ne bloque pas le lendemain.

---

## 8. Ce qui survit à une coupure de courant

| Étape | Après la coupure |
|---|---|
| Téléchargement | reprend le `.part` où il en était |
| Transcription, choix Gemini, plan visuel | relus depuis le cache |
| Envoi vers GitHub | reprend à la tranche de 100 Mo suivante |
| Récupération des clips | saute les fichiers déjà complets |
| Rendu des clips, habillage | **refaits entièrement** |

Le journal `media/publies.json` empêche de publier deux fois le même clip ou deux
fois le même jour. Si le courant saute **entre** l'envoi à YouTube et l'écriture du
journal, `--publier` rattrape le coup : il compare le titre aux 15 dernières vidéos
de la chaîne avant d'envoyer, et répare le journal s'il le trouve déjà en ligne.
