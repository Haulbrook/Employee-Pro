// SENTINEL — dashboard.js (Phase D.3)
// Polls /api/* every 30 s and renders the six dashboard sections.

const REFRESH_MS = 30_000;
const TASK_LIMIT = 50;

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// --- helpers --------------------------------------------------------------

function fmtUptime(seconds) {
  if (!seconds || seconds < 0) return '—';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d}d ${h}h ${m}m`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

function fmtPct(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—';
  return `${n.toFixed(1)}%`;
}

function safeText(s) {
  // Defense in depth — Gate-2 also sanitizes server-side, but never trust:
  // never call innerHTML with untrusted data.
  return String(s ?? '');
}

function setBar(el, pct, warnAt = 85, errAt = 95) {
  el.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  el.classList.remove('warn', 'err');
  if (pct >= errAt) el.classList.add('err');
  else if (pct >= warnAt) el.classList.add('warn');
}

async function getJSON(url) {
  const resp = await fetch(url, { cache: 'no-store', credentials: 'omit' });
  if (!resp.ok) throw new Error(`${url} → ${resp.status}`);
  return resp.json();
}

// --- renderers ------------------------------------------------------------

function renderStatus(s) {
  $('#instance-id').textContent = safeText(s.instance_id);
  $('#kv-mode').textContent = safeText(s.mode);
  $('#kv-network').textContent = safeText(s.network_state);
  $('#kv-uptime').textContent = fmtUptime(s.uptime_seconds);
  $('#kv-completed').textContent = String(s.tasks_completed ?? 0);
  $('#kv-failed').textContent = String(s.tasks_failed ?? 0);
  const q = s.quarantined_components || [];
  $('#kv-quarantined').textContent = q.length ? q.join(', ') : 'none';

  const overallEl = $('#overall');
  const dot = $('#status-dot');
  overallEl.textContent = (s.overall || 'unknown').toUpperCase();
  if (s.overall === 'ok')           dot.dataset.state = 'ok';
  else if (s.overall === 'warning') dot.dataset.state = 'warning';
  else                              dot.dataset.state = 'unknown';
}

function renderMetrics(m) {
  const dPct = m.disk?.usage_pct ?? 0;
  const mPct = m.memory?.usage_pct ?? 0;
  $('#metric-disk-pct').textContent = dPct;
  $('#metric-mem-pct').textContent = mPct;
  setBar($('#metric-disk-fill'), dPct, 85, 95);
  setBar($('#metric-mem-fill'),  mPct, 90, 95);

  const load = parseFloat(m.load_average_1m || 0);
  $('#metric-load-val').textContent = isFinite(load) ? load.toFixed(2) : '—';
  setBar($('#metric-load-fill'), Math.min(100, load * 25), 75, 90);

  $('#net-ping').textContent = m.network?.ping_8_8_8_8 ? 'ok' : 'down';
  $('#net-dns').textContent  = m.network?.dns_resolve_google_com ? 'ok' : 'down';
}

function renderTasks(payload) {
  const tasks = payload.tasks || [];
  const status = $('#filter-status').value;
  const type   = $('#filter-type').value;

  const filtered = tasks.filter(t =>
    (!status || t.status === status) &&
    (!type   || t.task_type === type)
  );

  $('#task-count').textContent = `${filtered.length} task${filtered.length === 1 ? '' : 's'}`;
  const tbody = $('#task-rows');
  tbody.innerHTML = '';
  for (const t of filtered.slice(0, TASK_LIMIT)) {
    const tr = document.createElement('tr');
    const pill = document.createElement('span');
    pill.className = `pill ${safeText(t.status)}`;
    pill.textContent = safeText(t.status);

    tr.append(
      td(t.task_id),
      td(t.task_type),
      td(t.priority ?? '—'),
      tdEl(pill),
      td(t.attempts ?? '—'),
      td(t.completed_at || t.escalated_at || '—'),
      td(t.error || '')
    );
    tbody.append(tr);
  }

  // Performance Review derived from history payload.
  const total = tasks.length;
  const completed = tasks.filter(t => t.status === 'complete').length;
  const failed    = tasks.filter(t => t.status === 'failed' || t.status === 'quarantined').length;
  const escalated = tasks.filter(t => t.status === 'escalated').length;
  const denom = completed + failed;
  $('#perf-completion').textContent = denom ? fmtPct(100 * completed / denom) : '—';
  $('#perf-error').textContent      = denom ? fmtPct(100 * failed    / denom) : '—';
  $('#perf-escalate').textContent   = denom ? fmtPct(100 * escalated / denom) : '—';
  $('#perf-total').textContent = String(total);
}

function renderAlerts(a) {
  const list = $('#alert-list');
  list.innerHTML = '';
  const items = a.escalations || [];

  if (a.quarantined_components?.length) {
    const li = document.createElement('li');
    li.className = 'warn';
    li.append(strong('Quarantined: '), text(a.quarantined_components.join(', ')),
              br(), small(`since ${a.last_quarantine_at || 'unknown'}`));
    list.append(li);
  }

  if (!items.length && !a.quarantined_components?.length) {
    const li = document.createElement('li');
    li.className = 'info';
    li.textContent = 'No alerts. SENTINEL is happy.';
    list.append(li);
    return;
  }

  for (const it of items.slice(0, 25)) {
    const li = document.createElement('li');
    if (it.status === 'escalated') li.className = 'warn';
    li.append(
      strong(`${it.task_id} `),
      text(`(${it.task_type}) ${it.status}${it.severity ? ' · ' + it.severity : ''}`),
      br(),
      small(`${it.completed_at || it.escalated_at || ''}${it.error ? ' · ' + it.error : ''}${it.reason ? ' · ' + it.reason : ''}`)
    );
    list.append(li);
  }
}

// tiny dom helpers (keeps innerHTML out of the picture)
function td(t) { const el = document.createElement('td'); el.textContent = safeText(t); return el; }
function tdEl(child) { const el = document.createElement('td'); el.append(child); return el; }
function strong(t) { const e = document.createElement('strong'); e.textContent = safeText(t); return e; }
function small(t)  { const e = document.createElement('small');  e.textContent = safeText(t); return e; }
function text(t)   { return document.createTextNode(safeText(t)); }
function br()      { return document.createElement('br'); }

// --- main loop ------------------------------------------------------------

async function refresh() {
  try {
    const [s, m, t, a] = await Promise.all([
      getJSON('/api/status'),
      getJSON('/api/metrics'),
      getJSON('/api/tasks'),
      getJSON('/api/alerts'),
    ]);
    renderStatus(s);
    renderMetrics(m);
    renderTasks(t);
    renderAlerts(a);
    $('#last-refresh').textContent = `updated ${new Date().toLocaleTimeString()}`;
  } catch (err) {
    console.error('refresh failed:', err);
    $('#last-refresh').textContent = `update failed: ${err.message}`;
    $('#status-dot').dataset.state = 'err';
    $('#overall').textContent = 'API UNREACHABLE';
  }
}

document.addEventListener('DOMContentLoaded', () => {
  refresh();
  setInterval(refresh, REFRESH_MS);
  $('#filter-status').addEventListener('change', () => {
    // re-render from cached last fetch — cheap path is to re-fetch tasks only.
    getJSON('/api/tasks').then(renderTasks).catch(() => {});
  });
  $('#filter-type').addEventListener('change', () => {
    getJSON('/api/tasks').then(renderTasks).catch(() => {});
  });
});
