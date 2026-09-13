"""Transcription mot a mot d'une video longue (faster-whisper, CPU)."""
import json, sys, time
from pathlib import Path
from faster_whisper import WhisperModel

src = Path(sys.argv[1]); out = src.with_suffix(".words.json")
t0 = time.time()
model = WhisperModel("small", device="cpu", compute_type="int8")
segs, info = model.transcribe(str(src), language="fr", word_timestamps=True,
                              vad_filter=True, beam_size=5)
words = []
for s in segs:
    for w in s.words or []:
        words.append({"w": w.word.strip(), "s": round(w.start, 2), "e": round(w.end, 2)})
    print(f"  {s.end:7.1f}s / {info.duration:.0f}s  ({time.time()-t0:.0f}s)", flush=True)
out.write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
print(f"OK {len(words)} mots -> {out}  en {time.time()-t0:.0f}s")
