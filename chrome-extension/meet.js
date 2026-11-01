// meet.js
// Runs inside a Google Meet tab. Reads what the page shows, without any
// voice recognition: whether you are in the call (not the lobby), its title,
// who is in it, and Meet's live captions with each speaker's name. Sends it
// all to background.js, which passes it to the Koli app on this computer.
// Meet's class names are obfuscated and change, and its words follow your
// language, so only icon names, data-* attributes and the captions' layout
// are relied on. Design: docs/design/meet-extension.md in the Koli repo.

(() => {
  'use strict';

  // ── Tuning ─────────────────────────────────────────────────────────────────

  const TICK_MS = 1000;            // how often the page is looked at
  const HEARTBEAT_MS = 5000;       // a status at least this often, so Koli knows the tab is alive
  const CAPTION_SEND_MS = 1000;    // a growing caption is sent at most this often
  const CAPTION_SETTLE_MS = 3000;  // an older caption unchanged this long is final (Meet fixes recent ones)
  const NEWEST_IDLE_MS = 8000;     // the newest caption is final after this long without a change
  const LEAVE_TICKS = 3;           // ticks without the hang-up button before you count as gone
  const CAPTIONS_KEY_RETRY_MS = 10000;  // pressing "c" again if captions still aren't on
  const CAPTIONS_KEY_TRIES = 3;
  const MEETING_CODE = /^[a-z]{3}-[a-z]{4}-[a-z]{3}$/;

  // Meet's icons are ligatures: the icon's text is its name, in any language.
  const ICON_SELECTOR = '.google-symbols, .google-material-icons, i.material-icons-extended, i';
  const ICON_CALL_END = 'call_end';
  const ICONS_IN_CALL = ['closed_caption', 'closed_caption_off', 'translate_cc', 'chat', 'chat_bubble'];
  const ICONS_CAPTIONS = ['closed_caption', 'closed_caption_off', 'translate_cc'];
  const CAPTIONS_REGION = 'div[role="region"][tabindex="0"]';

  // What Meet calls you in your own captions, in the languages we know of.
  // Your own name (from the self tile) counts too.
  const SELF_LABELS = new Set(['You', 'you', 'אתה', 'את', 'את/ה', 'אני', 'Tú', 'Vous', 'Du', 'Вы', 'Ты', 'Você',
    'Tu', 'Voi', 'Jij', 'Ty', 'Sen', 'أنت', 'あなた', '您', '你']);
  const JUNK_NAME = /^Google Participant \(|spaces\/|devices\//;

  const code = (location.pathname.split('/')[1] || '').toLowerCase();
  if (!MEETING_CODE.test(code)) return;     // a landing or lookup page, not a call

  // ── State ──────────────────────────────────────────────────────────────────

  const state = {
    joined: false,
    captionsOn: null,          // true / false when known from the button, null when unknown
    captionsWanted: true,      // Koli's answer says whether it wants captions on
    keyTries: 0,
    lastKeyAt: 0,
    selfName: '',
    title: '',
    people: new Map(),         // participant id (or name) -> {id, name, self, present}
    lastStatus: '',
    lastStatusAt: 0,
    missing: 0,                // ticks in a row the hang-up button was gone
  };
  const blocks = new Map();    // caption element -> {id, name, self, text, firstMs, lastMs, sentText, sentAt, final}
  let blockSeq = 0;
  const pageId = Math.random().toString(36).slice(2, 8);   // keeps block ids unique across reloads

  // ── Talking to background.js ───────────────────────────────────────────────

  function send(events, urgent = false) {
    if (!events.length) return;
    try {
      chrome.runtime.sendMessage({ kind: 'events', code, urgent, events }, (reply) => {
        if (chrome.runtime.lastError || !reply) return;
        if (typeof reply.captions_wanted === 'boolean') state.captionsWanted = reply.captions_wanted;
      });
    } catch (e) {
      // The extension was reloaded: this old copy of the script can't talk any more.
    }
  }

  // ── Reading the page ───────────────────────────────────────────────────────

  function icons(name) {
    return [...document.querySelectorAll(ICON_SELECTOR)].filter((el) => el.textContent.trim() === name);
  }

  // Present in the page, shown or not: in a narrow window Meet moves buttons
  // into its overflow menu, and that must not look like leaving.
  function hasIcon(name) {
    return icons(name).length > 0;
  }

  // Someone else's tile with a name: only there once you are let in.
  function remoteTile() {
    return [...document.querySelectorAll('[data-participant-id]')].some((tile) => {
      const name = tile.querySelector('span.notranslate');
      return name && name.textContent.trim() && !tile.querySelector('[data-self-name]');
    });
  }

  // Getting in: the hang-up button AND a captions or chat button (or another
  // person's tile). The lobby has a hang-up button but no captions or chat.
  function inCall() {
    return hasIcon(ICON_CALL_END) && (ICONS_IN_CALL.some(hasIcon) || remoteTile());
  }

  // Still in: the hang-up button is in the page. It goes with the call
  // (the "call ended" screen has none), checked over a few ticks.
  function stillInCall() {
    return hasIcon(ICON_CALL_END);
  }

  // The captions button's own pressed state, when it says; else, once we
  // pressed "c", on (nobody may have spoken yet); else whether the captions
  // area takes space on screen.
  function captionsOn() {
    for (const name of ICONS_CAPTIONS) {
      for (const icon of icons(name)) {
        const button = icon.closest('button');
        const pressed = button && button.getAttribute('aria-pressed');
        if (pressed === 'true') return true;
        if (pressed === 'false') return false;
      }
    }
    if (state.keyTries > 0) return true;
    const region = document.querySelector(CAPTIONS_REGION);
    if (!region) return null;
    return region.offsetHeight > 0;
  }

  // Meet's own shortcut for captions; pressed only when they are known to be off.
  function turnCaptionsOn() {
    if (!state.captionsWanted || state.captionsOn !== false) return;
    const now = Date.now();
    if (state.keyTries >= CAPTIONS_KEY_TRIES || now - state.lastKeyAt < CAPTIONS_KEY_RETRY_MS) return;
    state.keyTries += 1;
    state.lastKeyAt = now;
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'c', code: 'KeyC', bubbles: true }));
  }

  function readTitle() {
    let title = document.title.replace(/^(Google\s+)?Meet\s*[-–:]\s*/i, '').replace(/\s*[-–]\s*Google Meet$/i, '').trim();
    if (title.toLowerCase() === code || /^google meet$/i.test(title)) title = '';
    return title;
  }

  // The tiles: each has data-participant-id; yours holds data-self-name.
  // Everyone seen stays on the list (marked not present once gone), so a
  // person who left early is still an attendee.
  function readPeople() {
    const selfEl = document.querySelector('[data-self-name]');
    if (selfEl) state.selfName = selfEl.getAttribute('data-self-name') || state.selfName;
    const selfTile = selfEl ? selfEl.closest('[data-participant-id]') : null;
    const present = new Set();
    for (const tile of document.querySelectorAll('[data-participant-id]')) {
      // Real ids look like "spaces/…/devices/…": only names are checked for junk.
      const id = tile.getAttribute('data-participant-id') || '';
      if (!id) continue;
      const nameEl = tile.querySelector('span.notranslate');
      const isSelf = tile === selfTile || !!tile.querySelector('[data-self-name]');
      const name = ((nameEl && nameEl.textContent) || (isSelf ? state.selfName : '') || '').trim();
      if (!name || JUNK_NAME.test(name)) continue;   // the effects preview tile has no name
      present.add(id);
      state.people.set(id, { id, name, self: isSelf, present: true });
    }
    for (const [id, person] of state.people) {
      if (!present.has(id) && !id.startsWith('caption:')) person.present = false;
    }
  }

  function isSelfName(name) {
    return SELF_LABELS.has(name) || (!!state.selfName && name === state.selfName);
  }

  // ── Captions ───────────────────────────────────────────────────────────────
  // The captions area holds one block per stretch of one person's speech: a
  // name element followed by a text element that Meet rewrites in place while
  // they talk. The newest block may still change; an older one Meet stops
  // touching after a moment, and then it is final.

  // The layout is learned from the text Meet writes (as TranscripTonic does):
  // a caption's text node sits in the text element, the name element right
  // before it, both in the block, and the blocks side by side in one list.
  let observed = null;          // the captions region being watched
  let captionList = null;       // the element whose children are the blocks
  const textElements = new WeakSet();
  const observer = new MutationObserver((mutations) => {
    for (const m of mutations) {
      const nodes = m.type === 'characterData' ? [m.target] : [...m.addedNodes];
      for (const node of nodes) {
        if (node.nodeType !== Node.TEXT_NODE) continue;
        const textEl = node.parentElement;
        const block = textEl && textEl.previousElementSibling ? textEl.parentElement : null;
        if (block && block.parentElement) {
          textElements.add(textEl);
          captionList = block.parentElement;
        }
      }
    }
  });

  function watchCaptions() {
    const region = document.querySelector(CAPTIONS_REGION);
    if (region === observed) return;
    observer.disconnect();
    observed = region;
    captionList = null;
    if (region) observer.observe(region, { subtree: true, childList: true, characterData: true });
  }

  function captionBlocks() {
    if (!captionList || !captionList.isConnected) return [];
    const found = [];
    for (const el of captionList.children) {
      const text = [...el.querySelectorAll('*')].find((x) => textElements.has(x)) || el.lastElementChild;
      const name = text && text.previousElementSibling;
      if (!text || !name) continue;     // the list's trailing non-caption elements
      const nameText = name.textContent.trim();
      const bodyText = text.textContent.trim();
      if (nameText && bodyText && nameText.length <= 80) found.push({ el, name: nameText, text: bodyText });
    }
    return found;
  }

  function readCaptions() {
    watchCaptions();
    const now = Date.now();
    const events = [];
    const seen = new Set();
    const found = captionBlocks();
    found.forEach((f, index) => {
      seen.add(f.el);
      let block = blocks.get(f.el);
      if (block && f.text.length + 100 < block.full.length) {
        // Meet trims a very long block from the front: what follows is a new block.
        finish(block, events);
        block = newBlock(f, 0, now);
      } else if (block && block.final) {
        // The same person went on talking after the block was handed out: the rest is a new block.
        if (f.text.length <= block.full.length) return;
        block = newBlock(f, block.full.length, now);
      } else if (!block) {
        block = newBlock(f, 0, now);
      }
      const text = f.text.slice(block.offset).trim();
      if (f.text !== block.full) {
        block.full = f.text;
        block.text = text;
        block.lastMs = now;
      }
      const newest = index === found.length - 1;
      if (now - block.lastMs >= (newest ? NEWEST_IDLE_MS : CAPTION_SETTLE_MS)) {
        finish(block, events);
      } else if (block.text !== block.sentText && now - block.sentAt >= CAPTION_SEND_MS) {
        events.push(captionEvent(block, false));
        block.sentText = block.text;
        block.sentAt = now;
      }
    });
    // Blocks that scrolled away are final as they were last seen.
    for (const [el, block] of blocks) {
      if (!seen.has(el)) {
        finish(block, events);
        blocks.delete(el);
      }
    }
    return events;
  }

  // A block of caption element `f`, starting `offset` characters into its text.
  function newBlock(f, offset, now) {
    const block = { id: `${pageId}-${++blockSeq}`, name: f.name, self: isSelfName(f.name), offset, full: '',
                    text: '', firstMs: now, lastMs: now, sentText: '', sentAt: 0, final: false };
    blocks.set(f.el, block);
    if (!block.self && ![...state.people.values()].some((p) => p.name === f.name)) {
      // Someone talking who has no tile on screen (a big meeting): still an attendee.
      state.people.set(`caption:${f.name}`, { id: `caption:${f.name}`, name: f.name, self: false, present: true });
    }
    return block;
  }

  function captionEvent(block, final) {
    return { type: 'caption', code, id: block.id, name: block.name, self: block.self, text: block.text,
             first_ms: block.firstMs, last_ms: block.lastMs, final };
  }

  function finish(block, events) {
    if (block.final) return;
    block.final = true;
    events.push(captionEvent(block, true));
  }

  function finishAll() {
    const events = [];
    for (const block of blocks.values()) finish(block, events);
    blocks.clear();
    return events;
  }

  // ── Status ─────────────────────────────────────────────────────────────────

  function statusEvent() {
    return { type: 'status', code, joined: state.joined, captions: state.captionsOn === true, title: state.title,
             self_name: state.selfName, at_ms: Date.now(), people: [...state.people.values()] };
  }

  function leave() {
    if (!state.joined) return;
    state.joined = false;
    send([...finishAll(), { type: 'left', code, at_ms: Date.now() }], true);
  }

  // ── The loop ───────────────────────────────────────────────────────────────

  function tick() {
    if (state.joined) {
      // Gone only after LEAVE_TICKS ticks in a row without the hang-up button.
      state.missing = stillInCall() ? 0 : (state.missing || 0) + 1;
      if (state.missing >= LEAVE_TICKS) {
        leave();
        return;
      }
      if (state.missing) return;
    } else if (!inCall()) {
      return;
    }
    const justJoined = !state.joined;
    state.joined = true;
    state.captionsOn = captionsOn();
    state.title = readTitle() || state.title;
    readPeople();
    turnCaptionsOn();
    const events = readCaptions();
    const status = statusEvent();
    const summary = JSON.stringify([status.captions, status.title, status.people]);
    const now = Date.now();
    if (justJoined || summary !== state.lastStatus || now - state.lastStatusAt >= HEARTBEAT_MS) {
      events.unshift(status);
      state.lastStatus = summary;
      state.lastStatusAt = now;
    }
    send(events, justJoined);
  }

  // Leaving is seen on the next tick, once the call's buttons are gone (the
  // "call ended" screen). A click on hang-up alone proves nothing: a host
  // gets a "leave or end for everyone?" choice first and may cancel it.
  // Closing the tab or going elsewhere is caught here and in background.js.
  window.addEventListener('pagehide', leave);

  setInterval(tick, TICK_MS);
})();
