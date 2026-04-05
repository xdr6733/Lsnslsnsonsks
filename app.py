import base64
import json
import logging
import os
import threading
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta
from io import BytesIO

from flask import Flask, request, jsonify, render_template_string
import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo, MenuButtonWebApp
)

# ══════════════════════════════════════════════════════════════════
#  AYARLAR
# ══════════════════════════════════════════════════════════════════
BOT_TOKEN  = "8795486076:AAGlW5Rq92xHztoU-5pa8zdbVt4Fum23UFI"
ADMIN_ID   = 7181611360
PUBLIC_URL = "https://safeteam.onrender.com"
PORT       = 5000

# ══════════════════════════════════════════════════════════════════
#  FLASK + TELEBOT
# ══════════════════════════════════════════════════════════════════
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20MB (base64 dosya icin)

bot = telebot.TeleBot(BOT_TOKEN)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

reports: dict = {}

# ══════════════════════════════════════════════════════════════════
#  LOG & RATE-LIMIT
# ══════════════════════════════════════════════════════════════════
LOGS_DIR   = "logs"
GLOBAL_LOG = os.path.join(LOGS_DIR, "global.jsonl")
os.makedirs(os.path.join(LOGS_DIR, "users"), exist_ok=True)

activity_log: deque = deque(maxlen=1000)
_rate_windows: dict = defaultdict(lambda: deque())
banned_ips: set     = set()
_log_lock           = threading.Lock()

RATE_LIMIT    = 20
RATE_WINDOW   = 60
BAN_THRESHOLD = 60
BAN_WINDOW    = 300


def _uid_safe(uid: str) -> str:
    s = str(uid).strip()
    return s if s.isdigit() else "unknown"


def _user_log(uid: str) -> str:
    return os.path.join(LOGS_DIR, "users", f"uid_{_uid_safe(uid)}.jsonl")


def _write(path: str, rec: dict):
    with _log_lock:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as e:
            log.warning(f"[LOG] {path}: {e}")


def get_ip() -> str:
    for h in ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP"):
        v = request.headers.get(h)
        if v:
            return v.split(",")[0].strip()
    return request.remote_addr or "0.0.0.0"


def log_event(event: str, ip: str, uid="", uname="", detail="", extra: dict = None):
    rec = {
        "ts":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "event":    event,
        "ip":       ip,
        "user_id":  uid,
        "username": uname,
        "detail":   detail,
        "ua":       (request.headers.get("User-Agent", "") if request else "")[:150],
        "path":     (request.path if request else ""),
        "method":   (request.method if request else ""),
    }
    if extra:
        rec.update(extra)
    activity_log.appendleft(rec)
    _write(GLOBAL_LOG, rec)
    if uid and _uid_safe(uid) != "unknown":
        _write(_user_log(uid), rec)
    return rec


def rate_check(ip: str):
    if ip in banned_ips:
        return True, "banned"
    now      = datetime.now()
    w        = _rate_windows[ip]
    cut_ban  = now - timedelta(seconds=BAN_WINDOW)
    cut_rate = now - timedelta(seconds=RATE_WINDOW)
    while w and w[0] < cut_ban:
        w.popleft()
    w.append(now)
    n_ban = sum(1 for t in w if t > cut_ban)
    if n_ban >= BAN_THRESHOLD:
        banned_ips.add(ip)
        log.warning(f"[BAN] {ip} ({n_ban} req/5m)")
        try:
            bot.send_message(ADMIN_ID,
                f"🚨 *OTOMATİK IP ENGELİ*\n\n🌐 `{ip}`\n📊 {n_ban} istek/5dk",
                parse_mode="Markdown")
        except Exception:
            pass
        return True, "banned"
    if sum(1 for t in w if t > cut_rate) > RATE_LIMIT:
        return True, "rate_limited"
    return False, ""


# ══════════════════════════════════════════════════════════════════
#  ACCESS DENIED HTML (Telegram disi erisim)
# ══════════════════════════════════════════════════════════════════
ACCESS_DENIED_HTML = """<!DOCTYPE html>
<html lang="tr"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Erişim Engeli</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{min-height:100vh;background:#07080B;display:flex;flex-direction:column;
     align-items:center;justify-content:center;font-family:monospace;padding:32px;text-align:center}
.icon{font-size:52px;margin-bottom:20px}
.t1{color:#F74F6A;font-size:13px;font-weight:700;letter-spacing:4px;margin-bottom:12px}
.t2{color:#2E4057;font-size:11px;line-height:1.9}
.t2 b{color:#4A6080}
.foot{margin-top:28px;color:#1A2A38;font-size:9px;letter-spacing:2px}
</style></head><body>
<div class="icon">⛔</div>
<div class="t1">ERİŞİM ENGELİ</div>
<div class="t2">Bu uygulama yalnızca<br><b>Telegram</b> üzerinden<br>kullanılabilir.</div>
<div class="foot">// ACCESS DENIED · TELEGRAM ONLY</div>
</body></html>"""


