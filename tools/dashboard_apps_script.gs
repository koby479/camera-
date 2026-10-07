/*
 * Universal Cam Viewer — Management Dashboard (Google Apps Script)
 *
 * Setup:
 *   1. Create a new Google Sheet.
 *   2. Extensions → Apps Script → paste this file.
 *   3. Deploy → New deployment → Web app.
 *      Execute as: Me   |   Who has access: Only myself  (keeps the dashboard private)
 *   4. Copy the /exec URL, put it in app/core/telemetry.py DEFAULT_WEBHOOK (and rebuild the EXE).
 *
 * What the dashboard shows:
 *   • Every installation that ever called home: id, first/last seen, version, device count, IP list.
 *   • Status column: "approved" / "blocked" / "unknown" – editable directly in the sheet or via buttons.
 *   • Approve / Block buttons on the HTML dashboard page (visit the /exec URL in a browser).
 *
 * What is sent by the app (see app/core/telemetry.py):
 *   install_id, version, time, os, optional computer name, and per-device: name, host, port, protocol, channel.
 *   Usernames and passwords of cameras are NEVER sent by the app.
 */

const INSTALLS_SHEET = "Installs";
const DEVICES_SHEET  = "Devices";

const INST_COLS = {
  install_id:   1,
  first_seen:   2,
  last_seen:    3,
  version:      4,
  os:           5,
  computer:     6,
  device_count: 7,
  status:       8,
  block_msg:    9,
};

const DEV_COLS = {
  install_id: 1,
  last_seen:  2,
  name:       3,
  host:       4,
  port:       5,
  protocol:   6,
  channel:    7,
  is_nvr:     8,
};

// ── incoming heartbeat ────────────────────────────────────────────────────────
function doPost(e) {
  try {
    const data = JSON.parse(e.postData.contents);
    if (!data.install_id) return _json({ok:false, error:"missing install_id"});
    const status = _upsertInstall(data);
    _replaceDevices(data);
    const msg = status === "blocked"
      ? (_instSheet().getRange(_rowOf(data.install_id), INST_COLS.block_msg).getValue() || "")
      : "";
    return _json({ok:true, status:status, message:msg});
  } catch(err) {
    return _json({ok:false, error:String(err), status:"error"});
  }
}

// ── HTML dashboard ────────────────────────────────────────────────────────────
function doGet(e) {
  if (e.parameter.action) return _handleAction(e);
  return HtmlService
    .createHtmlOutput(_buildHtml())
    .setTitle("צופה מצלמות - לוח ניהול")
    .setXFrameOptionsMode(HtmlService.XFrameOptionsMode.ALLOWALL);
}

function _handleAction(e) {
  const id  = e.parameter.id     || "";
  const act = e.parameter.action || "";
  const msg = e.parameter.msg    || "";
  if (!id || !["approve","block"].includes(act)) return _json({ok:false,error:"bad request"});
  const sh  = _instSheet();
  const row = _rowOf(id);
  if (!row) return _json({ok:false,error:"not found"});
  sh.getRange(row, INST_COLS.status).setValue(act === "approve" ? "approved" : "blocked");
  if (act === "block" && msg) sh.getRange(row, INST_COLS.block_msg).setValue(msg);
  if (act === "approve")      sh.getRange(row, INST_COLS.block_msg).setValue("");
  return _json({ok:true});
}

// ── sheet helpers ─────────────────────────────────────────────────────────────
function _instSheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(INSTALLS_SHEET);
  if (!sh) {
    sh = ss.insertSheet(INSTALLS_SHEET);
    sh.appendRow(Object.keys(INST_COLS));
    sh.setFrozenRows(1);
  }
  return sh;
}

function _devSheet() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(DEVICES_SHEET);
  if (!sh) {
    sh = ss.insertSheet(DEVICES_SHEET);
    sh.appendRow(Object.keys(DEV_COLS));
    sh.setFrozenRows(1);
  }
  return sh;
}

function _rowOf(install_id) {
  const sh = _instSheet();
  const last = sh.getLastRow();
  if (last < 2) return null;
  const ids = sh.getRange(2, 1, last-1, 1).getValues().flat();
  const idx = ids.indexOf(install_id);
  return idx === -1 ? null : idx + 2;
}

