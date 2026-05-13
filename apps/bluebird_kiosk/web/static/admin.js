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

function showLogin() {
  document.getElementById('login').style.display = 'block';
  document.getElementById('panels').style.display = 'none';
}

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
  if (name === 'kiosk') loadKiosk();
}

async function login() {
  const pin = document.getElementById('login-pin').value;
  if (pin.length !== 6) return toast('PIN must be 6 digits.', 'error');
  const r = await fetch('/admin/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pin }),
  });
  const body = await r.json();
  if (!r.ok || !body.ok) {
    if (body.error === 'locked_out') {
      toast(`Locked out for ${body.retry_after_seconds}s.`, 'error');
    } else {
      toast('Invalid PIN.', 'error');
    }
    return;
  }
  sessionToken = body.session_token;
  document.getElementById('login-pin').value = '';
  showPanels();
}

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

document.getElementById('btn-login').addEventListener('click', login);
document.querySelectorAll('.tabs button').forEach((b) =>
  b.addEventListener('click', () => setTab(b.dataset.tab))
);
document.getElementById('btn-disp-apply').addEventListener('click', applyDisplay);
document.getElementById('btn-kiosk-restart').addEventListener('click', restartKiosk);
document.getElementById('btn-kiosk-slug').addEventListener('click', changeSlug);
document.getElementById('btn-sys-reboot').addEventListener('click', rebootSystem);
document.getElementById('btn-sys-shutdown').addEventListener('click', shutdownSystem);
document.getElementById('btn-sys-logs').addEventListener('click', loadLogs);
document.getElementById('btn-sys-pin').addEventListener('click', changePin);
document.getElementById('btn-sys-reset').addEventListener('click', factoryReset);

setTab('network');
showLogin();
