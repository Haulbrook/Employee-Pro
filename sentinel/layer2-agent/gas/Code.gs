/**
 * SENTINEL — Google Apps Script web-app backend (Phase B.1)
 *
 * Deploy as a Web App (Deploy → New deployment → type "Web app").
 * Bind to a Google Sheet with a tab named "tasks" and the 10-column schema:
 *   A task_id  B task_type  C description    D priority   E status
 *   F assigned_to  G created_at  H completed_at  I result   J error
 *
 * Authentication (Gate-2 FIX 6 / CT-18):
 *   The shared secret SENTINEL_SECRET is stored via Script Properties
 *   (Project Settings → Script Properties). Clients send it in the POST
 *   *body*, never as a URL query param.
 *
 * Setup:
 *   1. Create a Google Sheet with tab "tasks" and the header row above.
 *   2. Tools → Script Editor → paste this file as Code.gs.
 *   3. File → Project Properties → Script Properties:
 *        SENTINEL_SECRET   = <random string, also placed in /opt/sentinel/.env>
 *        TASKS_SHEET_NAME  = tasks            (optional override)
 *   4. Deploy → New deployment → Web app → Execute as: Me, Access: Anyone.
 *   5. Copy the URL into GAS_ENDPOINT_URL in /opt/sentinel/.env.
 */

var TASKS_SHEET = PropertiesService.getScriptProperties().getProperty('TASKS_SHEET_NAME') || 'tasks';
var STATUS = {
  PENDING:     'pending',
  PROCESSING:  'processing',
  COMPLETE:    'complete',
  FAILED:      'failed',
  QUARANTINED: 'quarantined'
};

var COL = {
  TASK_ID:      1,
  TASK_TYPE:    2,
  DESCRIPTION:  3,
  PRIORITY:     4,
  STATUS:       5,
  ASSIGNED_TO:  6,
  CREATED_AT:   7,
  COMPLETED_AT: 8,
  RESULT:       9,
  ERROR:       10
};

var VALID_TASK_TYPES = ['classify','generate','analyze','escalate','report','alert','custom'];
var VALID_STATUSES   = [STATUS.PENDING, STATUS.PROCESSING, STATUS.COMPLETE, STATUS.FAILED, STATUS.QUARANTINED];

// ---------- entry points ----------------------------------------------------

function doPost(e) {
  try {
    var body = JSON.parse(e.postData && e.postData.contents ? e.postData.contents : '{}');
    if (!authenticate_(body)) {
      return jsonResponse_(403, { error: 'forbidden' });
    }
    var action = body.action;
    switch (action) {
      case 'getNextTask': return jsonResponse_(200, getNextTask_(body));
      case 'submitResult': return jsonResponse_(200, submitResult_(body));
      case 'getStatus':    return jsonResponse_(200, getStatus_(body));
      case 'updateTask':   return jsonResponse_(200, updateTask_(body));
      case 'enqueueTask':  return jsonResponse_(200, enqueueTask_(body));
      default:
        return jsonResponse_(400, { error: 'unknown action: ' + action });
    }
  } catch (err) {
    return jsonResponse_(500, { error: String(err) });
  }
}

function doGet(e) {
  // GET is read-only health probe — does NOT accept secret-in-URL on purpose.
  return jsonResponse_(200, {
    service: 'SENTINEL-GAS',
    status: 'ok',
    sheet: TASKS_SHEET,
    timestamp: new Date().toISOString()
  });
}

// ---------- auth ------------------------------------------------------------

function authenticate_(body) {
  var expected = PropertiesService.getScriptProperties().getProperty('SENTINEL_SECRET');
  if (!expected) {
    // No secret configured — fail closed.
    return false;
  }
  return body && typeof body.secret === 'string' && body.secret === expected;
}

// ---------- actions ---------------------------------------------------------

function getNextTask_(body) {
  var instanceId = body.instance_id || 'SENTINEL-01';
  var sheet = openTasksSheet_();
  var data = sheet.getDataRange().getValues();
  if (data.length < 2) return { task: null };

  // Skip header row. Pick the highest-priority pending task (lowest priority number).
  var bestRow = -1;
  var bestPriority = 99;
  for (var i = 1; i < data.length; i++) {
    var row = data[i];
    var status = String(row[COL.STATUS - 1] || '').toLowerCase();
    var assigned = String(row[COL.ASSIGNED_TO - 1] || '');
    if (status !== STATUS.PENDING) continue;
    if (assigned && assigned !== instanceId && assigned !== 'SENTINEL') continue;
    var priority = parseInt(row[COL.PRIORITY - 1], 10);
    if (isNaN(priority)) priority = 3;
    if (priority < bestPriority) {
      bestPriority = priority;
      bestRow = i;
    }
  }
  if (bestRow === -1) return { task: null };

  // Claim the row by flipping status to processing + stamping assigned_to.
  var rowNum = bestRow + 1;
  sheet.getRange(rowNum, COL.STATUS).setValue(STATUS.PROCESSING);
  sheet.getRange(rowNum, COL.ASSIGNED_TO).setValue(instanceId);
  if (!data[bestRow][COL.CREATED_AT - 1]) {
    sheet.getRange(rowNum, COL.CREATED_AT).setValue(new Date().toISOString());
  }
  SpreadsheetApp.flush();

  return {
    task: rowToTask_(data[bestRow], rowNum)
  };
}

