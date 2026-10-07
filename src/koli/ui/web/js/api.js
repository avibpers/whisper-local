// api.js
// Talks to the Python side: call(name, ...args) runs a Bridge method and
// resolves with its data (or throws its error message); on(topic, fn) receives
// the events the app pushes into window.wl.receive(). With ?mock=1 in the URL
// a fake backend answers instead, so screens can be built in a plain browser.

const listeners = new Map();

export function on(topic, fn) {
  if (!listeners.has(topic)) listeners.set(topic, new Set());
  listeners.get(topic).add(fn);
  return () => listeners.get(topic).delete(fn);
}

export function emit(topic, payload) {
  for (const key of [topic, '*']) {
    const set = listeners.get(key);
    if (!set) continue;
    for (const fn of set) {
      try { fn(payload, topic); } catch (e) { console.error(`listener for ${topic} failed`, e); }
    }
  }
}

window.wl = {
  receive(batch) {
    for (const item of batch || []) emit(item.topic, item.payload);
  },
};

export const isMock = new URLSearchParams(location.search).has('mock');
let backend = null;

const ready = new Promise((resolve) => {
  if (isMock) {
    import('./mock-api.js').then((m) => { backend = m.createMock(emit); resolve(); });
    return;
  }
  const done = () => { backend = window.pywebview.api; resolve(); };
  if (window.pywebview && window.pywebview.api) done();
  else window.addEventListener('pywebviewready', done, { once: true });
});

export async function call(name, ...args) {
  await ready;
  const fn = backend[name];
  if (typeof fn !== 'function') throw new Error(`Unknown action: ${name}`);
  const res = await fn(...args);
  if (!res || res.ok !== true) throw new Error((res && res.error) || 'Something went wrong');
  return res.data;
}

// Page errors go to the app log so they show up in the Live console.
window.addEventListener('error', (e) => {
  if (!isMock) call('js_error', String(e.message), String(e.filename || ''), e.lineno || 0).catch(() => {});
});
window.addEventListener('unhandledrejection', (e) => {
  if (!isMock) call('js_error', String(e.reason && e.reason.message || e.reason), 'promise', 0).catch(() => {});
});
