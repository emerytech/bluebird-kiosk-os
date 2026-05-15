let sessionToken = null;

function toast(msg, kind = 'success') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = `toast show ${kind}`;
  setTimeout(() => (t.className = 'toast'), 2500);
}

async function api(path, opts = {}) {
  opts.headers = opts.headers || {};
  opts.headers['Content-Type'] = 'application/json';
  if (sessionToken) opts.headers['X-Admin-Session'] = sessionToken;
  const r = await fetch(path, opts);
  if (r.status === 401) {
    showLogin();
    throw new Error('unauthorized');
  }
  return r;
}

// ── Shuffled-keypad PIN entry ───────────────────────────────────────────────
// Mirrors the cloud Legacy Wall kiosk PIN UX: 6 dots, 3-column grid, digits
// reshuffled on every open. Auto-submits at 6 chars. No text input means no
// Chromium autofill to fight.

let _pinVal = '';
let _pinSubmitting = false;

function pinShuffleGrid() {
  const grid = document.getElementById('pin-grid');
  if (!grid) return;
  const digits = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9];
  // Fisher–Yates shuffle.
  for (let i = digits.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [digits[i], digits[j]] = [digits[j], digits[i]];
  }
  let html = '';
  for (let k = 0; k < 10; k++) {
    const d = digits[k];
    if (k === 9) {
      // Bottom row: Cancel · last-digit · Backspace
      html += '<button class="pin-key action" data-act="cancel">Cancel</button>';
      html += `<button class="pin-key" data-digit="${d}">${d}</button>`;
      html += '<button class="pin-key action" data-act="back">⌫</button>';
    } else {
      html += `<button class="pin-key" data-digit="${d}">${d}</button>`;
    }
  }
  grid.innerHTML = html;
  grid.querySelectorAll('[data-digit]').forEach(b => {
    b.addEventListener('click', () => pinDigit(b.dataset.digit));
  });
  grid.querySelectorAll('[data-act="back"]').forEach(b => {
    b.addEventListener('click', pinBack);
  });
  grid.querySelectorAll('[data-act="cancel"]').forEach(b => {
    b.addEventListener('click', pinCancel);
  });
}

function pinUpdateDots() {
  for (let i = 0; i < 6; i++) {
    const dot = document.getElementById('pd-' + i);
    if (dot) dot.classList.toggle('filled', i < _pinVal.length);
  }
}

function pinClear() {
  _pinVal = '';
  _pinSubmitting = false;
  const err = document.getElementById('pin-err');
  if (err) err.textContent = '';
  pinUpdateDots();
  pinShuffleGrid();
}

function pinDigit(d) {
  if (_pinSubmitting || _pinVal.length >= 6) return;
  _pinVal += String(d);
  pinUpdateDots();
  if (_pinVal.length >= 6) {
    pinAutoSubmit();
  }
}

function pinBack() {
  if (_pinSubmitting) return;
  _pinVal = _pinVal.slice(0, -1);
  pinUpdateDots();
  const err = document.getElementById('pin-err');
  if (err) err.textContent = '';
}

function pinCancel() {
  pinClear();
}

async function pinAutoSubmit() {
  if (_pinSubmitting) return;
  _pinSubmitting = true;
  const err = document.getElementById('pin-err');
  if (err) err.textContent = '';
  try {
    const r = await fetch('/admin/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pin: _pinVal }),
    });
    const body = await r.json();
    if (!r.ok || !body.ok) {
      if (body.error === 'locked_out') {
        if (err) err.textContent = `Locked out for ${body.retry_after_seconds}s.`;
      } else {
        if (err) err.textContent = 'Invalid PIN.';
      }
      // Reshuffle on failure so the user can't lean on muscle memory.
      pinClear();
      return;
    }
    sessionToken = body.session_token;
    pinClear();
    showPanels();
  } catch (e) {
    if (err) err.textContent = 'Network error.';
    pinClear();
  }
}

function showLogin() {
  document.getElementById('login').style.display = 'block';
  document.getElementById('panels').style.display = 'none';
  pinClear();
}