function _upsertInstall(data) {
  const sh  = _instSheet();
  const now = new Date();
  const row = _rowOf(data.install_id);
  const existingStatus = row ? sh.getRange(row, INST_COLS.status).getValue() : "unknown";
  const status = existingStatus || "unknown";
  const vals = [data.install_id, row ? sh.getRange(row, INST_COLS.first_seen).getValue() : now,
                now, data.version||"", data.os||"", data.computer||"",
                data.device_count||0, status,
                row ? sh.getRange(row, INST_COLS.block_msg).getValue() : ""];
  if (!row) sh.appendRow(vals);
  else sh.getRange(row, 1, 1, vals.length).setValues([vals]);
  return status;
}

function _replaceDevices(data) {
  const sh = _devSheet();
  const last = sh.getLastRow();
  if (last > 1) {
    const ids = sh.getRange(2,1,last-1,1).getValues();
    for (let i=ids.length; i>=1; i--)
      if (ids[i-1][0]===data.install_id) sh.deleteRow(i+1);
  }
  const now = new Date();
  (data.devices||[]).forEach(d =>
    sh.appendRow([data.install_id, now, d.name||"", d.host||"", d.port||"",
                  d.protocol||"", d.channel===null||d.channel===undefined?"":d.channel, !!d.is_nvr]));
}

