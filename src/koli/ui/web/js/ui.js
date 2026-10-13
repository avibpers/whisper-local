// ui.js
// Shared controls for every screen: icons, toggle, segmented, select, number,
// text and range fields that save on commit, tag input, shortcut capture, key
// caps, and the toast and modal hosts. Each control is accessible: a real
// <button>/<input>, a label, keyboard reachable.

import { html, useEffect, useRef, useState } from '../vendor/preact-htm.module.js';
import { call } from './api.js';
import { app, dismissToast, prettyKeys } from './store.js';

// ── Icons ─────────────────────────────────────────────────────────────────

const PATHS = {
  wave: '<path d="M4 12h1M8 9v6M12 5v14M16 8v8M20 11v2"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  book: '<path d="M5 4.5h10a3 3 0 0 1 3 3v12H8a3 3 0 0 1-3-3z"/><path d="M5 16.5a3 3 0 0 1 3-3h10"/>',
  command: '<path d="M5 7l5 5-5 5"/><path d="M12 17h7"/>',
  grid: '<rect x="4" y="4" width="7" height="7" rx="1.5"/><rect x="13" y="4" width="7" height="7" rx="1.5"/><rect x="4" y="13" width="7" height="7" rx="1.5"/><rect x="13" y="13" width="7" height="7" rx="1.5"/>',
  sliders: '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>',
  pulse: '<path d="M3 12h4l2-5 4 10 2-5h6"/>',
  home: '<path d="M4 11l8-6.5 8 6.5"/><path d="M6 10v9h12v-9"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="M16 16l4 4"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
  moon: '<path d="M19 14.5A7.5 7.5 0 1 1 9.5 5a6 6 0 0 0 9.5 9.5z"/>',
  mic: '<rect x="9" y="3.5" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5v3"/>',
  copy: '<rect x="8" y="8" width="11" height="11" rx="2"/><path d="M5 15V6a1 1 0 0 1 1-1h9"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  refresh: '<path d="M19 12a7 7 0 1 1-2.05-4.95M19 4.5V8h-3.5"/>',
  pause: '<path d="M9 6v12M15 6v12"/>',
  play: '<path d="M8 5.5v13l10-6.5z"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  trash: '<path d="M5 7h14M10 7V5h4v2M7 7l1 12h8l1-12"/>',
  download: '<path d="M12 4v11M7.5 10.5L12 15l4.5-4.5M5 19.5h14"/>',
  alert: '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17h0"/>',
  lock: '<rect x="5" y="11" width="14" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  pen: '<path d="M4 20l4-1 11-11-3-3L5 16z"/>',
  folder: '<path d="M4 7a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/>',
  dots: '<circle cx="6" cy="12" r="1.2"/><circle cx="12" cy="12" r="1.2"/><circle cx="18" cy="12" r="1.2"/>',
  grip: '<circle cx="9" cy="7" r="1"/><circle cx="15" cy="7" r="1"/><circle cx="9" cy="12" r="1"/><circle cx="15" cy="12" r="1"/><circle cx="9" cy="17" r="1"/><circle cx="15" cy="17" r="1"/>',
  up: '<path d="M7 14l5-5 5 5"/>',
  down: '<path d="M7 10l5 5 5-5"/>',
  doc: '<path d="M6 3.5h8l4 4v13H6z"/><path d="M14 3.5v4h4M9 12h6M9 16h6"/>',
  people: '<circle cx="9" cy="8" r="3.2"/><path d="M3.5 19a5.5 5.5 0 0 1 11 0"/><circle cx="16.5" cy="9" r="2.6"/><path d="M15 14.2a4.5 4.5 0 0 1 5.5 4.3"/>',
  // Window buttons, drawn thin like the Windows caption buttons.
  'win-min': '<path d="M6.5 12h11"/>',
  'win-max': '<rect x="6.5" y="6.5" width="11" height="11" rx="1.2"/>',
  'win-restore': '<rect x="6.5" y="9" width="8.5" height="8.5" rx="1.2"/><path d="M9 9V7.7c0-.66.54-1.2 1.2-1.2h6.1c.66 0 1.2.54 1.2 1.2v6.1c0 .66-.54 1.2-1.2 1.2H15"/>',
};

export function Icon({ name, size = '', label = '' }) {
  return html`<svg class=${'ic ' + size} viewBox="0 0 24 24" aria-hidden=${label ? 'false' : 'true'}
    role=${label ? 'img' : undefined} aria-label=${label || undefined}
    dangerouslySetInnerHTML=${{ __html: PATHS[name] || '' }}></svg>`;
}

// ── Basic controls ────────────────────────────────────────────────────────

