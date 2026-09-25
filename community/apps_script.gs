/**
 * PierTracker community IDs -> Google Sheet (see docs/COMMUNITY.md).
 *
 * One-time setup:
 *   1. Make a new Google Sheet. Extensions > Apps Script, paste this file, save.
 *   2. Set SECRET below to a long random string, and put the same string in livecams.json
 *      ("community": {"appsScriptSecret": "..."}).
 *   3. Deploy > New deployment > Web app. Execute as: Me. Who has access: Anyone.
 *      Copy the web app URL into livecams.json ("community": {"appsScriptUrl": "..."}).
 *
 * Each answer becomes a row, with the tracker's picture of that moment saved in a Drive folder and
 * linked from the row, and a "Reviewed by expert" checkbox plus a column for the expert's ID.
 * Only requests carrying SECRET are accepted, so only your PierTracker server can add rows.
 */
const SECRET = 'change-me-to-a-long-random-string';
const FOLDER_NAME = 'PierTracker community pictures';
const HEADERS = ['Submitted', 'Seen at (camera time)', 'Their ID', 'By', 'Note', 'Tracker said', 'Tracker confidence',
                 'Tracker status', 'Picture', 'Reviewed by expert', "Expert's ID"];

function doPost(e) {
  let data;
  try {
    data = JSON.parse(e.postData.contents);
  } catch (err) {
    return reply('bad request');
  }
  if (!data || data.secret !== SECRET || SECRET.startsWith('change-me')) return reply('forbidden');

  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheets()[0];
    if (sheet.getLastRow() === 0) {
      sheet.appendRow(HEADERS);
      sheet.setFrozenRows(1);
      sheet.getRange(1, 1, 1, HEADERS.length).setFontWeight('bold');
    }
    const blob = Utilities.newBlob(Utilities.base64Decode(data.image_jpeg_base64), 'image/jpeg', data.picture);
    const file = folder().createFile(blob);
    sheet.appendRow([data.submitted_at, data.taken_at, safe(data.community_id), safe(data.handle), safe(data.note),
                     data.tracker_said, data.tracker_prob, data.status, file.getUrl(), false, '']);
    sheet.getRange(sheet.getLastRow(), HEADERS.indexOf('Reviewed by expert') + 1).insertCheckboxes();
  } finally {
    lock.releaseLock();
  }
  return reply('ok');
}

/** Text from the public, never run as a formula. */
function safe(text) {
  text = String(text || '');
  return /^[=+\-@]/.test(text) ? "'" + text : text;
}

function folder() {
  const found = DriveApp.getFoldersByName(FOLDER_NAME);
  return found.hasNext() ? found.next() : DriveApp.createFolder(FOLDER_NAME);
}

function reply(text) {
  return ContentService.createTextOutput(text);
}