// Re-shuffle whenever the page regains focus — covers the "user dismissed
// the admin overlay and reopened it" case so the keypad layout is fresh
// every time.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') pinClear();
});
window.addEventListener('pageshow', pinClear);

function showPanels() {
  document.getElementById('login').style.display = 'none';
  document.getElementById('panels').style.display = 'block';
  loadNetwork();
}

function setTab(name) {
  document.querySelectorAll('.tabs button').forEach((b) =>
    b.classList.toggle('active', b.dataset.tab === name)
  );
  document.querySelectorAll('.pane').forEach((p) =>
    p.classList.toggle('active', p.dataset.tab === name)
  );
  if (name === 'network') loadNetwork();
  if (name === 'display') loadDisplay();
  if (name === 'console') consoleLoad();
  if (name === 'kiosk') loadKiosk();
}

// Old login() / btn-login / login-pin removed — the shuffled keypad
// auto-submits via pinAutoSubmit() above.

async function loadNetwork() {
  const list = document.getElementById('net-list');
  list.innerHTML = 'Scanning…';
  const r = await api('/admin/network/status').then((r) => r.json());
  const s = r.status || {};
  document.getElementById('net-status').textContent =
    `Ethernet: ${s.ethernet || '—'}   WiFi: ${s.wifi || '—'}   IP: ${s.ip || '—'}`;
  list.innerHTML = '';
  for (const n of r.networks) {
    const row = document.createElement('div');
    row.className = 'wifi-row';
    row.innerHTML = `
      <div><div class="ssid"></div><div class="meta"></div></div>
      <button class="secondary">Connect</button>
    `;
    row.querySelector('.ssid').textContent = n.ssid;
    row.querySelector('.meta').textContent = `${n.signal}% · ${n.security || 'open'}`;
    row.querySelector('button').addEventListener('click', async () => {
      const password = n.security ? prompt(`Password for ${n.ssid}:`) || '' : '';
      const resp = await api('/admin/network/connect', {
        method: 'POST',
        body: JSON.stringify({ ssid: n.ssid, password }),
      }).then((r) => r.json());
      toast(resp.message || (resp.ok ? 'Connected' : 'Failed'), resp.ok ? 'success' : 'error');
      if (resp.ok) loadNetwork();
    });
    list.appendChild(row);
  }
}

async function loadDisplay() {
  const sel = document.getElementById('disp-output');
  sel.innerHTML = '';
  const r = await api('/admin/display/outputs').then((r) => r.json());
  for (const o of r.outputs) {
    const opt = document.createElement('option');
    opt.value = o.name;
    opt.textContent = `${o.name} (${o.current_mode}, ${o.transform})`;
    sel.appendChild(opt);
  }
}

async function applyDisplay() {
  const body = {
    output: document.getElementById('disp-output').value,
    transform: document.getElementById('disp-rotation').value || null,
    brightness: parseInt(document.getElementById('disp-brightness').value, 10) || null,
  };
  if (!body.transform) delete body.transform;
  if (!body.brightness) delete body.brightness;
  const r = await api('/admin/display/apply', {
    method: 'POST',
    body: JSON.stringify(body),
  }).then((r) => r.json());
  toast(r.messages ? r.messages.join('; ') : 'Applied.', r.ok ? 'success' : 'error');
}

async function loadKiosk() {
  const r = await api('/admin/kiosk/state').then((r) => r.json());
  document.getElementById('k-slug').textContent = r.slug || '—';
  document.getElementById('k-url').textContent = r.url || '—';
  document.getElementById('k-device').textContent = r.device_id || '—';
  document.getElementById('k-version').textContent = r.version || '—';
}

async function restartKiosk() {
  const r = await api('/admin/kiosk/restart', { method: 'POST' }).then((r) => r.json());
  toast(r.message, r.ok ? 'success' : 'error');
}

async function returnToKiosk() {
  // Send the request without waiting on the response — the server will
  // close this Chromium process as part of the call, which kills the
  // socket before a response can come back. fetch().catch() swallows the
  // resulting network error so we don't show a confusing toast.
  const btn = document.getElementById('btn-return-to-kiosk');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Returning to kiosk…';
  }
  try {
    await api('/admin/kiosk/return-to-kiosk', { method: 'POST' });
  } catch (e) {
    // Expected: the admin window is being torn down server-side.
  }
}