export function Toggle({ checked, onChange, label, disabled = false, id }) {
  return html`<input type="checkbox" class="sw" id=${id} checked=${!!checked} disabled=${disabled}
    aria-label=${label} onChange=${(e) => onChange(e.currentTarget.checked)} />`;
}

export function Segmented({ value, options, onChange, label }) {
  return html`<div class="seg" role="radiogroup" aria-label=${label}>
    ${options.map(([v, text]) => html`
      <button type="button" role="radio" aria-checked=${String(v) === String(value)}
        class=${String(v) === String(value) ? 'on' : ''} onClick=${() => onChange(v)}>${text}</button>`)}
  </div>`;
}

export function Select({ value, options, onChange, label, style = '' }) {
  const known = options.some(([v]) => String(v) === String(value));
  return html`<select class="sel" aria-label=${label} style=${style}
      onChange=${(e) => onChange(options[e.currentTarget.selectedIndex - (known ? 0 : 1)][0])}>
    ${!known && html`<option selected>${value == null ? '—' : String(value)}</option>`}
    ${options.map(([v, text]) => html`<option selected=${String(v) === String(value)}>${text}</option>`)}
  </select>`;
}

// A text field that saves when you press Enter or leave the field, not on
// every keystroke (saves are instant, but half-typed values shouldn't be).
// The draft is its own state: the app window re-renders on every log line
// and event, and a field showing the saved value would lose what you type.
export function TextField({ value, onCommit, label, placeholder = '', mono = false, width = '', type = 'text' }) {
  const [draft, setDraft] = useState(value ?? '');
  useEffect(() => setDraft(value ?? ''), [value]);
  const commit = () => { if ((draft ?? '') !== (value ?? '')) onCommit(draft); };
  return html`<input class=${'inp' + (mono ? ' mono' : '')} type=${type} aria-label=${label} dir="auto"
    placeholder=${placeholder} value=${draft} style=${width ? `width:${width}` : ''}
    onInput=${(e) => setDraft(e.currentTarget.value)} onBlur=${commit}
    onKeyDown=${(e) => { if (e.key === 'Enter') { e.currentTarget.blur(); } if (e.key === 'Escape') setDraft(value ?? ''); }} />`;
}

export function TextArea({ value, onCommit, label, rows = 3, mono = false, placeholder = '' }) {
  const [draft, setDraft] = useState(value ?? '');
  useEffect(() => setDraft(value ?? ''), [value]);
  return html`<textarea class=${'inp' + (mono ? ' mono' : '')} rows=${rows} aria-label=${label} dir="auto"
    placeholder=${placeholder} value=${draft} onInput=${(e) => setDraft(e.currentTarget.value)}
    onBlur=${() => { if (draft !== (value ?? '')) onCommit(draft); }}></textarea>`;
}

export function NumberField({ value, onCommit, label, min, max, step = 1, unit = '' }) {
  const [draft, setDraft] = useState(value ?? '');
  const [err, setErr] = useState('');
  useEffect(() => { setDraft(value ?? ''); setErr(''); }, [value]);
  const commit = () => {
    if (String(draft) === String(value ?? '')) return;
    const n = Number(draft);
    if (draft === '' || Number.isNaN(n)) { setErr('Enter a number'); return; }
    if (min != null && n < min) { setErr(`At least ${min}`); return; }
    if (max != null && n > max) { setErr(`At most ${max}`); return; }
    setErr('');
    onCommit(n);
  };
  return html`<span class="row-flex">
    <input class=${'inp num mono' + (err ? ' err' : '')} inputmode="decimal" aria-label=${label}
      value=${draft} onInput=${(e) => setDraft(e.currentTarget.value)} onBlur=${commit}
      onKeyDown=${(e) => e.key === 'Enter' && e.currentTarget.blur()} />
    ${unit && html`<span class="muted">${unit}</span>`}
    ${err && html`<span class="field-err">${err}</span>`}
  </span>`;
}

export function RangeField({ value, onCommit, label, min, max, step, unit = '' }) {
  const [draft, setDraft] = useState(value ?? min);
  useEffect(() => setDraft(value ?? min), [value]);
  const shown = Number(draft) % 1 === 0 ? Number(draft) : Number(draft).toFixed(2);
  return html`<span class="row-flex">
    <input type="range" class="rng" min=${min} max=${max} step=${step} value=${draft} aria-label=${label}
      onInput=${(e) => setDraft(Number(e.currentTarget.value))}
      onChange=${(e) => onCommit(Number(e.currentTarget.value))} />
    <span class="mono" style="min-width:42px">${shown}${unit}</span>
  </span>`;
}