function submitResult_(body) {
  var taskId = body.task_id;
  var result = body.result || '';
  var status = (body.status || STATUS.COMPLETE).toLowerCase();
  var error  = body.error || '';

  if (!taskId) throw new Error('task_id required');
  if (VALID_STATUSES.indexOf(status) === -1) throw new Error('invalid status: ' + status);

  var found = findRowByTaskId_(taskId);
  if (!found) throw new Error('task not found: ' + taskId);
  var sheet = found.sheet;
  var rowNum = found.rowNum;

  sheet.getRange(rowNum, COL.STATUS).setValue(status);
  sheet.getRange(rowNum, COL.RESULT).setValue(String(result).slice(0, 50000));
  sheet.getRange(rowNum, COL.ERROR).setValue(String(error).slice(0, 5000));
  if (status === STATUS.COMPLETE || status === STATUS.FAILED || status === STATUS.QUARANTINED) {
    sheet.getRange(rowNum, COL.COMPLETED_AT).setValue(new Date().toISOString());
  }
  SpreadsheetApp.flush();
  return { ok: true, task_id: taskId, status: status };
}

function getStatus_(body) {
  var sheet = openTasksSheet_();
  var data = sheet.getDataRange().getValues();
  var counts = { pending: 0, processing: 0, complete: 0, failed: 0, quarantined: 0 };
  for (var i = 1; i < data.length; i++) {
    var s = String(data[i][COL.STATUS - 1] || '').toLowerCase();
    if (counts[s] !== undefined) counts[s]++;
  }
  return {
    sheet: TASKS_SHEET,
    counts: counts,
    total: data.length - 1,
    timestamp: new Date().toISOString()
  };
}

function updateTask_(body) {
  var taskId = body.task_id;
  if (!taskId) throw new Error('task_id required');
  var found = findRowByTaskId_(taskId);
  if (!found) throw new Error('task not found: ' + taskId);
  var sheet = found.sheet;
  var rowNum = found.rowNum;

  var fields = body.fields || {};
  var allowed = {
    task_type:   COL.TASK_TYPE,
    description: COL.DESCRIPTION,
    priority:    COL.PRIORITY,
    status:      COL.STATUS,
    assigned_to: COL.ASSIGNED_TO,
    result:      COL.RESULT,
    error:       COL.ERROR
  };
  for (var k in fields) {
    if (allowed.hasOwnProperty(k)) {
      var v = fields[k];
      if (k === 'status' && VALID_STATUSES.indexOf(String(v).toLowerCase()) === -1) {
        throw new Error('invalid status: ' + v);
      }
      if (k === 'task_type' && VALID_TASK_TYPES.indexOf(String(v).toLowerCase()) === -1) {
        throw new Error('invalid task_type: ' + v);
      }
      sheet.getRange(rowNum, allowed[k]).setValue(v);
    }
  }
  SpreadsheetApp.flush();
  return { ok: true, task_id: taskId, updated: Object.keys(fields) };
}

function enqueueTask_(body) {
  // Helper for tests / ad-hoc submission. Validates inputs.
  var taskType = String(body.task_type || '').toLowerCase();
  if (VALID_TASK_TYPES.indexOf(taskType) === -1) throw new Error('invalid task_type: ' + taskType);
  var description = String(body.description || '');
  if (!description) throw new Error('description required');
  var priority = parseInt(body.priority, 10);
  if (isNaN(priority) || priority < 1 || priority > 5) priority = 3;

  var sheet = openTasksSheet_();
  var taskId = body.task_id || nextTaskId_(sheet);
  sheet.appendRow([
    taskId,
    taskType,
    description,
    priority,
    STATUS.PENDING,
    body.assigned_to || '',
    new Date().toISOString(),
    '',
    '',
    ''
  ]);
  SpreadsheetApp.flush();
  return { ok: true, task_id: taskId };
}

// ---------- helpers ---------------------------------------------------------

function openTasksSheet_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  if (!ss) throw new Error('script must be bound to a spreadsheet');
  var sheet = ss.getSheetByName(TASKS_SHEET);
  if (!sheet) throw new Error('sheet not found: ' + TASKS_SHEET);
  return sheet;
}

function findRowByTaskId_(taskId) {
  var sheet = openTasksSheet_();
  var ids = sheet.getRange(2, COL.TASK_ID, Math.max(0, sheet.getLastRow() - 1)).getValues();
  for (var i = 0; i < ids.length; i++) {
    if (String(ids[i][0]) === String(taskId)) {
      return { sheet: sheet, rowNum: i + 2 };
    }
  }
  return null;
}

function rowToTask_(row, rowNum) {
  return {
    task_id:      String(row[COL.TASK_ID - 1]),
    task_type:    String(row[COL.TASK_TYPE - 1] || '').toLowerCase(),
    description:  String(row[COL.DESCRIPTION - 1] || ''),
    priority:     parseInt(row[COL.PRIORITY - 1], 10) || 3,
    status:       String(row[COL.STATUS - 1] || '').toLowerCase(),
    assigned_to:  String(row[COL.ASSIGNED_TO - 1] || ''),
    created_at:   String(row[COL.CREATED_AT - 1] || ''),
    completed_at: String(row[COL.COMPLETED_AT - 1] || ''),
    result:       String(row[COL.RESULT - 1] || ''),
    error:        String(row[COL.ERROR - 1] || ''),
    row:          rowNum
  };
}

function nextTaskId_(sheet) {
  var lastRow = sheet.getLastRow();
  var n = Math.max(0, lastRow - 1) + 1;
  return 'T-' + ('000' + n).slice(-3);
}

function jsonResponse_(httpCode, payload) {
  // GAS web-app responses cannot set arbitrary HTTP status codes; encode it
  // in the body so the client can branch. The 200 envelope keeps it simple.
  var body = { http_code: httpCode };
  for (var k in payload) body[k] = payload[k];
  return ContentService
    .createTextOutput(JSON.stringify(body))
    .setMimeType(ContentService.MimeType.JSON);
}
