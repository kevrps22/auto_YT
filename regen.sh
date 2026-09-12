#!/usr/bin/env bash
export PATH="$PATH:/c/Users/kevin/AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-8.1.2-full_build/bin"
export PYTHONIOENCODING=utf-8
cd "/c/Users/kevin/Documents/yt-shorts-bot"
# la liste passe par le DESCRIPTEUR 3 : sans cela python lit l'entree standard
# et avale les sujets restants -> une seule video generee.
while IFS= read -r t <&3; do
  [ -z "$t" ] && continue
  echo "=============== $t ==============="
  python -u generate.py "$t" </dev/null 2>&1 \
    | grep -viE "warning|futurew" \
    | grep -E "^\[script\] genere|^\[viralite|^\[visuels\] \[corps|^\[montage\] OK|^\[galerie|Traceback|Error"
done 3<<'TOPICS'
l'argent liquide
les cartes bancaires
le cafe
tes dents
le sel
la peste noire
le grand incendie de Londres 1666
les fourmis
TOPICS
echo "=============== FIN ==============="