export function TagInput({ values, onChange, label, placeholder = 'Add, then Enter', locked = [] }) {
  const [draft, setDraft] = useState('');
  const add = () => {
    const v = draft.trim();
    if (!v) return;
    if (!(values || []).some((x) => x.toLowerCase() === v.toLowerCase())) onChange([...(values || []), v]);
    setDraft('');
  };
  return html`<div class="tags">
    ${locked.map((v) => html`<span class="tag" style="padding-inline-end:10px" dir="auto">${v}</span>`)}
    ${(values || []).map((v) => html`<span class="tag" dir="auto">${v}
      <button type="button" aria-label=${`Remove ${v}`} onClick=${() => onChange(values.filter((x) => x !== v))}>×</button></span>`)}
    <input class="inp" style="height:28px;width:150px" aria-label=${label} placeholder=${placeholder}
      value=${draft} dir="auto" onInput=${(e) => setDraft(e.currentTarget.value)}
      onKeyDown=${(e) => { if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); add(); } }} onBlur=${add} />
  </div>`;
}

// Tags picked from a fixed list ("Languages I speak"): the chosen ones as
// removable tags, and a dropdown with the rest to add one.
export function ChoiceTags({ values, options, onChange, label, placeholder = 'Add…' }) {
  const chosen = values || [];
  const labelOf = (v) => (options.find(([value]) => value === v) || [v, v])[1];
  const rest = options.filter(([v]) => !chosen.includes(v));
  return html`<div class="tags">
    ${chosen.map((v) => html`<span class="tag" dir="auto">${labelOf(v)}
      <button type="button" aria-label=${`Remove ${labelOf(v)}`} onClick=${() => onChange(chosen.filter((x) => x !== v))}>×</button></span>`)}
    ${rest.length > 0 && html`<select class="sel" style="height:28px;width:150px" aria-label=${label}
        onChange=${(e) => { const i = e.currentTarget.selectedIndex - 1; e.currentTarget.selectedIndex = 0; if (i >= 0) onChange([...chosen, rest[i][0]]); }}>
      <option selected>${placeholder}</option>
      ${rest.map(([, text]) => html`<option>${text}</option>`)}
    </select>`}
  </div>`;
}

export function Keys({ hotkey, empty = 'Not set' }) {
  const keys = prettyKeys(hotkey);
  if (!keys.length) return html`<span class="muted small">${empty}</span>`;
  return html`<span class="keys">${keys.map((k) => html`<span class="kbd">${k}</span>`)}</span>`;
}

// Press-to-record shortcut field. Global hotkeys are paused while it listens,
// so pressing the current record shortcut doesn't start a dictation.
const MOD = { Control: 'ctrl', Alt: 'alt', Shift: 'shift', Meta: 'win', OS: 'win' };
const ORDER = ['ctrl', 'alt', 'shift', 'win'];
function keyName(e) {
  const c = e.code || '';
  if (c.startsWith('Key')) return c.slice(3).toLowerCase();
  if (c.startsWith('Digit')) return c.slice(5);
  if (/^F\d+$/.test(c)) return c.toLowerCase();
  const map = { Space: 'space', Escape: 'esc', Enter: 'enter', Tab: 'tab', Backspace: 'backspace',
    Delete: 'delete', Insert: 'insert', Home: 'home', End: 'end', PageUp: 'pageup', PageDown: 'pagedown',
    ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right',
    // Punctuation by physical key, so Shift+` records as shift+` (not "~")
    // and a non-English layout records the same key names.
    Backquote: '`', Minus: '-', Equal: '=', BracketLeft: '[', BracketRight: ']', Backslash: '\\',
    Semicolon: ';', Quote: "'", Comma: ',', Period: '.', Slash: '/' };
  return map[c] || (e.key && e.key.length === 1 ? e.key.toLowerCase() : null);
}

// Shortcuts the OS keeps for itself ([{hotkey, label}]), shared by every
// recorder. Fetched again each time a recording starts, so a change in the
// Windows settings shows up without restarting the app.
let osReservedRequest = null;
function loadOsReserved(fresh = false) {
  if (!osReservedRequest || fresh) osReservedRequest = call('hotkeys_reserved').catch(() => []);
  return osReservedRequest;
}

const hotkeyParts = (hotkey) => String(hotkey || '').toLowerCase().split('+').map((k) => k.trim()).filter(Boolean);
const sameKeys = (a, b) => a.length === b.length && a.every((k) => b.includes(k));
const isModifier = (key) => ORDER.includes(key);
const prettyHotkey = (hotkey) => prettyKeys(hotkey).join('+');

