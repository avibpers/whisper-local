// store.js
// Shared state for the app window. `app` holds settings, schema, tasks, logs
// and the last dictation; `live` holds the fast-changing level and voice
// detection values separately, so 20 updates a second only re-render the
// meters, not every screen. Also: settings helpers, toasts and modals.

import { useEffect, useReducer } from '../vendor/preact-htm.module.js';
import { call } from './api.js';

export function createStore(initial) {
  const state = { ...initial };
  const subs = new Set();
  return {
    get: () => state,
    set(patch) {
      Object.assign(state, typeof patch === 'function' ? patch(state) : patch);
      for (const fn of subs) fn();
    },
    use(selector = (s) => s) {
      const [, force] = useReducer((x) => x + 1, 0);
      useEffect(() => {
        subs.add(force);
        return () => subs.delete(force);
      }, []);
      return selector(state);
    },
  };
}

export const app = createStore({
  ready: false, version: '', settings: {}, schema: null, state: { state: 'idle' }, tasks: {},
  logs: [], pipeline: [], foreground: {}, profile: null, recent: [], route: 'live',
  toasts: [], modal: null, undo: false, streaming: '', notice: null, dev: false,
  meeting: { state: 'idle' },   // the meeting panel's state, for the Meetings screen
});

export const live = createStore({ level: 0, vad: null, levels: new Array(48).fill(0) });

// ── Settings ──────────────────────────────────────────────────────────────

export function getPath(obj, path) {
  let node = obj;
  for (const key of path.split('.')) {
    if (node == null || typeof node !== 'object') return undefined;
    node = node[key];
  }
  return node;
}

function setPath(obj, path, value) {
  const keys = path.split('.');
  const copy = { ...obj };
  let node = copy;
  keys.slice(0, -1).forEach((k) => { node[k] = { ...(node[k] || {}) }; node = node[k]; });
  node[keys[keys.length - 1]] = value;
  return copy;
}

export const setting = (path) => getPath(app.get().settings, path);
export const useSetting = (path) => app.use((s) => getPath(s.settings, path));

export function applySettingLocally(path, value) {
  app.set((s) => ({ settings: setPath(s.settings, path, value) }));
}

export function schemaFor(path) {
  const schema = app.get().schema;
  return schema ? schema.settings.find((x) => x.path === path) : null;
}

// Save one setting: optimistic, then confirmed (or rolled back) by the app.
export async function saveSetting(path, value, { quiet = false } = {}) {
  const before = setting(path);
  applySettingLocally(path, value);
  try {
    const res = await call('settings_set', path, value);
    applySettingLocally(path, res.value);
    if (!quiet && res.applied !== 'unchanged') {
      const msg = res.applied === 'background'
        ? `Saved · ${res.label} — applying in the background`
        : res.applied === 'next_launch' ? `Saved · ${res.label} — applies next time you open the app`
        : `Saved · ${res.label}`;
      toast(msg, { action: { label: 'Undo', run: undo } });
    }
    for (const w of res.warnings || []) toast(w, { kind: 'warn', timeout: 7000 });
    return res;
  } catch (e) {
    applySettingLocally(path, before);
    toast(e.message, { kind: 'err', timeout: 7000 });
    throw e;
  }
}

export async function saveMany(patch, label = '') {
  try {
    const res = await call('settings_set_many', patch, label);
    for (const r of res.results || []) applySettingLocally(r.path, r.value);
    if (label) toast(`Saved · ${label}`, { action: { label: 'Undo', run: undo } });
    return res;
  } catch (e) {
    toast(e.message, { kind: 'err' });
    throw e;
  }
}

export async function undo() {
  try {
    const res = await call('settings_undo');
    if (res) toast(`Undone · ${res.label}`);
    else toast('Nothing to undo');
    app.set({ settings: await call('settings_all') });
  } catch (e) {
    toast(e.message, { kind: 'err' });
  }
}

// ── Toasts ────────────────────────────────────────────────────────────────

let toastId = 0;
// `timeout: 0` keeps the toast until it is closed; `onDismiss` runs when it is.
export function toast(message, { kind = 'info', action = null, timeout = 4500, onDismiss = null } = {}) {
  const id = ++toastId;
  app.set((s) => ({ toasts: [...s.toasts.slice(-3), { id, message, kind, action, onDismiss }] }));
  if (timeout) setTimeout(() => dismissToast(id), timeout);
  return id;
}
export function dismissToast(id) {
  const closing = app.get().toasts.find((t) => t.id === id);
  app.set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) }));
  if (closing && closing.onDismiss) closing.onDismiss();
}

// ── Modals (in-page; the app never uses alert/confirm/prompt) ─────────────

export function openModal(render) { app.set({ modal: render }); }
export function closeModal() { app.set({ modal: null }); }

// Resolves true/false. `body` is text or a render function.
export function confirmModal({ title, body, confirm = 'OK', cancel = 'Cancel', danger = false }) {
  return new Promise((resolve) => {
    app.set({ modal: { kind: 'confirm', title, body, confirm, cancel, danger,
      resolve: (v) => { closeModal(); resolve(v); } } });
  });
}

// Resolves the entered text, or null when cancelled.
export function promptModal({ title, label, value = '', placeholder = '', confirm = 'Save' }) {
  return new Promise((resolve) => {
    app.set({ modal: { kind: 'prompt', title, label, value, placeholder, confirm,
      resolve: (v) => { closeModal(); resolve(v); } } });
  });
}

// ── Router ────────────────────────────────────────────────────────────────

export function routeFromHash() {
  const h = location.hash.replace(/^#\/?/, '');
  return h || null;
}

export function navigate(route) {
  if (!route) return;
  if (location.hash !== `#/${route}`) location.hash = `#/${route}`;
  app.set({ route });
}

// ── Formatting helpers ────────────────────────────────────────────────────

export function prettyKeys(hotkey) {
  if (!hotkey) return [];
  return String(hotkey).split('+').map((k) => k.trim()).filter(Boolean).map((k) => ({
    ctrl: 'Ctrl', control: 'Ctrl', alt: 'Alt', shift: 'Shift', win: 'Win', windows: 'Win', cmd: 'Cmd',
    command: 'Cmd', option: 'Option', fn: 'Fn', esc: 'Esc', escape: 'Esc', space: 'Space', enter: 'Enter',
    tab: 'Tab', up: '↑', down: '↓', left: '←', right: '→', '{n}': 'number',
  }[k.toLowerCase()] || (k.length === 1 ? k.toUpperCase() : k.charAt(0).toUpperCase() + k.slice(1))));
}

export function fmtBytes(n) {
  if (!n) return '';
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`;
  if (n >= 1e6) return `${Math.round(n / 1e6)} MB`;
  return `${Math.round(n / 1e3)} KB`;
}

export function fmtNumber(n) {
  return (n || 0).toLocaleString();
}

// Direction for user text: right-to-left when the first strong letter is
// Hebrew/Arabic. (dir="auto" also does this; this helps with alignment.)
export function textDir(text) {
  const m = /[A-Za-zÀ-ɏ֐-ࣿ]/.exec(text || '');
  return m && /[֐-ࣿ]/.test(m[0]) ? 'rtl' : 'ltr';
}
