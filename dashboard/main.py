"""Universal Cam Viewer — Management Dashboard (FastAPI + PostgreSQL on Render)

Endpoints:
  POST /heartbeat          — the app sends its report; returns {status, message}
  GET  /?key=ADMIN_KEY     — the HTML dashboard (you)
  POST /action?key=...     — approve / block an installation (called by the dashboard JS)
  GET  /api/installs?key=  — raw JSON (optional)
"""
from __future__ import annotations

import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Any

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

ADMIN_KEY   = os.environ.get("ADMIN_KEY", "change-me")
DATABASE_URL= os.environ.get("DATABASE_URL", "")
NEVER_SEND  = ("password","secret","token","key","auth","cred")  # extra paranoia: reject any field name


# ── DB ─────────────────────────────────────────────────────────────────────────
def _conn():
    url = DATABASE_URL
    if url.startswith("postgres://"):          # psycopg2 needs postgresql://
        url = "postgresql://" + url[len("postgres://"):]
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


def _migrate():
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS installs (
            install_id   TEXT PRIMARY KEY,
            first_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            version      TEXT,
            os           TEXT,
            computer     TEXT,
            device_count INTEGER,
            status       TEXT NOT NULL DEFAULT 'unknown',
            block_msg    TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS devices (
            id           SERIAL PRIMARY KEY,
            install_id   TEXT NOT NULL REFERENCES installs(install_id) ON DELETE CASCADE,
            last_seen    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            name         TEXT, host TEXT, port INTEGER,
            protocol     TEXT, channel INTEGER, is_nvr BOOLEAN
        );
        CREATE INDEX IF NOT EXISTS devices_install ON devices(install_id);
        """)
        c.commit()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _migrate()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


# ── heartbeat ─────────────────────────────────────────────────────────────────
@app.post("/heartbeat")
async def heartbeat(request: Request):
    try:
        data: dict = await request.json()
    except Exception:
        raise HTTPException(400, "bad json")
    install_id = str(data.get("install_id") or "")
    if not install_id:
        raise HTTPException(400, "missing install_id")
    # reject anything that looks like a credential
    for key in data:
        if any(s in key.lower() for s in NEVER_SEND):
            raise HTTPException(400, f"field '{key}' is not accepted")

    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            INSERT INTO installs (install_id, version, os, computer, device_count)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (install_id) DO UPDATE SET
              last_seen=NOW(), version=EXCLUDED.version, os=EXCLUDED.os,
              computer=EXCLUDED.computer, device_count=EXCLUDED.device_count
        """, (install_id, data.get("version"), data.get("os"),
              data.get("computer"), data.get("device_count")))

        cur.execute("DELETE FROM devices WHERE install_id=%s", (install_id,))
        for d in (data.get("devices") or []):
            cur.execute("""INSERT INTO devices
                (install_id,name,host,port,protocol,channel,is_nvr)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                (install_id, d.get("name"), d.get("host"), d.get("port"),
                 d.get("protocol"), d.get("channel"),
                 bool(d.get("is_nvr"))))

        cur.execute("SELECT status, block_msg FROM installs WHERE install_id=%s", (install_id,))
        row = cur.fetchone()
        c.commit()

    return {"ok": True, "status": row["status"] if row else "unknown",
            "message": row["block_msg"] if row else ""}


# ── admin actions ─────────────────────────────────────────────────────────────
@app.post("/action")
async def action(request: Request):
    if request.query_params.get("key") != ADMIN_KEY:
        raise HTTPException(403)
    body = await request.json()
    install_id = str(body.get("id") or "")
    act        = str(body.get("action") or "")
    msg        = str(body.get("message") or "")
    if not install_id or act not in ("approve","block"):
        raise HTTPException(400)
    with _conn() as c, c.cursor() as cur:
        if act == "approve":
            cur.execute("UPDATE installs SET status='approved', block_msg='' WHERE install_id=%s", (install_id,))
        else:
            cur.execute("UPDATE installs SET status='blocked', block_msg=%s WHERE install_id=%s", (msg, install_id))
        c.commit()
    return {"ok": True}


# ── JSON API ──────────────────────────────────────────────────────────────────
@app.get("/api/installs")
async def api_installs(request: Request):
    if request.query_params.get("key") != ADMIN_KEY:
        raise HTTPException(403)
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT * FROM installs ORDER BY last_seen DESC")
        installs = [dict(r) for r in cur.fetchall()]
        for row in installs:
            for k,v in row.items():
                if isinstance(v, datetime): row[k] = v.isoformat()
        cur.execute("SELECT * FROM devices ORDER BY install_id, id")
        devices = [dict(r) for r in cur.fetchall()]
    devmap: dict[str, list] = {}
    for d in devices:
        devmap.setdefault(d["install_id"], []).append(d)
    for row in installs:
        row["devices"] = devmap.get(row["install_id"], [])
    return installs


# ── dashboard HTML ─────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if request.query_params.get("key") != ADMIN_KEY:
        return HTMLResponse(_login_html(), 401)
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT i.*,
          (SELECT COUNT(*) FROM devices d WHERE d.install_id=i.install_id) devcount
          FROM installs i ORDER BY last_seen DESC""")
        installs = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT * FROM devices ORDER BY install_id, id")
        devices  = [dict(r) for r in cur.fetchall()]

    now = datetime.now(timezone.utc)
    devmap: dict[str, list] = {}
    for d in devices:
        devmap.setdefault(d["install_id"], []).append(d)

    total    = len(installs)
    active   = sum(1 for r in installs if r["last_seen"] and (now - r["last_seen"]) < timedelta(hours=24))
    blocked  = sum(1 for r in installs if r["status"] == "blocked")
    approved = sum(1 for r in installs if r["status"] == "approved")

    rows_html = ""
    for r in installs:
        devs = devmap.get(r["install_id"], [])
        dev_html = "".join(
            f'<div class="dev"><span class="dname">{_esc(d["name"] or "")}</span>'
            f'<span class="dhost">{_esc(d["host"] or "")}:{d["port"] or ""}</span>'
            f'<span class="dprot">{_esc(d["protocol"] or "")}</span></div>'
            for d in devs
        )
        since = ""
        if r["last_seen"]:
            delta = now - r["last_seen"]
            if delta < timedelta(minutes=5):   since = '<span class="live">🟢 פעיל</span>'
            elif delta < timedelta(hours=1):   since = f'לפני {int(delta.seconds/60)} דק\''
            elif delta < timedelta(hours=24):  since = f'לפני {int(delta.seconds/3600)} שע\''
            else:                              since = f'לפני {delta.days} ימים'

        status   = r["status"] or "unknown"
        badge    = (f'<span class="badge {status}">'
                    + {"blocked":"🔒 חסום","approved":"✅ מאושר"}.get(status,"❓ לא ידוע")
                    + '</span>')
        fs = r["first_seen"].strftime("%d/%m/%y %H:%M") if r["first_seen"] else ""
        short_id = r["install_id"][:8] + "…"
        rows_html += f"""<tr data-id="{_esc(r['install_id'])}">
          <td><code title="{_esc(r['install_id'])}">{short_id}</code></td>
          <td>{_esc(r['computer'] or '—')}</td>
          <td>{_esc(r['version'] or '')}</td>
          <td>{_esc(r['os'] or '')}</td>
          <td>{since}</td>
          <td>{fs}</td>
          <td class="num">{r['device_count'] or 0}</td>
          <td>{badge}</td>
          <td class="devlist">{dev_html}</td>
          <td class="acts">
            {"<button class='btn-approve' onclick='act(this,\"approve\")'>✔ אשר</button>" if status!="approved" else ""}
            {"<button class='btn-block' onclick='act(this,\"block\")'>🔒 חסום</button>" if status!="blocked" else ""}
          </td>
        </tr>"""

    return HTMLResponse(_dashboard_html(
        total, active, blocked, approved, rows_html,
        request.query_params.get("key")))