// How a shortcut clashes with the OS ones: `exact` are the same keys,
// `swallowed` are OS shortcuts that start with exactly these modifiers. A
// modifier-only recording with a swallowed match means the OS grabbed the last
// key (Alt+Shift+1 records as Alt+Shift).
function osClashes(value, reserved) {
  const keys = hotkeyParts(value);
  if (!keys.length) return { exact: [], swallowed: [] };
  const modifierOnly = keys.every(isModifier);
  const exact = [];
  const swallowed = [];
  for (const entry of reserved) {
    const parts = hotkeyParts(entry.hotkey);
    if (sameKeys(parts, keys)) exact.push(entry);
    else if (modifierOnly && parts.some((k) => !isModifier(k)) && sameKeys(parts.filter(isModifier), keys)) swallowed.push(entry);
  }
  return { exact, swallowed };
}

// "Alt+Shift to switch the keyboard language and Alt+Shift+1 to switch to …"
function describeOsShortcuts(entries) {
  const uses = entries.map((e) => `${prettyHotkey(e.hotkey)} to ${e.label}`);
  return uses.length > 1 ? `${uses.slice(0, -1).join(', ')} and ${uses[uses.length - 1]}` : uses[0];
}

const OS_HOTKEY_SETTINGS = 'Windows Settings → Time & language → Typing → Advanced keyboard settings → Input language hot keys';

// The note under a recorder whose keys clash with the OS. For a shortcut Koli
// listens for, any clash is a problem. For keys a command presses, the OS
// action may be the point (press Alt+Shift+1 to switch to English), so only a
// swallowed key matters, and each candidate is offered as a one-click fix.
function OsClashHint({ value, reserved, sendsKeys, onPick }) {
  const { exact, swallowed } = osClashes(value, reserved);
  const style = 'flex-basis:100%;margin-top:0';
  if (sendsKeys) {
    if (!swallowed.length) return null;
    return html`<span class="field-warn row-flex" role="note" style=${style}>
      <span>Windows caught the last key: it uses ${describeOsShortcuts(swallowed)}.</span>
      ${swallowed.map((e) => html`<button type="button" class="btn sm" onClick=${() => onPick(e.hotkey)}>Use ${prettyHotkey(e.hotkey)}</button>`)}
    </span>`;
  }
  const taken = [...exact, ...swallowed];
  if (!taken.length) return null;
  return html`<span class="field-warn" role="note" style=${style}>
    Taken by Windows: it uses ${describeOsShortcuts(taken)}, so this shortcut won't work as expected.
    Pick other keys, or change them in ${OS_HOTKEY_SETTINGS}.
  </span>`;
}

// `sendsKeys`: the keys are pressed by Koli (a command step), not a shortcut
// Koli listens for. It changes what the clash hint says.
export function HotkeyCapture({ value, onCommit, label, nullable = false, sendsKeys = false }) {
  const [listening, setListening] = useState(false);
  const [osReserved, setOsReserved] = useState([]);
  const pressed = useRef(new Set());
  const mods = useRef(new Set());
  const ref = useRef(null);

  useEffect(() => {
    let alive = true;
    loadOsReserved().then((list) => alive && setOsReserved(list || []));
    return () => { alive = false; };
  }, []);

  const stop = (result) => {
    setListening(false);
    call('dictation_pause', false).catch(() => {});
    if (result !== undefined) onCommit(result);
  };
  const start = () => {
    pressed.current = new Set();
    mods.current = new Set();
    setListening(true);
    call('dictation_pause', true).catch(() => {});
    loadOsReserved(true).then((list) => setOsReserved(list || []));
    setTimeout(() => ref.current && ref.current.focus(), 0);
  };
  const combo = (key) => [...ORDER.filter((m) => mods.current.has(m)), ...(key ? [key] : [])].join('+');

  const onKeyDown = (e) => {
    if (!listening) return;
    e.preventDefault();
    const mod = MOD[e.key];
    if (mod) { mods.current.add(mod); pressed.current.add(mod); return; }
    const name = keyName(e);
    if (name) stop(combo(name));
  };
  const onKeyUp = (e) => {
    if (!listening) return;
    e.preventDefault();
    const mod = MOD[e.key];
    if (mod) pressed.current.delete(mod);
    // Modifier-only shortcuts (like Ctrl+Win) finish when everything is released.
    if (pressed.current.size === 0 && mods.current.size) stop(combo(null));
  };

  return html`<span class="row-flex">
    <button type="button" ref=${ref} class=${'btn capture' + (listening ? ' listening' : '')}
      aria-label=${`${label}: ${value || 'not set'}. Activate to change.`}
      onClick=${() => (listening ? stop() : start())} onKeyDown=${onKeyDown} onKeyUp=${onKeyUp}
      onBlur=${() => listening && stop()}>
      ${listening ? html`<span class="muted">Press the new keys…</span>` : html`<${Keys} hotkey=${value} />`}
    </button>
    ${nullable && value && !listening && html`<button type="button" class="btn ghost sm" onClick=${() => onCommit(null)}>Clear</button>`}
    ${!listening && html`<${OsClashHint} value=${value} reserved=${osReserved} sendsKeys=${sendsKeys} onPick=${onCommit} />`}
  </span>`;
}

