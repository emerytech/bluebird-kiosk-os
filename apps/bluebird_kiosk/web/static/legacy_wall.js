// Local Legacy Wall slideshow — pulls /legacy-wall/api/media every minute and
// cycles through cached images with a cross-fade. Designed to run forever in
// a Chromium kiosk window with no human input.
//
// State machine:
//   loading → empty (no cached media yet, retry every 30s)
//   loading → showing (cycles forever, re-fetches list every 60s)
//   showing → empty (list went to zero after a re-fetch)

const SLIDE_MS = 6000;            // dwell per image
const LIST_REFRESH_MS = 60000;    // re-poll media list this often
const EMPTY_RETRY_MS = 30000;     // when there's no media, retry list

const els = {
  imgA: document.getElementById('lw-img-a'),
  imgB: document.getElementById('lw-img-b'),
  empty: document.getElementById('lw-empty'),
  caption: document.getElementById('lw-caption'),
  count: document.getElementById('lw-count'),
  status: document.getElementById('lw-status'),
};

const state = {
  media: [],
  idx: 0,
  active: 'a',
  listTimer: null,
  slideTimer: null,
};

// ── Network ─────────────────────────────────────────────────────────────────

async function fetchMedia() {
  try {
    const resp = await fetch('/legacy-wall/api/media', { cache: 'no-store' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const body = await resp.json();
    state.media = Array.isArray(body.media) ? body.media : [];
    setStatus(`${state.media.length} cached photo${state.media.length === 1 ? '' : 's'}`);
  } catch (e) {
    setStatus('Cache unavailable', true);
    state.media = [];
  }
}

function setStatus(text, warn = false) {
  els.status.textContent = text;
  els.status.classList.toggle('warn', !!warn);
}

// ── Rendering ──────────────────────────────────────────────────────────────

function showEmpty() {
  els.empty.classList.add('is-visible');
  els.imgA.classList.remove('is-visible');
  els.imgB.classList.remove('is-visible');
  els.caption.textContent = '';
  els.count.textContent = '';
  clearTimeout(state.slideTimer);
  state.slideTimer = setTimeout(loop, EMPTY_RETRY_MS);
}

function nextSlide() {
  if (!state.media.length) {
    showEmpty();
    return;
  }
  els.empty.classList.remove('is-visible');

  const media = state.media[state.idx % state.media.length];
  state.idx = (state.idx + 1) % state.media.length;

  const incoming = state.active === 'a' ? els.imgB : els.imgA;
  const outgoing = state.active === 'a' ? els.imgA : els.imgB;
  state.active = state.active === 'a' ? 'b' : 'a';

  // Pre-load and only swap visibility once the image is decoded so we
  // never show a half-loaded frame.
  incoming.onload = () => {
    incoming.classList.add('is-visible');
    outgoing.classList.remove('is-visible');
  };
  incoming.onerror = () => {
    // Skip broken image gracefully on the next tick.
    incoming.classList.remove('is-visible');
  };
  incoming.src = `/legacy-wall/media/${media.id}`;

  els.caption.textContent = media.caption || media.alt_text || '';
  els.count.textContent = `${(state.idx === 0 ? state.media.length : state.idx)} of ${state.media.length}`;
}

// ── Loop ────────────────────────────────────────────────────────────────────

async function loop() {
  await fetchMedia();
  nextSlide();
  clearTimeout(state.slideTimer);
  state.slideTimer = setTimeout(advance, SLIDE_MS);
}

function advance() {
  nextSlide();
  state.slideTimer = setTimeout(advance, SLIDE_MS);
}

function scheduleListRefresh() {
  clearInterval(state.listTimer);
  state.listTimer = setInterval(fetchMedia, LIST_REFRESH_MS);
}

// ── Boot ────────────────────────────────────────────────────────────────────

loop();
scheduleListRefresh();