# ══════════════════════════════════════════════════════════════════
#  MINI APP HTML
# ══════════════════════════════════════════════════════════════════
MINI_APP_HTML = r"""<!DOCTYPE html>
<html lang="tr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Safe Team Report</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg0:#07080B;--bg1:#0C0F14;--bg2:#111520;--bg3:#161C28;
  --line:#1E2840;--line2:#253050;
  --acc:#4F8EF7;--acc2:#3A6FD8;--ag:rgba(79,142,247,.12);--ag2:rgba(79,142,247,.06);
  --red:#F74F6A;--rg:rgba(247,79,106,.12);
  --grn:#3DFFA0;--gg:rgba(61,255,160,.1);
  --amb:#FFB830;
  --t1:#D4E2F8;--t2:#7A96C0;--t3:#3A5278;--t4:#1E3050;
}
*{margin:0;padding:0;box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{background:var(--bg0);color:var(--t1);font-family:'Inter',sans-serif;min-height:100vh;overflow-x:hidden}

/* OFFLINE OVERLAY — internetin yokken icerigi gizler, URL gostermez */
#net-overlay{
  display:none;position:fixed;inset:0;z-index:9999;
  background:var(--bg0);flex-direction:column;
  align-items:center;justify-content:center;text-align:center;padding:32px;
}
#net-overlay.show{display:flex}
.no-icon{font-size:48px;margin-bottom:16px;opacity:.5}
.no-t1{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:600;
  letter-spacing:2px;color:var(--t3);margin-bottom:8px}
.no-t2{font-size:11px;color:var(--t4);line-height:1.7}

/* TG GUARD */
#tg-guard{display:none;position:fixed;inset:0;z-index:9998;background:var(--bg0);
  flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:32px}
#tg-guard.show{display:flex}
.gd-ic{font-size:52px;margin-bottom:20px}
.gd-t1{color:var(--red);font-family:'JetBrains Mono',monospace;font-size:13px;
  font-weight:600;letter-spacing:3px;margin-bottom:10px}
.gd-t2{color:var(--t3);font-size:12px;line-height:1.8}
.gd-t2 b{color:var(--t2)}
.gd-ft{margin-top:24px;color:var(--t4);font-family:'JetBrains Mono',monospace;font-size:8px;letter-spacing:2px}

/* SHELL */
.shell{max-width:440px;margin:0 auto;padding:0 16px 100px}

/* TOP BAR */
.topbar{display:flex;align-items:center;justify-content:space-between;
  padding:16px 0 14px;border-bottom:1px solid var(--line);margin-bottom:16px}
.logo{display:flex;align-items:center;gap:10px}
.logo-mark{width:34px;height:34px;border-radius:9px;
  background:linear-gradient(135deg,var(--acc),var(--acc2));
  display:flex;align-items:center;justify-content:center;
  font-size:16px;box-shadow:0 0 16px rgba(79,142,247,.3);flex-shrink:0}
.logo-text{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:600;
  color:var(--t1);letter-spacing:2px}
.logo-sub{font-size:9px;color:var(--t3);letter-spacing:1.5px;margin-top:1px}
.sys-badge{display:flex;align-items:center;gap:5px;background:var(--bg2);
  border:1px solid var(--line);border-radius:6px;padding:5px 10px;
  font-family:'JetBrains Mono',monospace;font-size:8px;color:var(--grn);letter-spacing:1px}
.sys-dot{width:5px;height:5px;border-radius:50%;background:var(--grn);
  box-shadow:0 0 5px var(--grn);animation:blink 2s infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.2}}

/* TABS */
.tabs{display:grid;grid-template-columns:1fr 1fr;background:var(--bg1);
  border:1px solid var(--line);border-radius:10px;overflow:hidden;margin-bottom:18px}
.tab-btn{border:none;background:transparent;padding:11px 8px;cursor:pointer;
  font-family:'JetBrains Mono',monospace;font-size:9px;font-weight:600;
  letter-spacing:1.5px;color:var(--t3);text-transform:uppercase;
  display:flex;align-items:center;justify-content:center;gap:6px;
  position:relative;transition:color .2s}
.tab-btn.on{color:var(--acc);background:var(--bg3)}
.tab-btn.on::after{content:'';position:absolute;bottom:0;left:25%;right:25%;height:2px;
  background:var(--acc);border-radius:2px 2px 0 0;box-shadow:0 0 8px var(--acc)}
.tab-badge{background:var(--red);color:#fff;border-radius:4px;
  font-size:8px;padding:1px 5px;font-weight:700;display:none}
.tab-badge.on{display:inline-block}

/* STEP TRACK */
.track{display:flex;align-items:center;background:var(--bg1);border:1px solid var(--line);
  border-radius:10px;padding:12px 14px;margin-bottom:16px}
.trk-s{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px}
.trk-n{width:26px;height:26px;border-radius:6px;border:1.5px solid var(--line2);
  background:var(--bg1);font-family:'JetBrains Mono',monospace;font-size:10px;
  font-weight:600;color:var(--t3);display:flex;align-items:center;justify-content:center;
  transition:all .25s}
.trk-n.on{border-color:var(--acc);color:var(--acc);background:var(--ag2);
  box-shadow:0 0 10px rgba(79,142,247,.2)}
.trk-n.done{border-color:var(--grn);background:var(--grn);color:#07080B}
.trk-l{font-family:'JetBrains Mono',monospace;font-size:7px;color:var(--t3);
  letter-spacing:.8px;text-transform:uppercase;text-align:center}
.trk-l.on{color:var(--acc)}
.trk-wire{flex:1;height:1px;background:var(--line);margin-bottom:11px;transition:background .3s}
.trk-wire.done{background:var(--grn)}

/* PANELS */
.panel{display:none;animation:rise .25s ease}
.panel.on{display:block}
@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}

.sh{margin-bottom:14px}
.sh-tag{font-family:'JetBrains Mono',monospace;font-size:8px;color:var(--acc);
  letter-spacing:2px;opacity:.7;margin-bottom:3px}
.sh-title{font-size:17px;font-weight:700;color:var(--t1);letter-spacing:-.2px}
.sh-sub{font-size:12px;color:var(--t2);margin-top:4px;line-height:1.6}

/* TYPE GRID */
.tgrid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:14px}
.tcard{background:var(--bg2);border:1.5px solid var(--line);border-radius:11px;
  padding:14px 10px;text-align:center;cursor:pointer;
  transition:all .18s cubic-bezier(.34,1.56,.64,1);position:relative;overflow:hidden}
.tcard::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:var(--acc);transform:scaleX(0);transform-origin:left;transition:transform .2s}
.tcard:active{transform:scale(.93)}
.tcard.on{border-color:var(--acc);background:var(--bg3);box-shadow:0 0 18px var(--ag)}
.tcard.on::after{transform:scaleX(1)}
.tcard .ic{font-size:22px;display:block;margin-bottom:7px}
.tcard .lb{font-size:11px;font-weight:600;color:var(--t2)}
.tcard.on .lb{color:var(--acc)}
.tcard .ds{font-size:9.5px;color:var(--t3);margin-top:2px}

/* FIELDS */
.field{margin-bottom:13px}
.flbl{font-family:'JetBrains Mono',monospace;font-size:8.5px;color:var(--t3);
  letter-spacing:1.8px;text-transform:uppercase;margin-bottom:7px;
  display:flex;align-items:center;gap:7px}
.opt{background:rgba(255,184,48,.1);color:var(--amb);border:1px solid rgba(255,184,48,.2);
  border-radius:3px;font-size:7px;padding:1px 5px;letter-spacing:.5px}
input[type=text],textarea{width:100%;background:var(--bg2);border:1.5px solid var(--line);
  border-radius:9px;padding:11px 13px;color:var(--t1);
  font-family:'Inter',sans-serif;font-size:13px;outline:none;
  transition:border-color .2s,box-shadow .2s;line-height:1.5}
input[type=text]:focus,textarea:focus{border-color:var(--acc);box-shadow:0 0 0 3px var(--ag2)}
input::placeholder,textarea::placeholder{color:var(--t3)}
textarea{resize:none;min-height:80px}
.cc{font-family:'JetBrains Mono',monospace;font-size:9px;color:var(--t3);
  text-align:right;margin-top:4px}

/* CHIPS */
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:13px}
.chip{background:var(--bg2);border:1.5px solid var(--line);border-radius:7px;
  padding:7px 11px;font-size:11.5px;font-weight:500;color:var(--t2);
  cursor:pointer;transition:all .15s;user-select:none;white-space:nowrap}
.chip:active{transform:scale(.92)}
.chip.on{border-color:var(--acc);background:var(--ag);color:var(--acc);font-weight:600}

/* FILE UPLOAD */
.upzone{border:1.5px dashed var(--line2);border-radius:11px;padding:22px;
  text-align:center;cursor:pointer;background:var(--bg2);transition:all .2s}
.upzone:hover,.upzone.has{border-color:var(--acc);border-style:solid;background:var(--bg3)}
.up-ic{font-size:26px;margin-bottom:6px}
.up-t{font-size:12px;font-weight:600;color:var(--t2);margin-bottom:3px}
.up-s{font-size:10px;color:var(--t3)}
.fprev{display:none;align-items:center;gap:10px;background:var(--bg3);
  border:1px solid var(--line2);border-radius:10px;padding:10px 12px;margin-top:8px}
.fprev.on{display:flex}
.fthumb{width:36px;height:36px;object-fit:cover;border-radius:6px;display:none}
.fico{width:36px;height:36px;border-radius:6px;background:var(--ag);
  border:1px solid rgba(79,142,247,.2);display:flex;align-items:center;
  justify-content:center;font-size:15px;flex-shrink:0}
.finfo{flex:1;overflow:hidden}
.fname{font-size:12px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fsize{font-family:'JetBrains Mono',monospace;font-size:9px;color:var(--t3);margin-top:2px}
.frm{cursor:pointer;font-size:14px;color:var(--t3);padding:4px;transition:color .2s}
.frm:hover{color:var(--red)}
.dvd{position:relative;height:1px;background:var(--line);margin:14px 0}
.dvd::after{content:'OR';position:absolute;top:-7px;left:50%;transform:translateX(-50%);
  background:var(--bg0);padding:0 8px;font-family:'JetBrains Mono',monospace;
  font-size:8px;color:var(--t4);letter-spacing:2px}

/* SUMMARY */
.sum{background:var(--bg1);border:1px solid var(--line);border-radius:10px;
  overflow:hidden;margin-bottom:13px}
.sum-row{display:grid;grid-template-columns:90px 1fr;min-height:38px;
  border-bottom:1px solid var(--line)}
.sum-row:last-child{border-bottom:none}
.sum-k{background:var(--bg2);padding:10px 12px;font-family:'JetBrains Mono',monospace;
  font-size:8px;color:var(--t3);letter-spacing:1px;text-transform:uppercase;
  display:flex;align-items:center;border-right:1px solid var(--line)}
.sum-v{padding:10px 12px;font-size:12px;color:var(--t1);display:flex;align-items:center;line-height:1.4}

.warn{background:var(--rg);border:1px solid rgba(247,79,106,.2);
  border-radius:9px;padding:10px 13px;display:flex;gap:8px;margin-bottom:13px}
.warn .wi{font-size:13px;flex-shrink:0;margin-top:1px}
.warn p{font-size:11px;color:rgba(247,79,106,.85);line-height:1.55}

/* BUTTONS */
.btn{width:100%;padding:13px;border-radius:9px;border:none;cursor:pointer;
  font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:600;
  letter-spacing:1px;text-transform:uppercase;
  display:flex;align-items:center;justify-content:center;gap:6px;transition:all .18s}
.btn:active{transform:scale(.97)}
.btn-p{background:var(--acc);color:#fff;box-shadow:0 4px 16px rgba(79,142,247,.3)}
.btn-p:hover{box-shadow:0 6px 22px rgba(79,142,247,.4)}
.btn-p:disabled{opacity:.3;cursor:not-allowed;box-shadow:none;transform:none}
.btn-g{background:transparent;border:1.5px solid var(--line2);color:var(--t3)}
.btn-g:hover{border-color:var(--t2);color:var(--t1)}
.btn-r{background:var(--red);color:#fff;box-shadow:0 4px 16px var(--rg)}
.btn-r:hover{box-shadow:0 6px 22px rgba(247,79,106,.35)}
.btn-r:disabled{opacity:.35;cursor:not-allowed;box-shadow:none;transform:none}
.brow{display:flex;gap:8px}
.brow .btn{flex:1}

/* MY REPORTS */
.empty{text-align:center;padding:48px 20px}
.empty-i{font-size:38px;opacity:.3;margin-bottom:12px}
.empty-t{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--t3);
  letter-spacing:2px;text-transform:uppercase}
.empty-s{font-size:11px;color:var(--t3);margin-top:6px;line-height:1.6}
.rcard{background:var(--bg1);border:1px solid var(--line);border-radius:10px;
  padding:12px 14px;margin-bottom:8px;position:relative;overflow:hidden}
.rcard::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;border-radius:3px 0 0 3px}
.rcard.s0::before{background:var(--amb)}
.rcard.s1::before{background:var(--grn)}
.rcard.s2::before{background:var(--red)}
.rc-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.rc-id{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:600;color:var(--acc)}
.rc-st{font-family:'JetBrains Mono',monospace;font-size:8px;font-weight:600;
  padding:3px 8px;border-radius:4px;letter-spacing:.8px;text-transform:uppercase}
.rc-st.s0{background:rgba(255,184,48,.1);color:var(--amb);border:1px solid rgba(255,184,48,.2)}
.rc-st.s1{background:var(--gg);color:var(--grn);border:1px solid rgba(61,255,160,.2)}
.rc-st.s2{background:var(--rg);color:var(--red);border:1px solid rgba(247,79,106,.2)}
.rc-body{display:grid;grid-template-columns:1fr 1fr;gap:7px}
.rc-f .rk{font-family:'JetBrains Mono',monospace;font-size:7px;color:var(--t3);
  letter-spacing:1px;text-transform:uppercase;margin-bottom:2px}
.rc-f .rv{font-size:11.5px;color:var(--t1)}
.rc-dt{font-family:'JetBrains Mono',monospace;font-size:8.5px;color:var(--t3);
  margin-top:8px;padding-top:8px;border-top:1px solid var(--line)}

/* SUCCESS */
.succ{text-align:center;padding:32px 16px}
.succ-ring{width:64px;height:64px;border-radius:50%;margin:0 auto 14px;
  background:var(--gg);border:2px solid var(--grn);
  display:flex;align-items:center;justify-content:center;font-size:28px;
  box-shadow:0 0 28px rgba(61,255,160,.2);
  animation:pop .5s cubic-bezier(.34,1.56,.64,1)}
@keyframes pop{from{transform:scale(.2);opacity:0}to{transform:scale(1);opacity:1}}
.succ-t{font-family:'JetBrains Mono',monospace;font-size:16px;font-weight:600;
  color:var(--grn);letter-spacing:2px;margin-bottom:8px}
.succ-s{font-size:12px;color:var(--t2);line-height:1.65}
.succ-tid{display:inline-block;background:var(--bg2);border:1px solid var(--acc);
  border-radius:8px;padding:9px 18px;margin:14px auto;
  font-family:'JetBrains Mono',monospace;font-size:15px;font-weight:600;
  color:var(--acc);letter-spacing:3px;box-shadow:0 0 14px var(--ag2)}
.succ-note{font-family:'JetBrains Mono',monospace;font-size:8px;color:var(--t3);letter-spacing:.8px}

/* TOAST */
.toast{position:fixed;bottom:16px;left:50%;
  transform:translateX(-50%) translateY(80px);
  background:var(--bg3);border:1px solid var(--line2);border-radius:8px;
  padding:9px 18px;font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:600;
  letter-spacing:.5px;white-space:nowrap;z-index:9990;
  transition:transform .28s cubic-bezier(.34,1.56,.64,1);
  box-shadow:0 8px 28px rgba(0,0,0,.6)}
.toast.show{transform:translateX(-50%) translateY(0)}
.toast.err{border-color:var(--red);color:var(--red)}
.toast.ok{border-color:var(--grn);color:var(--grn)}
.dots{display:inline-flex;gap:3px;align-items:center}
.dots span{width:4px;height:4px;border-radius:50%;background:currentColor;animation:dt 1.1s infinite}
.dots span:nth-child(2){animation-delay:.18s}
.dots span:nth-child(3){animation-delay:.36s}
@keyframes dt{0%,80%,100%{transform:scale(.4);opacity:.3}40%{transform:scale(1);opacity:1}}
</style>
</head>
<body>

<!-- OFFLINE OVERLAY: internetsizken sayfayi tamamen kapat, URL gozukmesin -->
<div id="net-overlay">
  <div class="no-icon">📡</div>
  <div class="no-t1">BAĞLANTI YOK</div>
  <div class="no-t2">İnternet bağlantısı kesildi.<br>Bağlantı sağlandığında devam edebilirsin.</div>
</div>

<!-- TELEGRAM GUARD -->
<div id="tg-guard">
  <div class="gd-ic">⛔</div>
  <div class="gd-t1">ERİŞİM ENGELİ</div>
  <div class="gd-t2">Bu uygulama yalnızca<br><b>Telegram</b> üzerinden kullanılabilir.</div>
  <div class="gd-ft">// NOT A TELEGRAM CLIENT</div>
</div>

<div class="shell" id="main-shell">

  <div class="topbar">
    <div class="logo">
      <div class="logo-mark">🛡</div>
      <div>
        <div class="logo-text">SAFE TEAM</div>
        <div class="logo-sub">REPORT · v3.2</div>
      </div>
    </div>
    <div class="sys-badge"><div class="sys-dot"></div>ONLINE</div>
  </div>

  <div class="tabs">
    <button class="tab-btn on" id="tb-new" onclick="setTab('new')">＋ YENİ ŞİKAYET</button>
    <button class="tab-btn" id="tb-my" onclick="setTab('my')">
      ≡ ŞİKAYETLERİM <span class="tab-badge" id="tbadge">0</span>
    </button>
  </div>

  <div id="view-new">
    <div class="track">
      <div class="trk-s"><div class="trk-n on" id="tn1">1</div><div class="trk-l on" id="tl1">TÜR</div></div>
      <div class="trk-wire" id="tw1"></div>
      <div class="trk-s"><div class="trk-n" id="tn2">2</div><div class="trk-l" id="tl2">SEBEP</div></div>
      <div class="trk-wire" id="tw2"></div>
      <div class="trk-s"><div class="trk-n" id="tn3">3</div><div class="trk-l" id="tl3">KANIT</div></div>
      <div class="trk-wire" id="tw3"></div>
      <div class="trk-s"><div class="trk-n" id="tn4">4</div><div class="trk-l" id="tl4">GÖNDER</div></div>
    </div>

    <!-- STEP 1 -->
    <div class="panel on" id="p1">
      <div class="sh"><div class="sh-tag">// ADIM 01</div><div class="sh-title">Şikayet Türü</div>
        <div class="sh-sub">Hangi tür hesabı raporluyorsun?</div></div>
      <div class="tgrid">
        <div class="tcard" onclick="pickType(this,'kanal')"><span class="ic">📢</span><div class="lb">Kanal</div><div class="ds">Telegram kanalı</div></div>
        <div class="tcard" onclick="pickType(this,'grup')"><span class="ic">👥</span><div class="lb">Grup</div><div class="ds">Telegram grubu</div></div>
        <div class="tcard" onclick="pickType(this,'kullanici')"><span class="ic">👤</span><div class="lb">Kullanıcı</div><div class="ds">Bir hesap</div></div>
        <div class="tcard" onclick="pickType(this,'bot')"><span class="ic">🤖</span><div class="lb">Bot</div><div class="ds">Telegram botu</div></div>
      </div>
      <div class="field" id="tf" style="display:none">
        <div class="flbl" id="tf-lbl">HEDEF</div>
        <input type="text" id="ti" placeholder="@kullaniciadi" oninput="chk1()">
      </div>
      <button class="btn btn-p" id="n1" disabled onclick="go(2)" style="margin-top:6px">DEVAM ET →</button>
    </div>

    <!-- STEP 2 -->
    <div class="panel" id="p2">
      <div class="sh"><div class="sh-tag">// ADIM 02</div><div class="sh-title">Şikayet Sebebi</div>
        <div class="sh-sub">Bir veya birden fazla sebep seçebilirsin.</div></div>
      <div class="chips">
        <div class="chip" onclick="togR(this,'Spam')">🗑 Spam</div>
        <div class="chip" onclick="togR(this,'Dolandırıcılık')">💸 Dolandırıcılık</div>
        <div class="chip" onclick="togR(this,'Hakaret / Küfür')">🤬 Hakaret</div>
        <div class="chip" onclick="togR(this,'Tehdit')">⚠️ Tehdit</div>
        <div class="chip" onclick="togR(this,'Yanıltıcı Bilgi')">❌ Yanıltıcı Bilgi</div>
        <div class="chip" onclick="togR(this,'Telif Hakkı')">©️ Telif Hakkı</div>
        <div class="chip" onclick="togR(this,'Gizlilik İhlali')">🔒 Gizlilik</div>
        <div class="chip" onclick="togR(this,'Diğer')">📌 Diğer</div>
      </div>
      <div class="field">
        <div class="flbl">AÇIKLAMA <span class="opt">OPSİYONEL</span></div>
        <textarea id="rd" placeholder="Durumu kısaca anlat..." maxlength="500"
          oninput="document.getElementById('cc').textContent=this.value.length"></textarea>
        <div class="cc"><span id="cc">0</span>/500</div>
      </div>
      <div class="brow">
        <button class="btn btn-g" onclick="go(1)">← GERİ</button>
        <button class="btn btn-p" id="n2" disabled onclick="go(3)">DEVAM →</button>
      </div>
    </div>

    <!-- STEP 3 -->
    <div class="panel" id="p3">
      <div class="sh"><div class="sh-tag">// ADIM 03</div>
        <div class="sh-title">Kanıt <span style="font-size:11px;font-weight:400;color:var(--t3)">(opsiyonel)</span></div>
        <div class="sh-sub">Dosya veya metin kanıt ekle. Kanıt yoksa atlayabilirsin.</div></div>
      <div class="upzone" id="upz" onclick="document.getElementById('fi').click()">
        <input type="file" id="fi" accept="image/*,.pdf,.txt" style="display:none" onchange="pickFile(this)">
        <div class="up-ic">📎</div>
        <div class="up-t">Dosya Seç</div>
        <div class="up-s">Görsel · PDF · Metin &nbsp;|&nbsp; Maks 10 MB</div>
      </div>
      <div class="fprev" id="fprev">
        <img id="fimg" class="fthumb" src="" alt="">
        <div class="fico" id="fico">📄</div>
        <div class="finfo"><div class="fname" id="fn">—</div><div class="fsize" id="fs">—</div></div>
        <div class="frm" onclick="clearFile()">✕</div>
      </div>
      <div class="dvd"></div>
      <div class="field">
        <div class="flbl">METİN KANIT <span class="opt">OPSİYONEL</span></div>
        <textarea id="te" placeholder="Link, tarih/saat, mesaj metni..." style="min-height:70px" maxlength="1000"></textarea>
      </div>
      <div class="brow">
        <button class="btn btn-g" onclick="go(2)">← GERİ</button>
        <button class="btn btn-p" onclick="go(4)">DEVAM →</button>
      </div>
    </div>

    <!-- STEP 4 -->
    <div class="panel" id="p4">
      <div class="sh"><div class="sh-tag">// ADIM 04</div><div class="sh-title">Gözden Geçir &amp; Gönder</div>
        <div class="sh-sub">Bilgileri kontrol et, ardından raporu gönder.</div></div>
      <div class="sum">
        <div class="sum-row"><div class="sum-k">TÜR</div><div class="sum-v" id="sv-type">—</div></div>
        <div class="sum-row"><div class="sum-k">HEDEF</div><div class="sum-v" id="sv-target">—</div></div>
        <div class="sum-row"><div class="sum-k">SEBEPLER</div><div class="sum-v" id="sv-reasons">—</div></div>
        <div class="sum-row"><div class="sum-k">AÇIKLAMA</div><div class="sum-v" id="sv-detail">—</div></div>
        <div class="sum-row"><div class="sum-k">KANIT</div><div class="sum-v" id="sv-evid">—</div></div>
      </div>
      <div class="warn"><div class="wi">⚠️</div>
        <p>Asılsız şikayetler hesabınızı olumsuz etkileyebilir. Yalnızca gerçek ihlalleri raporlayın.</p></div>
      <div class="brow">
        <button class="btn btn-g" onclick="go(3)">← GERİ</button>
        <button class="btn btn-r" id="sbtn" onclick="doSubmit()"><span id="stxt">🚨 RAPORU GÖNDER</span></button>
      </div>
    </div>

    <!-- SUCCESS -->
    <div class="panel" id="p-ok">
      <div class="succ">
        <div class="succ-ring">✅</div>
        <div class="succ-t">RAPOR GÖNDERİLDİ</div>
        <div class="succ-s">Şikayetin yetkililere iletildi.<br>Onay/ret durumunu aşağıdan takip edebilirsin.</div>
        <div class="succ-tid" id="s-tid">SAFE-XXXXX</div>
        <div class="succ-note">// ŞİKAYETLERİM SEKMESİNDEN TAKİP EDEBİLİRSİN</div>
        <button class="btn btn-g" style="max-width:190px;margin:18px auto 0;font-size:9px" onclick="resetForm()">
          + YENİ RAPOR
        </button>
      </div>
    </div>
  </div>

  <div id="view-my" style="display:none"><div id="mr-list"></div></div>
</div>

<div class="toast" id="toast"></div>

<script>
/* ─────────────────────────────────────
   1. TELEGRAM GUARD
───────────────────────────────────── */
const tg = window.Telegram?.WebApp;
const hasTg = !!(tg && tg.initData && tg.initData.length > 0);

if (!hasTg) {
  document.getElementById('tg-guard').classList.add('show');
  document.getElementById('main-shell').style.display = 'none';
  throw new Error('NOT_TELEGRAM');
}
tg.ready();
tg.expand();

/* ─────────────────────────────────────
   2. OFFLINE OVERLAY
   Sorun: internet yokken sayfa yenilenince
   tarayici PUBLIC_URL'i gosterebiliyor.
   Cozum: tam ekran overlay ile URL'yi ort.
───────────────────────────────────── */
const netOverlay = document.getElementById('net-overlay');

function applyNet(online) {
  if (online) {
    netOverlay.classList.remove('show');
  } else {
    netOverlay.classList.add('show');
  }
}

window.addEventListener('online',  () => applyNet(true));
window.addEventListener('offline', () => applyNet(false));
// Sayfa yuklenince kontrol
if (!navigator.onLine) applyNet(false);

/* ─────────────────────────────────────
   3. KULLANICI + AKTiViTE LOG
───────────────────────────────────── */
const USER = {
  id:       String(tg.initDataUnsafe?.user?.id || ''),
  username: tg.initDataUnsafe?.user?.username
            || tg.initDataUnsafe?.user?.first_name
            || 'Bilinmiyor',
  initData: tg.initData || '',
};

// Her aksiyonu sunucuya gonder (IP sunucu tarafinda alinir)
async function trackEvent(event, detail='') {
  if (!navigator.onLine) return;
  try {
    await fetch('/track', {
      method: 'POST',
      headers: {'Content-Type':'application/json','X-Tg-Init':'1'},
      body: JSON.stringify({
        event, detail,
        user_id:   USER.id,
        username:  USER.username,
        init_data: USER.initData,
        ts_local:  new Date().toISOString(),
        page:      location.pathname,
      }),
    });
  } catch(_) {}
}

// Tum sayfa tiklamalari/girislerini yakala
document.addEventListener('click', e => {
  const el  = e.target.closest('[data-ev]') || e.target;
  const tag  = el.tagName;
  const txt  = (el.textContent||'').trim().slice(0,40);
  trackEvent('UI_CLICK', `${tag}: ${txt}`);
});

// Input odaklanmalarini logla
document.addEventListener('focusin', e => {
  if (e.target.id) trackEvent('INPUT_FOCUS', e.target.id);
});

// Sayfa gorunurlugu degisince logla (kapat/ac)
document.addEventListener('visibilitychange', () => {
  trackEvent('VISIBILITY', document.visibilityState);
});

// Ilk acilis
trackEvent('APP_OPEN', 'Mini App acildi');

/* ─────────────────────────────────────
   4. STATE
───────────────────────────────────── */
const S = {
  type:null, reasons:[], detail:'', tevid:'',
  fileB64:null, fileMime:'', fileName:'',
  step:1,
  myR: JSON.parse(localStorage.getItem('str_reports')||'[]'),
};

/* ─────────────────────────────────────
   TABS
───────────────────────────────────── */
function setTab(t) {
  const n = t==='new';
  document.getElementById('view-new').style.display = n?'block':'none';
  document.getElementById('view-my').style.display  = n?'none':'block';
  document.getElementById('tb-new').classList.toggle('on', n);
  document.getElementById('tb-my').classList.toggle('on', !n);
  trackEvent('TAB', t);
  if (!n) { renderMyR(); syncStatus(); }
}

/* ─────────────────────────────────────
   STEP TRACK
───────────────────────────────────── */
function updTrack(a) {
  for (let i=1;i<=4;i++) {
    const n=document.getElementById('tn'+i), l=document.getElementById('tl'+i);
    n.classList.remove('on','done'); l.classList.remove('on');
    if (i<a)      { n.classList.add('done'); n.textContent='✓'; }
    else if(i===a){ n.classList.add('on');   n.textContent=i; l.classList.add('on'); }
    else          { n.textContent=i; }
    if (i<=3) document.getElementById('tw'+i).classList.toggle('done',i<a);
  }
}

/* ─────────────────────────────────────
   NAVIGATION
───────────────────────────────────── */
function hideAll(){['p1','p2','p3','p4','p-ok'].forEach(id=>document.getElementById(id)?.classList.remove('on'));}
function go(n) {
  if (n>S.step && !canGo(S.step)) return;
  hideAll();
  if (n<=4) { document.getElementById('p'+n).classList.add('on'); updTrack(n); }
  S.step=n;
  window.scrollTo({top:0,behavior:'smooth'});
  if (n===4) buildSum();
  trackEvent('STEP',String(n));
}
function canGo(s) {
  if (s===1){
    if (!S.type){toast('Şikayet türü seç','err');return false;}
    if (!document.getElementById('ti').value.trim()){toast('Hedef girin','err');return false;}
    return true;
  }
  if (s===2){
    if (!S.reasons.length){toast('En az bir sebep seç','err');return false;}
    S.detail=document.getElementById('rd').value.trim(); return true;
  }
  if (s===3){S.tevid=document.getElementById('te').value.trim(); return true;}
  return true;
}

/* STEP 1 */
const PH={kanal:'@kanaladi veya t.me/kanal',grup:'@grupadi veya t.me/grup',kullanici:'@kullaniciadi',bot:'@botadi'};
const LB={kanal:'KANAL / LİNK',grup:'GRUP / LİNK',kullanici:'KULLANICI ADI',bot:'BOT ADI'};
function pickType(el,t){
  document.querySelectorAll('.tcard').forEach(c=>c.classList.remove('on'));
  el.classList.add('on'); S.type=t;
  document.getElementById('tf-lbl').textContent=LB[t];
  document.getElementById('ti').placeholder=PH[t];
  document.getElementById('tf').style.display='block';
  chk1(); trackEvent('TYPE',t);
}
function chk1(){document.getElementById('n1').disabled=!(S.type&&document.getElementById('ti').value.trim().length>1);}

/* STEP 2 */
function togR(el,r){
  el.classList.toggle('on');
  if(el.classList.contains('on')){if(!S.reasons.includes(r))S.reasons.push(r);}
  else S.reasons=S.reasons.filter(x=>x!==r);
  document.getElementById('n2').disabled=!S.reasons.length;
}

/* ─────────────────────────────────────
   STEP 3 — BASE64 FILE
   FormData/multipart kullanimiyoruz.
   Bunun yerine FileReader ile base64
   alip JSON icinde gondeririz.
   Bu yontem 400 hatasini tamamen cozer.
───────────────────────────────────── */
function pickFile(inp) {
  const f=inp.files[0]; if(!f) return;
  if(f.size>10*1024*1024){toast('10 MB siniri asildi','err');inp.value='';return;}
  document.getElementById('fn').textContent=f.name;
  document.getElementById('fs').textContent=fmtB(f.size);
  document.getElementById('fprev').classList.add('on');
  document.getElementById('upz').classList.add('has');

  const r=new FileReader();
  r.onload=e=>{
    const full=e.target.result;
    S.fileB64 =full.split(',')[1];   // sadece base64 kismi
    S.fileMime=f.type||'application/octet-stream';
    S.fileName=f.name;
    const img=document.getElementById('fimg'), ico=document.getElementById('fico');
    if(f.type.startsWith('image/')){img.src=full;img.style.display='block';ico.style.display='none';}
    else{img.style.display='none';ico.style.display='flex';ico.textContent=f.type==='application/pdf'?'📄':'📝';}
  };
  r.onerror=()=>{toast('Dosya okunamadi','err');clearFile();};
  r.readAsDataURL(f);
  trackEvent('FILE',f.name);
}
function clearFile(){
  S.fileB64=null;S.fileMime='';S.fileName='';
  document.getElementById('fi').value='';
  document.getElementById('fprev').classList.remove('on');
  document.getElementById('upz').classList.remove('has');
  const img=document.getElementById('fimg');
  img.src='';img.style.display='none';
  document.getElementById('fico').style.display='flex';
}
function fmtB(b){return b<1024?b+' B':b<1048576?(b/1024).toFixed(1)+' KB':(b/1048576).toFixed(1)+' MB';}

/* STEP 4 */
const TE={kanal:'📢',grup:'👥',kullanici:'👤',bot:'🤖'};
function buildSum(){
  document.getElementById('sv-type').textContent   =(TE[S.type]||'')+' '+S.type;
  document.getElementById('sv-target').textContent =document.getElementById('ti').value.trim();
  document.getElementById('sv-reasons').textContent=S.reasons.join(' · ')||'—';
  document.getElementById('sv-detail').textContent =S.detail||'Belirtilmedi';
  const ev=[];
  if(S.fileName) ev.push('📎 '+S.fileName);
  if(document.getElementById('te').value.trim()) ev.push('💬 Metin kanit');
  document.getElementById('sv-evid').textContent=ev.length?ev.join(' · '):'Eklenmedi';
}

/* ─────────────────────────────────────
   SUBMIT — JSON + base64
───────────────────────────────────── */
async function doSubmit(){
  // Dosya secildi ama henuz okunmadi mi?
  if(document.getElementById('fi').files[0] && !S.fileB64){
    toast('Dosya hazirlaniyor, bekle...','err'); return;
  }
  const btn=document.getElementById('sbtn'), txt=document.getElementById('stxt');
  btn.disabled=true;
  txt.innerHTML='<span class="dots"><span></span><span></span><span></span></span>';

  const payload={
    type:         S.type||'',
    target:       document.getElementById('ti').value.trim(),
    reasons:      S.reasons.join(', '),
    detail:       S.detail||'',
    text_evidence:document.getElementById('te').value.trim(),
    user_id:      USER.id,
    username:     USER.username,
    init_data:    USER.initData,
    file_b64:     S.fileB64||null,
    file_mime:    S.fileMime||null,
    file_name:    S.fileName||null,
  };

  try{
    const res=await fetch('/submit',{
      method:'POST',
      headers:{'Content-Type':'application/json','X-Tg-Init':'1'},
      body:JSON.stringify(payload),
    });
    if(!res.ok){
      const t=await res.text();
      throw new Error('HTTP '+res.status+': '+t.slice(0,150));
    }
    const data=await res.json();
    if(!data.ok) throw new Error(data.error||'Sunucu hatasi');

    S.myR.unshift({ticket_id:data.ticket_id,type:S.type,
      target:payload.target,reasons:S.reasons.join(', '),
      detail:S.detail,status:'bekliyor',created_at:new Date().toLocaleString('tr-TR')});
    localStorage.setItem('str_reports',JSON.stringify(S.myR));
    updBadge();
    hideAll();
    document.getElementById('p-ok').classList.add('on');
    document.getElementById('s-tid').textContent=data.ticket_id;
    updTrack(5);
    toast('Rapor gonderildi ✅','ok');
    if(tg.HapticFeedback) tg.HapticFeedback.notificationOccurred('success');
    window.scrollTo({top:0,behavior:'smooth'});
    trackEvent('SUBMIT_OK',data.ticket_id);
  }catch(e){
    btn.disabled=false;
    txt.innerHTML='🚨 RAPORU GÖNDER';
    toast('Hata: '+e.message,'err');
    console.error('[SUBMIT]',e);
    trackEvent('SUBMIT_ERR',e.message);
  }
}

/* RESET */
function resetForm(){
  S.type=null;S.reasons=[];S.detail='';S.tevid='';
  S.fileB64=null;S.fileMime='';S.fileName='';S.step=1;
  document.querySelectorAll('.tcard').forEach(c=>c.classList.remove('on'));
  document.querySelectorAll('.chip').forEach(c=>c.classList.remove('on'));
  ['ti','rd','te'].forEach(id=>{document.getElementById(id).value='';});
  document.getElementById('cc').textContent='0';
  document.getElementById('tf').style.display='none';
  document.getElementById('n1').disabled=true;
  document.getElementById('n2').disabled=true;
  document.getElementById('sbtn').disabled=false;
  document.getElementById('stxt').innerHTML='🚨 RAPORU GÖNDER';
  clearFile();hideAll();
  document.getElementById('p1').classList.add('on');
  updTrack(1);setTab('new');
  window.scrollTo({top:0,behavior:'smooth'});
}

/* MY REPORTS */
function updBadge(){
  const n=S.myR.length, b=document.getElementById('tbadge');
  b.textContent=n; b.classList.toggle('on',n>0);
}
const stMap={bekliyor:{c:'s0',l:'⏳ BEKLİYOR'},onaylandi:{c:'s1',l:'✅ ONAYLANDI'},reddedildi:{c:'s2',l:'❌ REDDEDİLDİ'}};
function renderMyR(){
  const c=document.getElementById('mr-list');
  if(!S.myR.length){
    c.innerHTML='<div class="empty"><div class="empty-i">📋</div><div class="empty-t">RAPOR YOK</div><div class="empty-s">Oluşturduğun raporlar burada görünecek</div></div>';
    return;
  }
  c.innerHTML=S.myR.map(r=>{
    const st=stMap[r.status]||stMap.bekliyor;
    return`<div class="rcard ${st.c}">
      <div class="rc-top"><div class="rc-id">${r.ticket_id}</div><div class="rc-st ${st.c}">${st.l}</div></div>
      <div class="rc-body">
        <div class="rc-f"><div class="rk">TÜR</div><div class="rv">${(TE[r.type]||'')} ${r.type||'—'}</div></div>
        <div class="rc-f"><div class="rk">HEDEF</div><div class="rv">${r.target||'—'}</div></div>
        <div class="rc-f" style="grid-column:1/-1"><div class="rk">SEBEPLER</div><div class="rv">${r.reasons||'—'}</div></div>
      </div>
      <div class="rc-dt">// ${r.created_at}</div>
    </div>`;
  }).join('');
}
async function syncStatus(){
  if(!S.myR.length||!navigator.onLine) return;
  try{
    const res=await fetch('/status?ids='+encodeURIComponent(S.myR.map(r=>r.ticket_id).join(',')),
      {headers:{'X-Tg-Init':'1'}});
    const d=await res.json();
    if(d.statuses){
      let ch=false;
      S.myR.forEach(r=>{if(d.statuses[r.ticket_id]&&d.statuses[r.ticket_id]!==r.status){r.status=d.statuses[r.ticket_id];ch=true;}});
      if(ch){localStorage.setItem('str_reports',JSON.stringify(S.myR));renderMyR();}
    }
  }catch(_){}
}

/* TOAST */
let _tt;
function toast(msg,t=''){
  const el=document.getElementById('toast');
  el.textContent=msg; el.className='toast show '+(t||'');
  clearTimeout(_tt); _tt=setTimeout(()=>el.classList.remove('show'),2800);
}

/* INIT */
document.getElementById('tf').style.display='none';
updBadge(); syncStatus();
setInterval(syncStatus,30000);
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════
_TG_UA = ("telegram", "tgwebapp", "tgios", "tgandroid")

def _is_tg() -> bool:
    ua = request.headers.get("User-Agent", "").lower()
    return any(k in ua for k in _TG_UA) or bool(request.headers.get("X-Tg-Init"))


# ══════════════════════════════════════════════════════════════════
#  FLASK ROUTES
# ══════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    ip = get_ip()
    ua = request.headers.get("User-Agent", "")
    if not _is_tg():
        log_event("ACCESS_DENIED", ip, detail=ua[:80])
        return ACCESS_DENIED_HTML, 403
    log_event("APP_OPEN", ip, detail=ua[:60])
    return render_template_string(MINI_APP_HTML)


@app.route("/track", methods=["POST"])
def track():
    """Client-side olayları sunucu tarafında logla (IP burada alinir)."""
    ip = get_ip()
    try:
        d = request.get_json(silent=True) or {}
        log_event(
            d.get("event", "CLIENT_EVENT"), ip,
            uid   = d.get("user_id", ""),
            uname = d.get("username", ""),
            detail= d.get("detail", "")[:200],
            extra = {"page": d.get("page",""), "ts_client": d.get("ts_local","")}
        )
    except Exception:
        pass
    return jsonify({"ok": True})


@app.route("/submit", methods=["POST"])
def submit_report():
    ip = get_ip()
    blocked, reason = rate_check(ip)
    if blocked:
        log_event("BLOCKED", ip, detail=reason)
        return jsonify({"ok": False, "error": "Erisim engellendi."}), (403 if reason=="banned" else 429)

    try:
        d = request.get_json(silent=True)
        if not d:
            return jsonify({"ok": False, "error": "JSON bekleniyor"}), 400

        rtype    = d.get("type",          "bilinmiyor")
        target   = d.get("target",        "—")
        reasons  = d.get("reasons",       "—")
        detail   = d.get("detail",        "").strip()
        tevid    = d.get("text_evidence", "").strip()
        uid      = d.get("user_id",       "bilinmiyor")
        uname    = d.get("username",      "Bilinmiyor")
        file_b64 = d.get("file_b64")
        file_mime= d.get("file_mime",     "application/octet-stream")
        file_name= d.get("file_name",     "dosya")

        short     = str(uuid.uuid4())[:5].upper()
        ticket_id = f"SAFE-{short}"
        ts        = datetime.now().strftime("%d.%m.%Y %H:%M")

        reports[ticket_id] = dict(
            ticket_id=ticket_id, type=rtype, target=target, reasons=reasons,
            detail=detail, text_evidence=tevid, user_id=uid, username=uname,
            ip=ip, status="bekliyor", created_at=ts,
        )

        log_event("REPORT_SUBMIT", ip, uid=uid, uname=uname,
                  detail=f"{rtype} → {target}",
                  extra={"ticket_id": ticket_id, "has_file": bool(file_b64)})

        ico = {"kanal":"📢","grup":"👥","kullanici":"👤","bot":"🤖"}.get(rtype,"❓")
        msg = (
            f"🛡 *SAFE TEAM REPORT*\n━━━━━━━━━━━━━━━━━━\n"
            f"🎫 `{ticket_id}`  ·  🕐 _{ts}_\n\n"
            f"👤 *Sikayetci:* {uname} (ID: `{uid}`)\n"
            f"🌐 *IP:* `{ip}`\n"
            f"{ico} *Tur:* {rtype.upper()}\n"
            f"🔗 *Hedef:* `{target}`\n"
            f"📋 *Sebepler:* {reasons}\n"
        )
        if detail:    msg += f"💬 *Aciklama:* {detail}\n"
        if tevid:     msg += f"📝 *Metin Kanit:* {tevid[:300]}\n"
        if file_b64:  msg += f"📎 *Dosya:* {file_name}\n"

        kb = InlineKeyboardMarkup()
        kb.row(
            InlineKeyboardButton("✅  Onayla", callback_data=f"onay_{ticket_id}"),
            InlineKeyboardButton("❌  Reddet", callback_data=f"ret_{ticket_id}"),
        )
        bot.send_message(ADMIN_ID, msg, parse_mode="Markdown", reply_markup=kb)

        # ── DOSYA GONDERIMI (base64 → BytesIO) ──
        if file_b64 and file_name:
            try:
                raw    = base64.b64decode(file_b64)
                stream = BytesIO(raw)
                stream.name = file_name   # telebot bu alani kullanir
                cap    = f"📎 Kanit — `{ticket_id}`"
                img_ext= (".jpg",".jpeg",".png",".gif",".webp",".bmp",".heic")
                if any(file_name.lower().endswith(x) for x in img_ext):
                    bot.send_photo(ADMIN_ID, stream, caption=cap, parse_mode="Markdown")
                else:
                    bot.send_document(ADMIN_ID, stream, caption=cap,
                                      parse_mode="Markdown",
                                      visible_file_name=file_name)
                log.info(f"[DOSYA] {file_name} gonderildi ({len(raw)//1024}KB)")
            except Exception as fe:
                log.warning(f"[DOSYA HATA] {fe}")

        log.info(f"[SIKAYET] {ticket_id} {uname}({ip}) {rtype}:{target}")
        return jsonify({"ok": True, "ticket_id": ticket_id})

    except Exception as e:
        log.error(f"[SUBMIT HATA] {e}", exc_info=True)
        log_event("ERROR", ip, detail=str(e)[:200])
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/status")
def get_status():
    ip  = get_ip()
    ids = [i.strip() for i in request.args.get("ids","").split(",") if i.strip()]
    log_event("STATUS_CHECK", ip, detail=f"{len(ids)} ticket")
    return jsonify({"statuses": {tid: reports[tid]["status"] for tid in ids if tid in reports}})


@app.route("/admin/log")
def admin_log():
    ip = get_ip()
    if ip not in ("127.0.0.1","::1"):
        return jsonify({"error":"Yetkisiz"}), 403
    limit = min(int(request.args.get("limit",100)), 500)
    uid   = request.args.get("uid")
    if uid:
        p = _user_log(uid)
        if not os.path.exists(p):
            return jsonify({"error":"Bulunamadi"}), 404
        with open(p, encoding="utf-8") as f:
            recs = [json.loads(l) for l in f if l.strip()]
        return jsonify({"uid":uid,"count":len(recs),"records":recs[-limit:]})
    return jsonify({"ram":len(activity_log),"banned":list(banned_ips),
                    "dir":LOGS_DIR,"records":list(activity_log)[:limit]})


@app.route("/admin/unban", methods=["POST"])
def admin_unban():
    if get_ip() not in ("127.0.0.1","::1"):
        return jsonify({"error":"Yetkisiz"}), 403
    tip = (request.json or {}).get("ip","")
    banned_ips.discard(tip); _rate_windows.pop(tip,None)
    return jsonify({"ok":True,"unbanned":tip})


# ══════════════════════════════════════════════════════════════════
#  BOT HANDLERS
# ══════════════════════════════════════════════════════════════════

@bot.message_handler(commands=["start"])
def cmd_start(m):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🛡 SAFE TEAM REPORT", web_app=WebAppInfo(url=f"{PUBLIC_URL}/")))
    bot.send_message(m.chat.id,
        "🛡 *SAFE TEAM REPORT'a Hoş Geldin!*\n\n"
        "Telegram'daki kanal, grup, kullanıcı veya botları güvenli şekilde raporla.\n\n"
        "🔒 Şikayetin gizlidir ve yetkili tarafından incelenir.\n"
        "✅ Onay / ❌ ret kararı sana iletilir.",
        parse_mode="Markdown", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith(("onay_","ret_")))
def cb_handler(call):
    action, tid = call.data.split("_",1)
    rep = reports.get(tid)
    if not rep:
        bot.answer_callback_query(call.id,"⚠️ Bulunamadi"); return
    admin = call.from_user.username or call.from_user.first_name or "Yetkili"
    ts    = datetime.now().strftime("%d.%m.%Y %H:%M")
    if action=="onay":
        rep["status"]="onaylandi"; stxt,atxt="✅ *ONAYLANDI*","✅ Onaylandi"
    else:
        rep["status"]="reddedildi"; stxt,atxt="❌ *REDDEDILDI*","❌ Reddedildi"
    try:
        bot.edit_message_text(
            (call.message.text or "")+f"\n━━━━━━━━━━━━━━━━━━\n{stxt}\n👤 _{admin}_ · 🕐 _{ts}_",
            call.message.chat.id, call.message.message_id, parse_mode="Markdown")
    except Exception: pass
    bot.answer_callback_query(call.id, atxt)
    uid = rep.get("user_id")
    if uid and str(uid).isdigit():
        try:
            txt = (f"🛡 *SAFE TEAM REPORT*\n\n✅ *Sikayetiniz Onaylandi*\n\nTicket: `{tid}`\nHedef: `{rep['target']}`\n\nTesekkurler."
                   if action=="onay" else
                   f"🛡 *SAFE TEAM REPORT*\n\n❌ *Sikayetiniz Reddedildi*\n\nTicket: `{tid}`\nHedef: `{rep['target']}`\n\nYeterli delil bulunamadi.")
            bot.send_message(int(uid), txt, parse_mode="Markdown")
        except Exception as e: log.warning(f"[BILDIRIM] {e}")


@bot.message_handler(commands=["raporlar"])
def cmd_raporlar(m):
    if m.from_user.id!=ADMIN_ID: return
    total=len(reports); bkl=sum(1 for r in reports.values() if r["status"]=="bekliyor")
    onay=sum(1 for r in reports.values() if r["status"]=="onaylandi")
    ret=sum(1 for r in reports.values() if r["status"]=="reddedildi")
    bot.reply_to(m,f"🛡 *Istatistikler*\n\n📝 Toplam: *{total}*\n⏳ Bekliyor: *{bkl}*\n✅ Onaylanan: *{onay}*\n❌ Reddedilen: *{ret}*",parse_mode="Markdown")


@bot.message_handler(commands=["loglar"])
def cmd_loglar(m):
    if m.from_user.id!=ADMIN_ID: return
    if not activity_log: bot.reply_to(m,"Henuz kayit yok."); return
    ICONS={"APP_OPEN":"📱","REPORT_SUBMIT":"📨","STATUS_CHECK":"🔍","BLOCKED":"🚫",
           "ERROR":"❗","ACCESS_DENIED":"⛔","UI_CLICK":"👆","INPUT_FOCUS":"⌨️",
           "VISIBILITY":"👁","TAB":"🔀","STEP":"➡️","TYPE":"🎯","FILE":"📎",
           "SUBMIT_OK":"✅","SUBMIT_ERR":"❌","CLIENT_EVENT":"•","TRACK":"•"}
    lines=[]
    for r in list(activity_log)[:15]:
        u=f"@{r['username']}" if r.get("username") and r["username"]!="Bilinmiyor" else ""
        lines.append(f"{ICONS.get(r['event'],'•')} `{r['ts'][11:]}` `{r['ip']}` {u}\n   _{r['event']}_ {r.get('detail','')[:50]}")
    bot.reply_to(m,f"📋 *Son {len(lines)} Kayit*\n━━━━━━━━\n"+"\n".join(lines)+f"\n━━━━━━━━\n🚫 Engelli: *{len(banned_ips)}*",parse_mode="Markdown")


@bot.message_handler(commands=["banlist"])
def cmd_banlist(m):
    if m.from_user.id!=ADMIN_ID: return
    if not banned_ips: bot.reply_to(m,"✅ Engelli IP yok."); return
    bot.reply_to(m,"*Engelli IP'ler*\n\n"+"\n".join(f"🚫 `{ip}`" for ip in sorted(banned_ips)),parse_mode="Markdown")


@bot.message_handler(commands=["ban"])
def cmd_ban(m):
    if m.from_user.id!=ADMIN_ID: return
    p=m.text.strip().split()
    if len(p)<2: bot.reply_to(m,"`/ban 1.2.3.4`",parse_mode="Markdown"); return
    banned_ips.add(p[1]); bot.reply_to(m,f"🚫 `{p[1]}` engellendi.",parse_mode="Markdown")


@bot.message_handler(commands=["unban"])
def cmd_unban(m):
    if m.from_user.id!=ADMIN_ID: return
    p=m.text.strip().split()
    if len(p)<2: bot.reply_to(m,"`/unban 1.2.3.4`",parse_mode="Markdown"); return
    banned_ips.discard(p[1]); _rate_windows.pop(p[1],None)
    bot.reply_to(m,f"✅ `{p[1]}` engeli kaldirildi.",parse_mode="Markdown")


@bot.message_handler(commands=["ipsorgu"])
def cmd_ipsorgu(m):
    if m.from_user.id!=ADMIN_ID: return
    p=m.text.strip().split()
    if len(p)<2: bot.reply_to(m,"`/ipsorgu 1.2.3.4`",parse_mode="Markdown"); return
    tip=p[1]; recs=[r for r in activity_log if r.get("ip")==tip]
    win=_rate_windows.get(tip,deque()); now=datetime.now()
    r1=sum(1 for t in win if t>now-timedelta(seconds=60))
    r5=sum(1 for t in win if t>now-timedelta(seconds=300))
    if not recs: bot.reply_to(m,f"ℹ️ `{tip}` icin kayit yok.",parse_mode="Markdown"); return
    lines=[f"`{r['ts']}` _{r['event']}_ {r.get('detail','')[:40]}" for r in recs[:10]]
    bot.reply_to(m,
        f"🔍 *IP: `{tip}`*\nDurum: {'🚫 ENGELLİ' if tip in banned_ips else '✅ Serbest'}\n"
        f"1dk: *{r1}* · 5dk: *{r5}* · Toplam: *{len(recs)}*\n\n"+"\n".join(lines),
        parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════

def run_bot():
    log.info("[BOT] Polling baslatiyor...")
    bot.infinity_polling(timeout=30, long_polling_timeout=20)


def setup_menu():
    try:
        bot.set_chat_menu_button(menu_button=MenuButtonWebApp(
            text="🛡 Safe Report", web_app=WebAppInfo(url=f"{PUBLIC_URL}/")))
        log.info("[BOT] Menu butonu ayarlandi")
    except Exception as e:
        log.warning(f"[BOT] Menu: {e}")


if __name__ == "__main__":
    log.info("="*60)
    log.info("  SAFE TEAM REPORT v3.2")
    log.info(f"  URL      : {PUBLIC_URL}")
    log.info(f"  Admin    : {ADMIN_ID}")
    log.info(f"  Logs     : {os.path.abspath(LOGS_DIR)}/")
    log.info(f"             global.jsonl + users/uid_<id>.jsonl")
    log.info(f"  Rate     : {RATE_LIMIT}/{RATE_WINDOW}s  Ban:{BAN_THRESHOLD}/{BAN_WINDOW}s")
    log.info("="*60)
    log.info("  Komutlar: /raporlar /loglar /banlist /ban /unban /ipsorgu")

    setup_menu()
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