// ── Hosts ─────────────────────────────────────────────────────────────────

export function Toasts() {
  const toasts = app.use((s) => s.toasts);
  return html`<div class="toasts" role="status" aria-live="polite">
    ${toasts.map((t) => html`<div class=${'toast' + (t.kind === 'err' ? ' err' : '')}>
      <${Icon} name=${t.kind === 'err' ? 'alert' : t.kind === 'warn' ? 'alert' : 'check'} />
      <span class="grow" dir="auto">${t.message}</span>
      ${t.action && html`<button type="button" class="btn ghost sm" onClick=${() => { dismissToast(t.id); t.action.run(); }}>${t.action.label}</button>`}
      <button type="button" class="btn ghost icon sm" aria-label="Dismiss" onClick=${() => dismissToast(t.id)}><${Icon} name="x" size="sm" /></button>
    </div>`)}
  </div>`;
}

export function ModalHost() {
  const modal = app.use((s) => s.modal);
  const [text, setText] = useState('');
  useEffect(() => { if (modal && modal.kind === 'prompt') setText(modal.value || ''); }, [modal]);
  // Keyboard: focus moves into the dialog, Tab stays inside it, Esc closes
  // it, and focus goes back to whatever opened it.
  useEffect(() => {
    if (!modal) return undefined;
    const opener = document.activeElement;
    const focusables = () => [...document.querySelectorAll('.scrim a[href], .scrim button:not([disabled]), .scrim input:not([disabled]), .scrim select:not([disabled]), .scrim textarea:not([disabled])')];
    const first = setTimeout(() => {
      const scrim = document.querySelector('.scrim');
      if (scrim && !scrim.contains(document.activeElement)) {
        const list = focusables();
        (list.find((el) => el.matches('input, textarea, select')) || list[0] || scrim).focus();
      }
    }, 0);
    const onKey = (e) => {
      if (e.key === 'Escape') {
        if (modal.resolve) modal.resolve(modal.kind === 'prompt' ? null : false);
        else app.set({ modal: null });
      } else if (e.key === 'Tab') {
        const list = focusables();
        if (!list.length) return;
        const i = list.indexOf(document.activeElement);
        if (e.shiftKey && i <= 0) { e.preventDefault(); list[list.length - 1].focus(); }
        else if (!e.shiftKey && (i === -1 || i === list.length - 1)) { e.preventDefault(); list[0].focus(); }
      }
    };
    window.addEventListener('keydown', onKey);
    return () => {
      clearTimeout(first);
      window.removeEventListener('keydown', onKey);
      if (opener && opener.isConnected && typeof opener.focus === 'function') opener.focus();
    };
  }, [modal]);
  if (!modal) return null;
  if (typeof modal === 'function') {
    return html`<div class="scrim" onClick=${(e) => e.target === e.currentTarget && app.set({ modal: null })}>${modal()}</div>`;
  }
  const isPrompt = modal.kind === 'prompt';
  return html`<div class="scrim">
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="mtitle">
      <div class="modal-head"><h2 id="mtitle">${modal.title}</h2></div>
      <div class="modal-body">
        ${typeof modal.body === 'function' ? modal.body() : modal.body && html`<p style="margin:0">${modal.body}</p>`}
        ${isPrompt && html`<label class="stack" style="gap:6px"><span class="eyebrow">${modal.label}</span>
          <input class="inp" type=${modal.secret ? 'password' : 'text'} value=${text} placeholder=${modal.placeholder} autofocus dir="auto"
            onInput=${(e) => setText(e.currentTarget.value)}
            onKeyDown=${(e) => e.key === 'Enter' && modal.resolve(text)} /></label>`}
      </div>
      <div class="modal-foot">
        <button type="button" class="btn" onClick=${() => modal.resolve(isPrompt ? null : false)}>${modal.cancel || 'Cancel'}</button>
        <button type="button" class=${'btn ' + (modal.danger ? 'danger' : 'pri')} onClick=${() => modal.resolve(isPrompt ? text : true)}>${modal.confirm}</button>
      </div>
    </div>
  </div>`;
}
