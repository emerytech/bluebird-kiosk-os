// Touch-friendly on-screen keyboard for the kiosk firstboot wizard and any
// other surface inside a Chromium --kiosk window where no native OSK exists.
//
// Usage:
//   <input data-osk="alphanum">  // shows the 26-letter + 10-digit layout
//   <input data-osk="qwerty">    // shows the full QWERTY + symbols layout
//
// Auto-shows on focus, auto-hides on blur (with a small grace window so
// tapping a key doesn't immediately blur the input and close the keyboard).
// Physical keyboard still works — this widget only inserts via .value
// mutation + dispatched "input" events, so handlers downstream stay happy.

(function () {
  const ROOT_ID = 'osk-root';
  let active = null;          // current target <input>
  let mode = 'alphanum';      // 'alphanum' | 'qwerty'
  let shift = false;          // sticky uppercase / symbols
  let symbols = false;        // toggle to symbols layer (qwerty mode)
  let dismissTimer = null;

  function getRoot() {
    let el = document.getElementById(ROOT_ID);
    if (el) return el;
    el = document.createElement('div');
    el.id = ROOT_ID;
    el.className = 'osk-root';
    el.addEventListener('mousedown', (e) => {
      // Prevent the input from blurring when the user taps the keyboard.
      e.preventDefault();
    });
    document.body.appendChild(el);
    return el;
  }

  // ── Layouts ────────────────────────────────────────────────────────────────

  const ALPHANUM_ROWS = [
    ['1','2','3','4','5','6','7','8','9','0'],
    ['Q','W','E','R','T','Y','U','I','O','P'],
    ['A','S','D','F','G','H','J','K','L'],
    ['Z','X','C','V','B','N','M'],
  ];

  const QWERTY_LOWER = [
    ['1','2','3','4','5','6','7','8','9','0'],
    ['q','w','e','r','t','y','u','i','o','p'],
    ['a','s','d','f','g','h','j','k','l'],
    ['z','x','c','v','b','n','m','-','_','.'],
  ];

  const QWERTY_UPPER = [
    ['!','@','#','$','%','^','&','*','(',')'],
    ['Q','W','E','R','T','Y','U','I','O','P'],
    ['A','S','D','F','G','H','J','K','L'],
    ['Z','X','C','V','B','N','M','-','_','.'],
  ];

  const SYMBOLS = [
    ['~','`','!','@','#','$','%','^','&','*'],
    ['(',')','-','_','=','+','[',']','{','}'],
    ['|','\\',';',':',"'",'"',',','.','<','>'],
    ['/','?','€','£','¥','§','¶','•','…','—'],
  ];

  // ── Render ─────────────────────────────────────────────────────────────────

  function render() {
    const root = getRoot();
    if (!active) {
      root.classList.remove('osk-open');
      return;
    }
    root.classList.add('osk-open');

    let rows;
    if (mode === 'qwerty') {
      rows = symbols ? SYMBOLS : (shift ? QWERTY_UPPER : QWERTY_LOWER);
    } else {
      rows = ALPHANUM_ROWS;
    }

    const parts = [];
    parts.push('<div class="osk-pane">');
    for (let i = 0; i < rows.length; i++) {
      parts.push('<div class="osk-row">');
      for (const key of rows[i]) {
        parts.push(`<button class="osk-key" data-k="${escapeAttr(key)}">${escapeHtml(key)}</button>`);
      }
      parts.push('</div>');
    }

    // Bottom row: differs per mode.
    parts.push('<div class="osk-row osk-row-bottom">');
    if (mode === 'qwerty') {
      parts.push(`<button class="osk-key osk-key-mod ${symbols ? 'on' : ''}" data-act="symbols">${symbols ? 'ABC' : '?123'}</button>`);
      parts.push(`<button class="osk-key osk-key-mod ${shift ? 'on' : ''}" data-act="shift">⇧</button>`);
      parts.push('<button class="osk-key osk-space" data-act="space">space</button>');
    } else {
      parts.push('<button class="osk-key osk-key-mod" data-act="dash">-</button>');
      parts.push('<button class="osk-key osk-space" data-act="space">space</button>');
    }
    parts.push('<button class="osk-key osk-key-back" data-act="back">⌫</button>');
    parts.push('<button class="osk-key osk-key-enter" data-act="enter">↵</button>');
    parts.push('<button class="osk-key osk-key-mod" data-act="hide">⌄</button>');
    parts.push('</div>');
    parts.push('</div>');

    root.innerHTML = parts.join('');
    root.querySelectorAll('[data-k]').forEach(b => {
      b.addEventListener('click', () => insertText(b.dataset.k));
    });
    root.querySelectorAll('[data-act]').forEach(b => {
      b.addEventListener('click', () => action(b.dataset.act));
    });
  }

  // ── Key handlers ───────────────────────────────────────────────────────────

  function insertText(s) {
    if (!active) return;
    const el = active;
    if (el.maxLength > 0 && el.value.length >= el.maxLength) return;
    const start = el.selectionStart ?? el.value.length;
    const end = el.selectionEnd ?? el.value.length;
    el.value = el.value.slice(0, start) + s + el.value.slice(end);
    const newPos = start + s.length;
    try { el.setSelectionRange(newPos, newPos); } catch (e) {}
    el.dispatchEvent(new Event('input', { bubbles: true }));
    // In qwerty mode, auto-clear non-sticky shift after one key.
    if (mode === 'qwerty' && shift) { shift = false; render(); }
  }

  function action(name) {
    if (!active) return;
    switch (name) {
      case 'back': {
        const start = active.selectionStart ?? active.value.length;
        const end = active.selectionEnd ?? active.value.length;
        if (start === end && start > 0) {
          active.value = active.value.slice(0, start - 1) + active.value.slice(end);
          try { active.setSelectionRange(start - 1, start - 1); } catch (e) {}
        } else {
          active.value = active.value.slice(0, start) + active.value.slice(end);
          try { active.setSelectionRange(start, start); } catch (e) {}
        }
        active.dispatchEvent(new Event('input', { bubbles: true }));
        break;
      }
      case 'space': insertText(' '); break;
      case 'dash':  insertText('-'); break;
      case 'shift': shift = !shift; render(); break;
      case 'symbols': symbols = !symbols; shift = false; render(); break;
      case 'enter':
        // Dispatch Enter as a keyboard event so existing keydown listeners
        // can pick it up (license-input has one that calls redeemLicense).
        active.dispatchEvent(new KeyboardEvent('keydown',
          { key: 'Enter', code: 'Enter', bubbles: true }));
        break;
      case 'hide': hide(); break;
    }
  }

  // ── Focus management ──────────────────────────────────────────────────────

  function show(input, m) {
    if (dismissTimer) { clearTimeout(dismissTimer); dismissTimer = null; }
    active = input;
    mode = m || input.dataset.osk || 'alphanum';
    shift = false;
    symbols = false;
    render();
  }

  function hide() {
    active = null;
    render();
  }

  function bindInput(input) {
    if (input.dataset.oskBound === '1') return;
    input.dataset.oskBound = '1';
    input.addEventListener('focus', () => show(input, input.dataset.osk));
    input.addEventListener('blur', () => {
      // Delay hide so a keyboard tap doesn't dismiss before its click fires.
      if (dismissTimer) clearTimeout(dismissTimer);
      dismissTimer = setTimeout(() => { hide(); }, 150);
    });
  }

  function scan() {
    document.querySelectorAll('[data-osk]').forEach(bindInput);
  }

  // Mutation observer so inputs added later (e.g. inside a modal) get bound.
  const mo = new MutationObserver(() => scan());
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      scan();
      mo.observe(document.body, { childList: true, subtree: true });
    });
  } else {
    scan();
    mo.observe(document.body, { childList: true, subtree: true });
  }

  // ── Utility ───────────────────────────────────────────────────────────────

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c =>
      ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;' }[c]));
  }
  function escapeAttr(s) { return escapeHtml(s); }

  // Public API for code that wants to control programmatically.
  window.BBOSK = { show, hide };
})();