async function changeSlug() {
  const slug = prompt('New school slug:');
  if (!slug) return;
  const r = await api('/admin/kiosk/slug', {
    method: 'POST',
    body: JSON.stringify({ slug }),
  });
  if (r.ok) {
    toast('Slug updated. Restart the kiosk to apply.');
    loadKiosk();
  } else {
    const d = await r.json().catch(() => ({}));
    toast(`Failed: ${d.detail || r.status}`, 'error');
  }
}

async function rebootSystem() {
  if (!confirm('Reboot this kiosk now?')) return;
  await api('/admin/system/reboot', { method: 'POST' });
}

async function shutdownSystem() {
  if (!confirm('Shut down this kiosk?')) return;
  await api('/admin/system/shutdown', { method: 'POST' });
}

async function loadLogs() {
  const r = await api('/admin/system/logs?lines=200');
  document.getElementById('logs-output').textContent = await r.text();
}

async function changePin() {
  const newPin = prompt('New 6-digit PIN:');
  if (!newPin) return;
  const r = await api('/admin/system/change-pin', {
    method: 'POST',
    body: JSON.stringify({ new_pin: newPin }),
  });
  if (r.ok) toast('PIN updated.');
  else toast('PIN change failed.', 'error');
}

async function factoryReset() {
  if (!confirm('Factory reset will erase the school slug, PIN, and device ID. Continue?')) return;
  if (!confirm('Really? This kiosk will return to the first-boot wizard.')) return;
  await api('/admin/system/factory-reset', { method: 'POST' });
}

// ── Software updates ────────────────────────────────────────────────────────
//
// "Check for updates" kicks the bluebird-update.service oneshot and then
// polls /admin/system/update-status every 2s. When the unit goes back to
// inactive (or failed), we stop polling and offer a reboot.

let _updatePollTimer = null;

function setUpdateState(label) {
  const el = document.getElementById('update-state');
  if (el) el.textContent = label || '';
}

async function checkForUpdates() {
  if (_updatePollTimer) return;  // already running
  const btn = document.getElementById('btn-sys-update');
  const out = document.getElementById('update-output');
  btn.disabled = true;
  out.style.display = 'block';
  out.textContent = '';
  setUpdateState('starting…');

  let r;
  try {
    r = await api('/admin/system/check-updates', { method: 'POST' });
  } catch (e) {
    setUpdateState('failed to start');
    btn.disabled = false;
    toast('Could not start update: ' + e, 'error');
    return;
  }
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    setUpdateState('failed to start');
    btn.disabled = false;
    toast(body.message || 'Could not start update.', 'error');
    return;
  }

  _updatePollTimer = setInterval(pollUpdateStatus, 2000);
  pollUpdateStatus();
}

async function pollUpdateStatus() {
  let r, body;
  try {
    r = await api('/admin/system/update-status?lines=120');
    body = await r.json();
  } catch (e) {
    return;
  }
  const out = document.getElementById('update-output');
  if (body.log) out.textContent = body.log;
  out.scrollTop = out.scrollHeight;

  const state = body.state || 'unknown';
  setUpdateState('state: ' + state);

  const done = state === 'inactive' || state === 'failed';
  if (done) {
    clearInterval(_updatePollTimer);
    _updatePollTimer = null;
    document.getElementById('btn-sys-update').disabled = false;
    if (state === 'failed') {
      toast('Update failed. See log above.', 'error');
      return;
    }
    // Success — most kiosk-side changes need a reboot to take effect.
    if (confirm('Update complete. Reboot now to apply?')) {
      rebootSystem();
    }
  }
}

document.querySelectorAll('.tabs button').forEach((b) =>
  b.addEventListener('click', () => setTab(b.dataset.tab))
);
document.getElementById('btn-disp-apply').addEventListener('click', applyDisplay);
document.getElementById('btn-kiosk-restart').addEventListener('click', restartKiosk);
document.getElementById('btn-return-to-kiosk').addEventListener('click', returnToKiosk);
document.getElementById('btn-kiosk-slug').addEventListener('click', changeSlug);
document.getElementById('btn-sys-reboot').addEventListener('click', rebootSystem);
document.getElementById('btn-sys-shutdown').addEventListener('click', shutdownSystem);
document.getElementById('btn-sys-logs').addEventListener('click', loadLogs);
document.getElementById('btn-sys-pin').addEventListener('click', changePin);
document.getElementById('btn-sys-reset').addEventListener('click', factoryReset);
document.getElementById('btn-sys-update').addEventListener('click', checkForUpdates);

