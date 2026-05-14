// First-boot wizard — explicit per-step navigation.
// Each step has its own primary "Continue →" button. WiFi connections happen
// in-place on Step 1 but no longer auto-advance; the user clicks Continue
// when they're satisfied with the network state.

const stepEls = document.querySelectorAll('.step');
const progressDots = document.querySelectorAll('.dot-step');
const chosen = { ssid: '', slug: '', pin: '' };
let currentStep = 0;

// ── Navigation ──────────────────────────────────────────────────────────────

function showStep(idx) {
  stepEls.forEach((el) => {
    el.classList.toggle('active', Number(el.dataset.step) === idx);
  });
  progressDots.forEach((d) => {
    const n = Number(d.dataset.progressStep);
    d.classList.toggle('done', n < idx);
    d.classList.toggle('current', n === idx);
  });
  currentStep = idx;
  // Per-step entry behavior.
  if (idx === 1) loadNetwork();
  if (idx === 2) document.getElementById('slug-input').focus();
}

// ── Toast ───────────────────────────────────────────────────────────────────

function toast(msg, kind = 'success') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = `toast show ${kind}`;
  setTimeout(() => (t.className = 'toast'), 2500);
}

// ── Step 1: Network ─────────────────────────────────────────────────────────

async function loadNetwork() {
  const list = document.getElementById('wifi-list');
  const summary = document.getElementById('net-summary');
  list.innerHTML = '<div class="muted">Scanning…</div>';
  summary.textContent = 'Checking network status…';

  let r;
  try {
    r = await fetch('/firstboot/wifi/scan').then((r) => r.json());
  } catch (e) {
    summary.textContent = 'Network scan failed.';
    list.innerHTML = '';
    return;
  }

  const status = r.status || {};
  if (status.ethernet) {
    summary.innerHTML = `Connected via Ethernet — <strong>${escapeHTML(status.ethernet)}</strong>. You can continue, or join a WiFi network below if you'd prefer wireless.`;
  } else if (status.wifi) {
    summary.innerHTML = `Connected to WiFi — <strong>${escapeHTML(status.wifi)}</strong>. You're online. Click Continue.`;
  } else {
    summary.textContent = 'No active network. Pick a WiFi network below, or plug in an Ethernet cable.';
  }

  list.innerHTML = '';
  if (!r.networks || !r.networks.length) {
    list.innerHTML = '<div class="muted">No WiFi networks visible.</div>';
    return;
  }
  for (const n of r.networks) {
    const row = document.createElement('div');
    row.className = 'wifi-row';
    row.innerHTML = `
      <div>
        <div class="ssid"></div>
        <div class="meta"></div>
      </div>
      <button class="ghost">Connect</button>
    `;
    row.querySelector('.ssid').textContent = n.ssid;
    const sec = n.security || 'open';
    row.querySelector('.meta').textContent = `${n.signal}% · ${sec}`;
    row.querySelector('button').addEventListener('click', () => connectWifi(n));
    list.appendChild(row);
  }
}

async function connectWifi(network) {
  let password = '';
  if (network.security) {
    password = prompt(`Password for ${network.ssid}:`) || '';
    if (!password) return;
  }
  toast(`Connecting to ${network.ssid}…`);
  let r;
  try {
    r = await fetch('/firstboot/wifi/connect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ssid: network.ssid, password }),
    }).then((r) => r.json());
  } catch (e) {
    toast(`Network error: ${e}`, 'error');
    return;
  }
  if (r.ok) {
    chosen.ssid = network.ssid;
    toast('Connected.', 'success');
    loadNetwork();   // refresh summary, do NOT advance
  } else {
    toast(`Failed: ${r.message}`, 'error');
  }
}

// ── Step 2: School slug ─────────────────────────────────────────────────────

async function validateSlug() {
  const slug = document.getElementById('slug-input').value.trim();
  const hint = document.getElementById('slug-hint');
  hint.textContent = '';
  if (!slug) {
    hint.textContent = 'Please enter a slug.';
    return;
  }
  hint.textContent = 'Checking…';
  let r;
  try {
    r = await fetch('/firstboot/slug/validate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slug }),
    }).then((r) => r.json());
  } catch (e) {
    hint.textContent = `Network error: ${e}`;
    return;
  }
  if (r.exists) {
    chosen.slug = slug;
    hint.textContent = '';
    showStep(3);
  } else {
    hint.textContent = 'Unknown school slug, or Legacy Wall not enabled for that school.';
  }
}

// ── Step 3: PIN ─────────────────────────────────────────────────────────────

function buildNumpad(targetInputId) {
  const input = document.getElementById(targetInputId);
  const dots = document.getElementById(targetInputId + '-dots');
  const refresh = () => {
    if (!dots) return;
    const v = input.value;
    dots.querySelectorAll('.pin-dot').forEach((d, i) => {
      d.classList.toggle('filled', i < v.length);
    });
  };
  document.querySelectorAll(`[data-numpad="${targetInputId}"] button`).forEach((b) => {
    b.addEventListener('click', () => {
      const k = b.dataset.key;
      if (k === 'back') input.value = input.value.slice(0, -1);
      else if (k === 'clear') input.value = '';
      else if (/^\d$/.test(k) && input.value.length < 6) input.value += k;
      refresh();
    });
  });
  refresh();
}

async function finalize() {
  const p1 = document.getElementById('pin-1').value;
  const p2 = document.getElementById('pin-2').value;
  if (p1.length !== 6 || p2.length !== 6) {
    toast('PIN must be 6 digits.', 'error');
    return;
  }
  if (p1 !== p2) {
    toast('PINs do not match.', 'error');
    return;
  }
  chosen.pin = p1;
  let r;
  try {
    r = await fetch('/firstboot/finalize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slug: chosen.slug, pin: chosen.pin }),
    });
  } catch (e) {
    toast(`Network error: ${e}`, 'error');
    return;
  }
  if (!r.ok) {
    const detail = await r.json().catch(() => ({}));
    toast(`Setup failed: ${detail.detail || r.status}`, 'error');
    return;
  }
  showStep(4);
  setTimeout(() => {
    fetch('/admin/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pin: chosen.pin }),
    })
      .then((r) => r.json())
      .then((body) => {
        if (!body || !body.session_token) return;
        return fetch('/admin/system/reboot', {
          method: 'POST',
          headers: { 'X-Admin-Session': body.session_token },
        });
      })
      .catch(() => {});
  }, 3000);
}

// ── Utility ─────────────────────────────────────────────────────────────────

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])
  );
}

// ── Wiring ──────────────────────────────────────────────────────────────────

document.getElementById('btn-start').addEventListener('click', () => showStep(1));
document.getElementById('btn-network-continue').addEventListener('click', () => showStep(2));
document.getElementById('btn-slug').addEventListener('click', validateSlug);
document.getElementById('btn-slug-back').addEventListener('click', () => showStep(1));
document.getElementById('btn-finalize').addEventListener('click', finalize);
document.getElementById('btn-pin-back').addEventListener('click', () => showStep(2));

document.getElementById('slug-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); validateSlug(); }
});

buildNumpad('pin-1');
buildNumpad('pin-2');
showStep(0);
