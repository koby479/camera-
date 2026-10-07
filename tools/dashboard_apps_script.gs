/*
 * Management dashboard backend for Universal Cam Viewer's opt-in reporting.
 * Receives what app/core/telemetry.py sends (install id, version, time, device list WITHOUT
 * credentials) and keeps one row per installation in a Google Sheet, plus a device list sheet.
 *
 * Setup (one time):
 *   1. Create a new Google Sheet. Extensions -> Apps Script. Paste this file in, replacing the default code.
 *   2. Deploy -> New deployment -> type "Web app". Execute as: Me. Who has access: Anyone.
 *   3. Copy the deployment URL (ends in /exec) and put it in the app's settings.json as
 *      "telemetry_webhook": "<that URL>", or set app/core/telemetry.DEFAULT_WEBHOOK to it before building the EXE.
 *   4. Open the sheet: an "Installs" tab (one row per installation, updated in place) and a "Devices" tab
 *      (one row per NVR/camera reported) are created automatically on first report.
 *
 * This script never receives or stores camera usernames/passwords - the app does not send them.
 */

const INSTALLS_SHEET = "Installs";
const DEVICES_SHEET = "Devices";
const INSTALLS_HEADER = ["install_id", "first_seen", "last_seen", "version", "os", "computer", "device_count"];
const DEVICES_HEADER = ["install_id", "last_seen", "name", "host", "port", "protocol", "channel", "is_nvr"];

function doPost(e) {
  try {
    const data = JSON.parse(e.postData.contents);
    if (!data.install_id) return _json({ok: false, error: "missing install_id"});
    _upsertInstall(data);
    _replaceDevices(data);
    return _json({ok: true});
  } catch (err) {
    return _json({ok: false, error: String(err)});
  }
}

function doGet(e) {
  return _json({ok: true, info: "POST only - this is the Universal Cam Viewer reporting endpoint"});
}

function _sheet(name, header) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let sh = ss.getSheetByName(name);
  if (!sh) {
    sh = ss.insertSheet(name);
    sh.appendRow(header);
    sh.setFrozenRows(1);
  }
  return sh;
}

function _upsertInstall(data) {
  const sh = _sheet(INSTALLS_SHEET, INSTALLS_HEADER);
  const now = new Date();
  const ids = sh.getRange(2, 1, Math.max(sh.getLastRow() - 1, 0), 1).getValues().flat();
  const row = [data.install_id, now, now, data.version || "", data.os || "", data.computer || "",
               data.device_count || 0];
  const idx = ids.indexOf(data.install_id);
  if (idx === -1) {
    sh.appendRow(row);
  } else {
    const r = idx + 2;
    sh.getRange(r, 1, 1, INSTALLS_HEADER.length).setValues([row]);
    sh.getRange(r, 2).setValue(sh.getRange(r, 2).getValue() || now);   // keep the original first_seen
  }
}

function _replaceDevices(data) {
  const sh = _sheet(DEVICES_SHEET, DEVICES_HEADER);
  const last = sh.getLastRow();
  if (last > 1) {
    const ids = sh.getRange(2, 1, last - 1, 1).getValues();
    for (let i = ids.length; i >= 1; i--) {
      if (ids[i - 1][0] === data.install_id) sh.deleteRow(i + 1);
    }
  }
  const now = new Date();
  (data.devices || []).forEach(function (d) {
    sh.appendRow([data.install_id, now, d.name || "", d.host || "", d.port || "", d.protocol || "",
                  d.channel === null || d.channel === undefined ? "" : d.channel, !!d.is_nvr]);
  });
}

function _json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
