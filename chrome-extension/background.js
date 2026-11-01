// background.js
// The extension's service worker: the only part allowed to reach the Koli
// app at http://127.0.0.1:52881 (a Meet page itself can't). It batches what
// meet.js reads, posts it once a second (at once for joining and leaving),
// tells Koli a call ended when its tab closes or leaves Meet, and keeps
// Koli's last answer for the popup. Koli not running is normal: batches wait
// a little and are then dropped, never piling up.

const KOLI = 'http://127.0.0.1:52881';
const HEADERS = { 'Content-Type': 'application/json', 'X-Koli-Extension': '1' };
const FLUSH_MS = 1000;
const MAX_QUEUE = 500;          // events kept while Koli doesn't answer (about a minute of captions)

let queue = [];
let timer = null;
let sending = false;

// ── Talking to Koli ──────────────────────────────────────────────────────────

async function post(events) {
  const response = await fetch(`${KOLI}/meet/events`, {
    method: 'POST', headers: HEADERS, body: JSON.stringify({ events }),
  });
  if (!response.ok) throw new Error(`Koli answered ${response.status}`);
  return response.json();
}

async function flush() {
  timer = null;
  if (sending || !queue.length) return;
  sending = true;
  const batch = queue;
  queue = [];
  try {
    const reply = await post(batch);
    await remember({ koli: true, reply, at: Date.now() });
  } catch (e) {
    // Koli is closed or busy: keep the newest events for the next try.
    queue = batch.concat(queue).slice(-MAX_QUEUE);
    await remember({ koli: false, error: String(e && e.message || e), at: Date.now() });
  } finally {
    sending = false;
    if (queue.length && !timer) timer = setTimeout(flush, FLUSH_MS * 3);
  }
}

function enqueue(events, urgent) {
  queue.push(...events);
  if (queue.length > MAX_QUEUE) queue = queue.slice(-MAX_QUEUE);
  if (urgent) {
    clearTimeout(timer);
    timer = null;
    flush();
  } else if (!timer) {
    timer = setTimeout(flush, FLUSH_MS);
  }
}

async function remember(link) {
  await chrome.storage.session.set({ link });
}

// ── Meet tabs ────────────────────────────────────────────────────────────────
// Which tab is in which call, kept in session storage: the worker may be
// stopped between messages and must still know which call a closed tab had.

async function tabCalls() {
  return (await chrome.storage.session.get('tabs')).tabs || {};
}

async function setTabCall(tabId, code) {
  const tabs = await tabCalls();
  if (code) tabs[tabId] = code; else delete tabs[tabId];
  await chrome.storage.session.set({ tabs });
}

async function tabGone(tabId) {
  const tabs = await tabCalls();
  const code = tabs[tabId];
  if (!code) return;
  await setTabCall(tabId, null);
  enqueue([{ type: 'left', code, at_ms: Date.now() }], true);
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message && message.kind === 'events' && sender.tab) {
    const joined = message.events.some((e) => e.type === 'status' && e.joined);
    const left = message.events.some((e) => e.type === 'left');
    if (joined) setTabCall(sender.tab.id, message.code);
    if (left) setTabCall(sender.tab.id, null);
    enqueue(message.events, message.urgent);
    chrome.storage.session.get('link').then(({ link }) => sendResponse((link && link.reply) || {}));
    return true;   // answers asynchronously
  }
  if (message && message.kind === 'hello') {
    hello().then(sendResponse);
    return true;
  }
  return false;
});

chrome.tabs.onRemoved.addListener((tabId) => { tabGone(tabId); });
chrome.tabs.onUpdated.addListener((tabId, change) => {
  if (change.url && !change.url.startsWith('https://meet.google.com/')) tabGone(tabId);
});

// ── The popup's question: is Koli there, and what is it doing? ───────────────

async function hello() {
  try {
    const response = await fetch(`${KOLI}/meet/hello`, { headers: HEADERS });
    const reply = await response.json();
    await remember({ koli: true, reply, at: Date.now() });
    return { koli: true, reply };
  } catch (e) {
    return { koli: false, error: String(e && e.message || e) };
  }
}