// ── Console tab (allow-listed diagnostic runner) ────────────────────────────

async function consoleLoad() {
  const buttons = document.getElementById('console-buttons');
  const meta = document.getElementById('console-meta');
  buttons.innerHTML = '<span style="color:var(--bb-muted);font-size:0.85em;">Loading…</span>';
  try {
    const r = await api('/admin/system/diagnostics');
    const data = await r.json();
    const list = data.diagnostics || [];
    meta.textContent = list.length
      ? `${list.length} command${list.length === 1 ? '' : 's'} available`
      : 'No commands cached — tap Refresh to pull from BlueBird.';
    buttons.innerHTML = '';
    for (const d of list) {
      const btn = document.createElement('button');
      btn.className = 'secondary';
      btn.style.textAlign = 'left';
      btn.style.whiteSpace = 'normal';
      btn.innerHTML = `
        <div style="font-weight:600;">${escapeHTML(d.label)}</div>
        ${d.description ? `<div style="font-size:0.78em;color:var(--bb-muted);margin-top:2px;">${escapeHTML(d.description)}</div>` : ''}
      `;
      btn.addEventListener('click', () => consoleRun(d.id, btn));
      buttons.appendChild(btn);
    }
    if (!list.length) {
      const btn = document.createElement('button');
      btn.className = 'secondary';
      btn.textContent = 'Refresh from server';
      btn.addEventListener('click', consoleRefresh);
      buttons.appendChild(btn);
    }
  } catch (e) {
    meta.textContent = 'Load failed.';
    buttons.innerHTML = '';
  }
}

async function consoleRefresh() {
  const meta = document.getElementById('console-meta');
  meta.textContent = 'Refreshing…';
  try {
    const r = await api('/admin/system/diagnostics/refresh', { method: 'POST' });
    const data = await r.json();
    if (!data.ok) toast('Refresh failed — using cached list.', 'error');
  } catch (e) { /* fall through to load */ }
  consoleLoad();
}

async function consoleRun(diagId, btn) {
  const resBox = document.getElementById('console-result');
  const lbl = document.getElementById('console-result-label');
  const cmd = document.getElementById('console-result-cmd');
  const exit = document.getElementById('console-result-exit');
  const out = document.getElementById('console-result-output');
  resBox.style.display = 'block';
  lbl.textContent = 'Running…';
  exit.textContent = '';
  cmd.textContent = '';
  out.textContent = '';
  if (btn) btn.disabled = true;
  try {
    const r = await api('/admin/system/diagnostics/run', {
      method: 'POST',
      body: JSON.stringify({ diag_id: diagId }),
    });
    const d = await r.json();
    lbl.textContent = d.label || 'Result';
    cmd.textContent = '$ ' + (d.cmd || '');
    if (d.error) {
      exit.textContent = `error: ${d.error}`;
      exit.style.color = '#F87171';
      out.textContent = d.stderr || '';
    } else {
      const code = d.exit_code != null ? d.exit_code : '?';
      exit.textContent = `exit ${code}${d.timed_out ? ' · timed out' : ''}`;
      exit.style.color = code === 0 ? '' : '#FBBF24';
      const stdout = d.stdout || '';
      const stderr = d.stderr || '';
      out.textContent = stderr ? `${stdout}\n\n--- stderr ---\n${stderr}` : stdout || '(no output)';
    }
  } catch (e) {
    lbl.textContent = 'Error';
    exit.textContent = String(e);
    exit.style.color = '#F87171';
  } finally {
    if (btn) btn.disabled = false;
  }
}

function escapeHTML(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

document.getElementById('btn-console-refresh').addEventListener('click', consoleRefresh);

setTab('network');
showLogin();
