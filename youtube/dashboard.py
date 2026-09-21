"""
dashboard.py — Tableau de bord YouTube LIVE (serveur local, auto-refresh).

- Lit les stats via l'API YouTube Data v3 (cle API, lecture seule, sans OAuth).
- Se rafraichit tout seul (par defaut toutes les 60s) sans recharger la page.
- Enregistre un point a chaque scan -> construit un historique -> graphiques par video.
- Analyse chaque video : pourquoi peu / beaucoup de vues (heuristique).

Config (.env) :
  YT_API_KEY=AIza...
  YT_CHANNEL=@Attends_Quoi

Usage :
  python dashboard.py           # ouvre http://localhost:8765
"""

import json
import os
import statistics
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import requests

API = "https://www.googleapis.com/youtube/v3"
PORT = 8765
REFRESH_SEC = 60
HISTORY_FILE = Path(__file__).with_name("history.json")
MAX_POINTS = 500          # points d'historique gardes par video


# ------------------------------------------------------------------ env
def _load_env():
    env = Path(__file__).with_name(".env")
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()
KEY = os.getenv("YT_API_KEY")
CHANNEL = os.getenv("YT_CHANNEL", "@Attends_Quoi")


# ------------------------------------------------------------------ API
def _get(endpoint, **params):
    params["key"] = KEY
    r = requests.get(f"{API}/{endpoint}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def resolve_channel():
    if CHANNEL.startswith("UC"):
        data = _get("channels", part="snippet,statistics,contentDetails", id=CHANNEL)
    else:
        data = _get("channels", part="snippet,statistics,contentDetails", forHandle=CHANNEL.lstrip("@"))
    if not data.get("items"):
        raise SystemExit(f"Chaine introuvable : {CHANNEL}")
    return data["items"][0]


def video_ids(uploads):
    ids, tok = [], None
    while True:
        resp = _get("playlistItems", part="contentDetails", playlistId=uploads,
                    maxResults=50, **({"pageToken": tok} if tok else {}))
        ids += [it["contentDetails"]["videoId"] for it in resp.get("items", [])]
        tok = resp.get("nextPageToken")
        if not tok:
            break
    return ids


def videos_stats(ids):
    out = []
    for i in range(0, len(ids), 50):
        resp = _get("videos", part="snippet,statistics", id=",".join(ids[i:i + 50]))
        out += resp.get("items", [])
    return out


# ------------------------------------------------------------------ etat + historique
_state = {"data": None, "error": None}
_lock = threading.Lock()
_channel = None
_uploads = None
_ids = []
_poll = 0


def _load_history():
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


_history = _load_history()


def _save_history():
    HISTORY_FILE.write_text(json.dumps(_history), encoding="utf-8")


def _num(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return 0


def _age_hours(iso):
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


def _fmt_age(h):
    if h < 1:
        return f"il y a {int(h * 60)} min"
    if h < 24:
        return f"il y a {int(h)} h"
    return f"il y a {int(h / 24)} j"


def _velocity(hist):
    """Vues gagnees par heure sur les ~30 dernieres minutes (depuis l'historique)."""
    if len(hist) < 2:
        return None
    now_t, now_v = hist[-1]
    target = now_t - 1800          # 30 min avant
    ref = min(hist, key=lambda p: abs(p[0] - target))
    dt_h = (now_t - ref[0]) / 3600
    if dt_h <= 0:
        return None
    return (now_v - ref[1]) / dt_h


def _analyze(views, age_h, vel, median):
    """Renvoie (emoji, verdict, explication)."""
    if age_h < 0.2:
        return "🆕", "Tout juste publiee", "Laisse passer 1-2h, l'algo commence sa phase de test."
    if vel is not None and vel > 40:
        return "🔥", "En train de percer", "L'algo la pousse fort en ce moment. Bon signe !"
    if views >= median * 1.3 and median > 0:
        return "✅", "Au-dessus de ta moyenne", "Cette video performe mieux que tes autres."
    if age_h > 3 and views < 30:
        return "⚠️", "Distribution tres faible", ("Quasi pas diffusee. Possible bridage (cadence/automatisation) "
                                                  "ou hook/sujet moins accrocheur.")
    if (vel is None or vel < 3) and age_h > 6:
        return "😴", "L'algo a arrete de pousser", "Elle a eu sa vague puis s'est stabilisee. Normal apres 1 jour."
    if median > 0 and views < median * 0.5:
        return "🔻", "Sous ta moyenne", "Moins bien que d'habitude : teste un autre sujet/hook."
    return "➡️", "Diffusion normale", "Rien d'anormal, elle suit son cours."


def refresh():
    """Scan complet : recupere les stats, met a jour l'historique et l'etat."""
    global _channel, _uploads, _ids, _poll
    try:
        if _channel is None:
            _channel = resolve_channel()
            _uploads = _channel["contentDetails"]["relatedPlaylists"]["uploads"]
        # re-decouvre les nouvelles videos toutes les 10 passes (economie de quota)
        if _poll % 10 == 0 or not _ids:
            _ids = video_ids(_uploads)
        _poll += 1

        chan_stats = _get("channels", part="statistics", id=_channel["id"])["items"][0]["statistics"]
        vids = videos_stats(_ids)
        now = time.time()

        # enregistre un point d'historique par video
        for v in vids:
            vid = v["id"]
            views = _num(v["statistics"].get("viewCount"))
            h = _history.setdefault(vid, [])
            if not h or now - h[-1][0] >= 25:      # eviter les doublons trop rapproches
                h.append([now, views])
                del h[:-MAX_POINTS]
        _save_history()

        median = statistics.median([_num(v["statistics"].get("viewCount")) for v in vids]) if vids else 0

        out_videos = []
        for v in sorted(vids, key=lambda x: x["snippet"]["publishedAt"], reverse=True):
            st, sn = v["statistics"], v["snippet"]
            vid = v["id"]
            views = _num(st.get("viewCount"))
            age_h = _age_hours(sn["publishedAt"])
            hist = _history.get(vid, [])
            vel = _velocity(hist)
            emoji, verdict, expl = _analyze(views, age_h, vel, median)
            out_videos.append({
                "id": vid,
                "title": sn["title"],
                "thumb": sn["thumbnails"].get("medium", sn["thumbnails"]["default"])["url"],
                "age": _fmt_age(age_h),
                "views": views,
                "likes": _num(st.get("likeCount")),
                "comments": _num(st.get("commentCount")),
                "velocity": round(vel, 1) if vel is not None else None,
                "emoji": emoji, "verdict": verdict, "explain": expl,
                "history": [[int(t * 1000), val] for t, val in hist],   # ms pour JS
            })

        sn = _channel["snippet"]
        with _lock:
            _state["data"] = {
                "updated": datetime.now().strftime("%H:%M:%S"),
                "channel": {
                    "title": sn["title"],
                    "avatar": sn["thumbnails"]["default"]["url"],
                    "subs": _num(chan_stats.get("subscriberCount")),
                    "views": _num(chan_stats.get("viewCount")),
                    "videos": _num(chan_stats.get("videoCount")),
                },
                "videos": out_videos,
            }
            _state["error"] = None
    except Exception as e:
        with _lock:
            _state["error"] = str(e)


def _loop():
    while True:
        refresh()
        time.sleep(REFRESH_SEC)


# ------------------------------------------------------------------ serveur
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def do_GET(self):
        if self.path.startswith("/api/data"):
            with _lock:
                payload = {"error": _state["error"], **(_state["data"] or {})}
            self._send(json.dumps(payload), "application/json")
        else:
            self._send(PAGE.replace("__REFRESH__", str(REFRESH_SEC)), "text/html; charset=utf-8")


PAGE = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dashboard YouTube</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
<style>
  :root{color-scheme:dark}*{box-sizing:border-box}
  body{margin:0;font-family:system-ui,Segoe UI,Roboto,sans-serif;background:#0f0f0f;color:#f1f1f1}
  .wrap{max-width:940px;margin:0 auto;padding:24px}
  header{display:flex;align-items:center;gap:16px}
  header img{width:64px;height:64px;border-radius:50%}
  header h1{margin:0;font-size:1.4rem}
  .live{margin-left:auto;font-size:.8rem;color:#9aa}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#3ba55d;margin-right:6px;animation:p 2s infinite}
  @keyframes p{50%{opacity:.3}}
  .cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:20px 0 26px}
  .card{background:#1c1c1c;border-radius:14px;padding:18px;text-align:center}
  .card .v{font-size:1.8rem;font-weight:700}.card .l{color:#aaa;font-size:.8rem;margin-top:4px}
  .row{display:flex;align-items:center;gap:14px;padding:12px 6px;border-bottom:1px solid #262626}
  .row img{width:110px;border-radius:8px;cursor:pointer}
  .meta{flex:1;min-width:0}
  .meta .t{font-weight:600}.meta .d{color:#888;font-size:.8rem;margin-top:2px}
  .badge{display:inline-block;font-size:.75rem;background:#262626;border-radius:20px;padding:3px 10px;margin-top:6px}
  .nums{text-align:right;white-space:nowrap}
  .nums .views{color:#ffd400;font-weight:700;font-size:1.1rem}
  .nums .sub{color:#aaa;font-size:.8rem}
  .btn{background:#333;border:0;color:#f1f1f1;border-radius:8px;padding:8px 12px;cursor:pointer;font-size:.85rem;margin-left:10px}
  .btn:hover{background:#ff4e45}
  dialog{background:#181818;color:#f1f1f1;border:1px solid #333;border-radius:16px;max-width:640px;width:92%;padding:24px}
  dialog::backdrop{background:rgba(0,0,0,.6)}
  dialog h2{margin:0 0 4px}.close{position:absolute}
  .expl{background:#222;border-radius:10px;padding:12px;margin:14px 0;font-size:.92rem}
  .mrow{display:flex;gap:18px;margin:10px 0;font-size:.9rem;color:#ccc}
  .mrow b{color:#fff}
  @media(max-width:600px){.cards{grid-template-columns:1fr}.row img{width:80px}}
</style></head><body><div class="wrap">
  <header>
    <img id="ava"><h1 id="cname">Chargement…</h1>
    <div class="live"><span class="dot"></span><span id="upd">—</span></div>
  </header>
  <div class="cards">
    <div class="card"><div class="v" id="subs">—</div><div class="l">Abonnes</div></div>
    <div class="card"><div class="v" id="tviews">—</div><div class="l">Vues totales</div></div>
    <div class="card"><div class="v" id="tvids">—</div><div class="l">Videos</div></div>
  </div>
  <div id="list"></div>
</div>

<dialog id="modal">
  <h2 id="m-title"></h2>
  <div id="m-verdict" class="badge"></div>
  <div id="m-expl" class="expl"></div>
  <div class="mrow"><span>Vues : <b id="m-views"></b></span><span>Likes : <b id="m-likes"></b></span>
       <span>Comm. : <b id="m-comm"></b></span><span>Vitesse : <b id="m-vel"></b></span></div>
  <canvas id="chart" height="220"></canvas>
  <div style="text-align:right;margin-top:16px"><button class="btn" onclick="modal.close()">Fermer</button></div>
</dialog>

<script>
const fmt = n => n.toLocaleString('fr-FR');
let DATA = null, chart = null;

async function tick(){
  try{
    const d = await (await fetch('/api/data')).json();
    if(d.error){ document.getElementById('upd').textContent = 'erreur: '+d.error; return; }
    if(!d.channel) return;
    DATA = d;
    document.getElementById('ava').src = d.channel.avatar;
    document.getElementById('cname').textContent = d.channel.title;
    document.getElementById('subs').textContent = fmt(d.channel.subs);
    document.getElementById('tviews').textContent = fmt(d.channel.views);
    document.getElementById('tvids').textContent = fmt(d.channel.videos);
    document.getElementById('upd').textContent = 'MAJ '+d.updated;
    render(d.videos);
  }catch(e){ document.getElementById('upd').textContent = 'hors ligne'; }
}

function render(videos){
  const list = document.getElementById('list');
  list.innerHTML = '';
  videos.forEach((v,i)=>{
    const el = document.createElement('div');
    el.className='row';
    const vel = v.velocity!=null ? (v.velocity>0?'+':'')+v.velocity+'/h' : '—';
    el.innerHTML = `
      <img src="${v.thumb}" onclick="openModal(${i})">
      <div class="meta">
        <div class="t">${v.title}</div>
        <div class="d">${v.age}</div>
        <div class="badge">${v.emoji} ${v.verdict}</div>
      </div>
      <div class="nums">
        <div class="views">${fmt(v.views)}</div>
        <div class="sub">👍 ${fmt(v.likes)} · 💬 ${fmt(v.comments)}</div>
        <div class="sub">⚡ ${vel}</div>
      </div>
      <button class="btn" onclick="openModal(${i})">📊 Détails</button>`;
    list.appendChild(el);
  });
}

function openModal(i){
  const v = DATA.videos[i];
  document.getElementById('m-title').textContent = v.title;
  document.getElementById('m-verdict').textContent = v.emoji+' '+v.verdict;
  document.getElementById('m-expl').textContent = v.explain;
  document.getElementById('m-views').textContent = fmt(v.views);
  document.getElementById('m-likes').textContent = fmt(v.likes);
  document.getElementById('m-comm').textContent = fmt(v.comments);
  document.getElementById('m-vel').textContent = v.velocity!=null ? v.velocity+' vues/h' : '—';
  const pts = v.history.map(p=>({x:p[0], y:p[1]}));
  if(chart) chart.destroy();
  chart = new Chart(document.getElementById('chart'), {
    type:'line',
    data:{datasets:[{label:'Vues', data:pts, borderColor:'#ffd400',
      backgroundColor:'rgba(255,212,0,.12)', fill:true, tension:.3, pointRadius:0}]},
    options:{scales:{x:{type:'time',time:{unit:'hour'},ticks:{color:'#999'},grid:{color:'#222'}},
      y:{ticks:{color:'#999'},grid:{color:'#222'}}},
      plugins:{legend:{display:false}}}
  });
  document.getElementById('modal').showModal();
}

tick();
setInterval(tick, __REFRESH__*1000);
</script>
</body></html>"""


def main():
    if not KEY:
        raise SystemExit("Ajoute YT_API_KEY=... dans .env")
    refresh()                                    # premier scan avant d'ouvrir
    threading.Thread(target=_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://localhost:{PORT}"
    print(f"Dashboard live : {url}  (Ctrl+C pour arreter)")
    webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nArret.")


if __name__ == "__main__":
    main()