// ── HTML page ─────────────────────────────────────────────────────────────────
function _buildHtml() {
  const sh   = _instSheet();
  const devSh= _devSheet();
  const last = sh.getLastRow();
  const me   = ScriptApp.getService().getUrl();

  let rows = "";
  if (last >= 2) {
    const data = sh.getRange(2,1,last-1,Object.keys(INST_COLS).length).getValues();
    // collect devices per install
    const devLast = devSh.getLastRow();
    const devMap  = {};
    if (devLast >= 2) {
      devSh.getRange(2,1,devLast-1,Object.keys(DEV_COLS).length).getValues().forEach(r => {
        const id = r[0]; if(!devMap[id]) devMap[id]=[];
        devMap[id].push({name:r[2],host:r[3],port:r[4],protocol:r[5],channel:r[6],is_nvr:r[7]});
      });
    }
    rows = data.map(r => {
      const id      = r[0];
      const first   = r[1] ? new Date(r[1]).toLocaleString("he-IL") : "";
      const last_s  = r[2] ? new Date(r[2]).toLocaleString("he-IL") : "";
      const ver     = r[3]; const os = r[4]; const comp = r[5]; const cnt = r[6];
      const status  = r[7]||"unknown";
      const devices = (devMap[id]||[]).map(d=>
        `<div class='dev'>${d.name||""} — ${d.host}:${d.port} (${d.protocol}${d.is_nvr?" NVR":""})</div>`).join("");
      const badge = status==="blocked"
        ? `<span class='badge blocked'>חסום</span>`
        : status==="approved"
        ? `<span class='badge approved'>מאושר</span>`
        : `<span class='badge unknown'>לא ידוע</span>`;
      const approveBtn = status!=="approved"
        ? `<button onclick="act('${id}','approve','')">✔ אשר</button>` : "";
      const blockBtn = status!=="blocked"
        ? `<button class='danger' onclick="askBlock('${id}')">✖ חסום</button>` : "";
      return `<tr>
        <td>${id.substring(0,8)}…</td>
        <td>${first}</td><td>${last_s}</td><td>${ver}</td>
        <td>${os}</td><td>${comp}</td><td>${cnt}</td>
        <td>${badge}</td>
        <td><div class='devs'>${devices}</div></td>
        <td class='actions'>${approveBtn} ${blockBtn}</td>
      </tr>`;
    }).join("");
  }

  return `<!DOCTYPE html>
<html dir="rtl" lang="he">
<head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>לוח ניהול — צופה מצלמות</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:system-ui,sans-serif;background:#0d1117;color:#c9d1d9;padding:24px;font-size:14px}
  h1{color:#58a6ff;margin-bottom:16px;font-size:22px}
  .meta{color:#8b949e;margin-bottom:20px;font-size:12px}
  table{width:100%;border-collapse:collapse}
  th{background:#161b22;color:#8b949e;padding:8px 10px;text-align:right;font-weight:600;
     border-bottom:2px solid #30363d;white-space:nowrap}
  td{padding:8px 10px;border-bottom:1px solid #21262d;vertical-align:top}
  tr:hover td{background:#161b22}
  .badge{display:inline-block;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:600}
  .badge.blocked{background:#ff4d4f22;color:#ff4d4f;border:1px solid #ff4d4f55}
  .badge.approved{background:#3fb95022;color:#3fb950;border:1px solid #3fb95055}
  .badge.unknown{background:#d2992222;color:#d29922;border:1px solid #d2992255}
  button{padding:4px 12px;border-radius:6px;border:1px solid #30363d;
         background:#21262d;color:#c9d1d9;cursor:pointer;font-size:12px;margin:2px}
  button:hover{background:#30363d}
  button.danger{border-color:#ff4d4f55;color:#ff4d4f}
  button.danger:hover{background:#ff4d4f22}
  .devs{font-size:12px;color:#8b949e}
  .dev{margin:2px 0}
  .actions{white-space:nowrap}
  #modal{display:none;position:fixed;inset:0;background:#00000088;align-items:center;justify-content:center}
  #modal.open{display:flex}
  #modal-box{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:24px;min-width:360px}
  #modal-box h2{margin-bottom:12px;color:#ff4d4f;font-size:16px}
  #modal-box textarea{width:100%;height:80px;background:#0d1117;color:#c9d1d9;border:1px solid #30363d;
    border-radius:6px;padding:8px;font-size:13px;resize:vertical;margin-bottom:12px}
  #modal-box .row{display:flex;gap:8px;justify-content:flex-end}
  .empty{color:#8b949e;padding:20px;text-align:center}
</style>
</head>
<body>
<h1>🎥 לוח ניהול — צופה מצלמות אוניברסלי</h1>
<p class="meta">מציג את כל ההתקנות שדיווחו ופרטי המצלמות שלהן.
  לחץ ✔ אשר / ✖ חסום. הגיליון עצמו מכיל את כל הנתונים.</p>

${rows ? `<table>
  <thead><tr>
    <th>ID</th><th>ראשון</th><th>אחרון</th><th>גרסה</th>
    <th>OS</th><th>מחשב</th><th>מצלמות</th><th>סטטוס</th><th>מכשירים</th><th>פעולות</th>
  </tr></thead>
  <tbody>${rows}</tbody>
</table>` : '<p class="empty">אין דיווחים עדיין. הגדר את ה-webhook בתוכנה.</p>'}

<div id="modal">
  <div id="modal-box">
    <h2>חסימת התקנה</h2>
    <p style="margin-bottom:8px;font-size:13px;color:#8b949e">הודעה שתוצג למשתמש (אופציונלי):</p>
    <textarea id="block-msg" placeholder="ההתקנה הזו נחסמה. לפתיחת הגישה פנה למנהל המערכת."></textarea>
    <div class="row">
      <button onclick="closeModal()">ביטול</button>
      <button class="danger" onclick="doBlock()">✖ חסום</button>
    </div>
  </div>
</div>

<script>
const ENDPOINT = "${me}";
let _blockId = null;

function act(id, action, msg) {
  fetch(ENDPOINT + "?action=" + action + "&id=" + encodeURIComponent(id)
        + (msg ? "&msg=" + encodeURIComponent(msg) : ""))
    .then(r => r.json())
    .then(d => { if(d.ok) location.reload(); else alert("שגיאה: " + d.error); })
    .catch(e => alert("שגיאת רשת: " + e));
}
function askBlock(id) { _blockId=id; document.getElementById("block-msg").value=""; document.getElementById("modal").classList.add("open"); }
function closeModal() { document.getElementById("modal").classList.remove("open"); _blockId=null; }
function doBlock() { act(_blockId,"block",document.getElementById("block-msg").value); closeModal(); }
document.getElementById("modal").addEventListener("click",function(e){if(e.target===this)closeModal();});
</script>
</body></html>`;
}

function _json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