def _esc(s: str) -> str:
    return (s.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
             .replace('"',"&quot;").replace("'","&#39;"))


def _login_html():
    return """<!DOCTYPE html><html dir="rtl" lang="he">
<head><meta charset="utf-8"><title>לוח ניהול</title>
<style>body{background:#0d1117;color:#c9d1d9;font-family:system-ui,sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:40px;text-align:center;min-width:320px}
h1{margin-bottom:20px;font-size:20px}
input{width:100%;padding:10px;background:#0d1117;border:1px solid #30363d;border-radius:8px;
color:#c9d1d9;font-size:14px;margin-bottom:12px}
button{width:100%;padding:10px;background:#1f6feb;border:none;border-radius:8px;
color:#fff;font-size:15px;cursor:pointer}button:hover{background:#388bfd}</style>
</head><body><div class="box"><h1>🎥 לוח ניהול</h1>
<form onsubmit="location.href='/?key='+document.getElementById('k').value;return false">
<input id="k" type="password" placeholder="מפתח מנהל" autofocus>
<button type="submit">כניסה</button></form></div></body></html>"""


def _dashboard_html(total, active, blocked, approved, rows_html, key):
    return f"""<!DOCTYPE html>
<html dir="rtl" lang="he">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>לוח ניהול — צופה מצלמות</title>
<style>
:root{{--bg:#0d1117;--bg2:#161b22;--bg3:#21262d;--border:#30363d;
  --txt:#c9d1d9;--muted:#8b949e;--blue:#58a6ff;--green:#3fb950;
  --yellow:#d29922;--red:#ff4d4f;--radius:10px}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--txt);font-family:system-ui,sans-serif;
  padding:24px 20px;font-size:14px;min-height:100vh}}
h1{{color:var(--blue);font-size:22px;margin-bottom:4px}}
.sub{{color:var(--muted);font-size:12px;margin-bottom:24px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:28px}}
.card{{background:var(--bg2);border:1px solid var(--border);border-radius:var(--radius);
  padding:16px 20px}}
.card .val{{font-size:32px;font-weight:700;margin-bottom:4px}}
.card .lbl{{color:var(--muted);font-size:12px}}
.card.blue .val{{color:var(--blue)}}
.card.green .val{{color:var(--green)}}
.card.red .val{{color:var(--red)}}
.card.yellow .val{{color:var(--yellow)}}
.toolbar{{display:flex;gap:10px;margin-bottom:16px;flex-wrap:wrap}}
.search{{flex:1;min-width:200px;padding:8px 12px;background:var(--bg2);
  border:1px solid var(--border);border-radius:8px;color:var(--txt);font-size:13px}}
.refresh{{padding:8px 16px;background:var(--bg3);border:1px solid var(--border);
  border-radius:8px;color:var(--muted);cursor:pointer;font-size:13px}}
.refresh:hover{{color:var(--txt)}}
.wrap{{overflow-x:auto;border-radius:var(--radius);border:1px solid var(--border)}}
table{{width:100%;border-collapse:collapse;min-width:800px}}
th{{background:var(--bg2);color:var(--muted);padding:10px 12px;text-align:right;
  font-weight:600;font-size:12px;white-space:nowrap;border-bottom:2px solid var(--border)}}
td{{padding:10px 12px;border-bottom:1px solid var(--border);vertical-align:top}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:#161b2288}}
code{{background:var(--bg3);border-radius:4px;padding:2px 6px;font-size:12px}}
.num{{text-align:center}}
.live{{color:var(--green);font-size:12px}}
.badge{{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600;white-space:nowrap}}
.badge.blocked{{background:#ff4d4f18;color:var(--red);border:1px solid #ff4d4f44}}
.badge.approved{{background:#3fb95018;color:var(--green);border:1px solid #3fb95044}}
.badge.unknown{{background:#d2992218;color:var(--yellow);border:1px solid #d2992244}}
.devlist{{font-size:11px}}
.dev{{display:flex;gap:6px;align-items:center;padding:2px 0;color:var(--muted)}}
.dname{{color:var(--txt);font-weight:500}}
.dhost{{color:var(--blue);font-family:monospace}}
.dprot{{background:var(--bg3);border-radius:4px;padding:1px 5px;font-size:10px}}
.acts{{white-space:nowrap}}
button.btn-approve{{padding:4px 10px;border-radius:6px;border:1px solid #3fb95044;
  background:#3fb95010;color:var(--green);cursor:pointer;font-size:11px;margin:2px}}
button.btn-block{{padding:4px 10px;border-radius:6px;border:1px solid #ff4d4f44;
  background:#ff4d4f10;color:var(--red);cursor:pointer;font-size:11px;margin:2px}}
button.btn-approve:hover{{background:#3fb95028}}
button.btn-block:hover{{background:#ff4d4f28}}
#modal-bg{{display:none;position:fixed;inset:0;background:#00000099;
  align-items:center;justify-content:center;z-index:999}}
#modal-bg.open{{display:flex}}
#modal{{background:var(--bg2);border:1px solid var(--border);border-radius:14px;
  padding:28px;min-width:380px;max-width:500px;width:90%}}
#modal h2{{margin-bottom:8px;color:var(--red);font-size:17px}}
#modal p{{color:var(--muted);font-size:13px;margin-bottom:12px}}
#modal textarea{{width:100%;min-height:80px;background:var(--bg);color:var(--txt);
  border:1px solid var(--border);border-radius:8px;padding:10px;font-size:13px;
  resize:vertical;margin-bottom:16px}}
.modal-row{{display:flex;gap:8px;justify-content:flex-end}}
.modal-row button{{padding:8px 18px;border-radius:8px;font-size:13px;cursor:pointer;border:none}}
.btn-cancel{{background:var(--bg3);color:var(--txt)}}
.btn-ok{{background:var(--red);color:#fff}}
.btn-ok:hover{{background:#ff6b6b}}
#refresh-note{{color:var(--muted);font-size:11px;text-align:left;margin-top:12px}}
</style>
</head>
<body>
<h1>🎥 לוח ניהול — צופה מצלמות אוניברסלי</h1>
<p class="sub">מתרענן כל 60 שניות &nbsp;·&nbsp; <span id="clock"></span></p>

<div class="cards">
  <div class="card blue"><div class="val">{total}</div><div class="lbl">סה"כ התקנות</div></div>
  <div class="card green"><div class="val">{active}</div><div class="lbl">פעילות (24 שע')</div></div>
  <div class="card red"><div class="val">{blocked}</div><div class="lbl">חסומות</div></div>
  <div class="card yellow"><div class="val">{approved}</div><div class="lbl">מאושרות</div></div>
</div>

<div class="toolbar">
  <input class="search" type="text" placeholder="חפש לפי ID, מחשב, IP, גרסה..." oninput="filter(this.value)">
  <button class="refresh" onclick="location.reload()">↻ רענן</button>
</div>

<div class="wrap">
<table id="tbl">
<thead><tr>
  <th>ID</th><th>מחשב</th><th>גרסה</th><th>OS</th>
  <th>פעיל</th><th>הצטרף</th><th>מצלמות</th><th>סטטוס</th>
  <th>מכשירים</th><th>פעולות</th>
</tr></thead>
<tbody id="tbody">{rows_html}</tbody>
</table>
</div>
<p id="refresh-note"></p>

<div id="modal-bg">
<div id="modal">
  <h2>🔒 חסימת התקנה</h2>
  <p>כתוב הודעה שתוצג למשתמש החסום (אפשר להשאיר ריק לברירת מחדל):</p>
  <textarea id="block-msg" placeholder="ההתקנה נחסמה. לפתיחת הגישה פנה למנהל."></textarea>
  <div class="modal-row">
    <button class="btn-cancel" onclick="closeModal()">ביטול</button>
    <button class="btn-ok" onclick="doBlock()">🔒 חסום</button>
  </div>
</div>
</div>

<script>
const KEY = {json.dumps(key)};
let _blockRow = null;

function act(btn, action) {{
  const tr = btn.closest("tr");
  const id = tr.dataset.id;
  if (action === "block") {{ _blockRow = tr; document.getElementById("block-msg").value=""; document.getElementById("modal-bg").classList.add("open"); return; }}
  _send(id, action, "");
}}
function closeModal() {{ document.getElementById("modal-bg").classList.remove("open"); _blockRow=null; }}
function doBlock() {{ const msg=document.getElementById("block-msg").value; _send(_blockRow.dataset.id,"block",msg); closeModal(); }}

function _send(id, action, message) {{
  fetch("/action?key="+encodeURIComponent(KEY), {{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify({{id,action,message}})}})
    .then(r=>r.json()).then(d=>{{ if(d.ok) location.reload(); else alert("שגיאה: "+JSON.stringify(d)); }});
}}

function filter(q) {{
  q = q.toLowerCase();
  document.querySelectorAll("#tbody tr").forEach(tr => {{
    tr.style.display = q==='' || tr.textContent.toLowerCase().includes(q) ? "" : "none";
  }});
}}

document.getElementById("modal-bg").addEventListener("click", e => {{ if(e.target===e.currentTarget) closeModal(); }});

// clock + auto-refresh
function tick() {{ document.getElementById("clock").textContent = new Date().toLocaleTimeString("he-IL"); }}
tick(); setInterval(tick, 1000);
let secs = 60;
const note = document.getElementById("refresh-note");
const countdown = setInterval(() => {{ secs--; note.textContent="רענון אוטומטי בעוד "+secs+" שניות"; if(secs<=0){{ clearInterval(countdown); location.reload(); }} }}, 1000);
</script>
</body>
</html>"""
