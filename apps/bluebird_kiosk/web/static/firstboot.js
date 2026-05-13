const stepEls = document.querySelectorAll('.step');
let currentStep = 0;
let chosen = { ssid: '', slug: '', pin: '' };

function showStep(idx) {
  stepEls.forEach((el, i) => el.classList.toggle('active', i === idx));
  currentStep = idx;
}

function toast(msg, kind = 'success') {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = `toast show ${kind}`;
  setTimeout(() => (t.className = 'toast'), 2500);
}

async function loadWifi() {
  const list = document.getElementById('wifi-list');
  list.innerHTML = 'Scanning…';
  const r = await fetch('/firstboot/wifi/scan').then((r) => r.json());
  if (r.status && (r.status.ethernet || r.status.wifi)) {
    document.getElementById('wifi-skip').style.display = '';
  }
  list.innerHTML = '';
  for (const n of r.networks) {
    const row = document.createElement('div');
    row.className = 'wifi-row';
    row.innerHTML = `
      <div>
        <div class="ssid"></div>
        <div class="meta"></div>
      </div>
      <button class="secondary">Connect</button>
    `;
    row.querySelector('.ssid').textContent = n.ssid;
    row.querySelector('.meta').textContent = `${n.signal}% · ${n.security || 'open'}`;
    row.querySelector('button').addEventListener('click', () => connectWifi(n));
    list.appendChild(row);
  }
  if (!r.networks.length) list.textContent = 'No networks found.';
}

async function connectWifi(network) {
  let password = '';
  if (network.security) {
    password = prompt(`Password for ${network.ssid}:`) || '';
  }
  toast(`Connecting to ${network.ssid}…`);
  const r = await fetch('/firstboot/wifi/connect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ssid: network.ssid, password }),
  }).then((r) => r.json());
  if (r.ok) {
    chosen.ssid = network.ssid;
    toast('Connected.', 'success');
    showStep(2);
  } else {
    toast(`Failed: ${r.message}`, 'error');
  }
}

async function validateSlug() {
  const slug = document.getElementById('slug-input').value.trim();
  if (!slug) return toast('Enter a school slug.', 'error');
  const r = await fetch('/firstboot/slug/validate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ slug }),
  }).then((r) => r.json());
  if (r.exists) {
    chosen.slug = slug;
    showStep(3);
  } else {
    toast('Unknown school slug, or Legacy Wall not enabled for that school.', 'error');
  }
}

function buildNumpad(targetInputId) {
  const input = document.getElementById(targetInputId);
  const dots = document.getElementById(targetInputId + '-dots');
  const update = () => {
    if (!dots) return;
    const v = input.value;
    dots.querySelectorAll('.pin-dot').forEach((d, i) => {
      d.classList.toggle('filled', i < v.length);
    });
  };
  document.querySelectorAll(`[data-numpad="${targetInputId}"] button`).forEach((b) => {
    b.addEventListener('click', () => {
      const k = b.dataset.key;
      if (k === 'back') {
        input.value = input.value.slice(0, -1);
      } else if (k === 'clear') {
        input.value = '';
      } else if (/^\d$/.test(k) && input.value.length < 6) {
        input.value += k;
      }
      update();
    });
  });
  update();
}

async function finalize() {
  const p1 = document.getElementById('pin-1').value;
  const p2 = document.getElementById('pin-2').value;
  if (p1.length !== 6 || p2.length !== 6) return toast('PIN must be 6 digits.', 'error');
  if (p1 !== p2) return toast('PINs do not match.', 'error');
  chosen.pin = p1;
  const r = await fetch('/firstboot/finalize', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ slug: chosen.slug, pin: chosen.pin }),
  });
  if (r.ok) {
    showStep(4);
    setTimeout(() => {
      fetch('/admin/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pin: chosen.pin }),
      }).finally(() => {
        // Trigger reboot via systemd — graceful exit out of firstboot mode.
        fetch('/admin/system/reboot', {
          method: 'POST',
          headers: { 'X-Admin-Session': 'firstboot-stub' },
        }).catch(() => {});
      });
    }, 3000);
  } else {
    const detail = await r.json().catch(() => ({}));
    toast(`Setup failed: ${detail.detail || r.status}`, 'error');
  }
}

document.getElementById('btn-start').addEventListener('click', () => {
  showStep(1);
  loadWifi();
});
document.getElementById('wifi-skip').addEventListener('click', () => showStep(2));
document.getElementById('btn-slug').addEventListener('click', validateSlug);
document.getElementById('btn-finalize').addEventListener('click', finalize);
buildNumpad('pin-1');
buildNumpad('pin-2');
showStep(0);
