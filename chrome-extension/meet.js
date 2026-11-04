// meet.js
// Runs inside a Google Meet tab. Reads what the page shows, without any
// voice recognition: whether you are in the call (not the lobby), its title,
// who is in it, and Meet's live captions with each speaker's name. Sends it
// all to background.js, which passes it to the Koli app on this computer.
// Captions come from Meet's own caption channel when meet_channel.js can decode
// it (it keeps flowing while the tab is hidden), else from the page, which
// shows nothing new while the tab is hidden and all of it on return.
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
  // One person talking on and on keeps a single caption block changing: its
  // start goes out as a line once it has stopped changing for
  // CAPTION_SETTLE_MS and holds this many words (the last few stay, Meet
  // still rewrites them). Without this a monologue reached Koli only when
  // it ended (seen live 2026-11-02).
  const PIECE_MIN_WORDS = 12;
  const PIECE_KEEP_WORDS = 4;
  const LEAVE_TICKS = 3;           // ticks without the hang-up button before you count as gone
  const CAPTIONS_KEY_RETRY_MS = 10000;  // trying again if captions still aren't on
  const CAPTIONS_KEY_TRIES = 3;
  // The caption channel. The page changing while the channel has said nothing
  // for this long means the channel is no use: back to reading the page.
  const CHANNEL_SILENT_MS = 20000;
  const CHANNEL_KEEP_MS = 120000;   // a caption not updated for this long is over
  // A speaker the tiles and Meet's people list don't name after this long: on
  // screen, the page's captions (which do name them) are better; hidden, they
  // go out under UNNAMED_SPEAKER, since the page can't be asked.
  const CHANNEL_NAME_WAIT_MS = 10000;
  const UNNAMED_SPEAKER = 'Participant';
  // On return from hidden Meet writes all it held back at once. What appears
  // in this window is that backlog: Whisper covered that time, so it isn't sent.
  const BACKLOG_ABSORB_MS = 5000;
  const MEETING_CODE = /^[a-z]{3}-[a-z]{4}-[a-z]{3}$/;

  // Meet's icons are ligatures: the icon's text is its name, in any language.
  const ICON_SELECTOR = '.google-symbols, .google-material-icons, i.material-icons-extended, i';
  const ICON_CALL_END = 'call_end';
  const ICONS_IN_CALL = ['closed_caption', 'closed_caption_off', 'translate_cc', 'chat', 'chat_bubble'];
  const ICONS_CAPTIONS = ['closed_caption', 'closed_caption_off', 'translate_cc'];
  const CAPTIONS_REGION = 'div[role="region"][tabindex="0"]';
  // Your own tile has controls nobody else's has: Backgrounds and effects,
  // and Reframe (seen in a live call 2026-11-02, where data-self-name was
  // gone). Other people's tiles have Pin and Mute instead.
  const ICONS_SELF_TILE = ['visual_effects', 'frame_person'];

  // What Meet calls you in your own captions, in the languages we know of.
  // Your own name (from the self tile) counts too.
  const SELF_LABELS = new Set(['You', 'you', 'אתה', 'את', 'את/ה', 'אני', 'Tú', 'Vous', 'Du', 'Вы', 'Ты', 'Você',
    'Tu', 'Voi', 'Jij', 'Ty', 'Sen', 'أنت', 'あなた', '您', '你']);
  const JUNK_NAME = /^Google Participant \(|spaces\/|devices\//;

  // The code is anywhere in the path: "/abc-defg-hij" and, from a calendar
  // or another link, "/meet/abc-defg-hij" (seen live 2026-11-02).
  const code = location.pathname.split('/').map((part) => part.toLowerCase())
    .find((part) => MEETING_CODE.test(part)) || '';
  if (!code) return;     // a landing or lookup page, not a call

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
    visible: document.visibilityState === 'visible',
    absorbUntil: 0,            // until then the page's captions are backlog (see BACKLOG_ABSORB_MS)
    pageChangedAt: 0,          // when the page last wrote caption text it wasn't holding back
  };
  // Meet's caption channel, as told by meet_channel.js.
  const channel = {
    live: false,               // captions are read from here (else from the page)
    givenUp: false,            // it failed us in this tab: the page for good
    lastAt: 0,                 // when it last delivered a caption
    captions: new Map(),       // "device:id" -> {device, version, text, final, firstMs, lastMs}
    users: new Map(),          // device -> {name, self}, from Meet's people messages
    selfSeen: false,           // it has carried your own captions
    labels: new Set(),
    counts: { captions: 0, hidden: 0, undecoded: 0 },
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

  // Your own tile: data-self-name where Meet still sets it, else the
  // controls only your tile has.
  function isSelfTile(tile) {
    if (tile.querySelector('[data-self-name]')) return true;
    return [...tile.querySelectorAll(ICON_SELECTOR)].some((el) => ICONS_SELF_TILE.includes(el.textContent.trim()));
  }

  // The person's name on a tile: the first non-empty span.notranslate that
  // isn't an icon. A thumbnail of the same person (same participant id)
  // has an icon ("devices") there instead of the name (seen live).
  function tileName(tile) {
    const iconWords = new Set([...tile.querySelectorAll(ICON_SELECTOR)].map((el) => el.textContent.trim()));
    for (const el of tile.querySelectorAll('span.notranslate')) {
      const text = el.textContent.trim();
      if (text && !iconWords.has(text) && !el.closest(ICON_SELECTOR)) return text;
    }
    return '';
  }

  // Someone else's tile with a name: only there once you are let in.
  function remoteTile() {
    return [...document.querySelectorAll('[data-participant-id]')].some((tile) => tileName(tile) && !isSelfTile(tile));
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

  // The captions button says whether captions are on: its pressed state
  // when it has one; else its icon, which offers the other state (seen live:
  // closed_caption_off with "Turn on captions" while off, closed_caption
  // with "Turn off captions" while on); else whether the captions area takes
  // space on screen.
  function captionsButton() {
    for (const name of ICONS_CAPTIONS) {
      for (const icon of icons(name)) {
        const button = icon.closest('button');
        if (button) return { button, icon: name };
      }
    }
    return null;
  }

  function captionsOn() {
    const found = captionsButton();
    if (found) {
      const pressed = found.button.getAttribute('aria-pressed');
      if (pressed === 'true') return true;
      if (pressed === 'false') return false;
      if (found.icon === 'closed_caption_off') return false;
      if (found.icon === 'closed_caption') return true;
    }
    const region = document.querySelector(CAPTIONS_REGION);
    if (!region) return null;
    return region.offsetHeight > 0;
  }

  // A click as a mouse makes it: Meet's buttons answer pointer and mouse
  // events, and ignore a bare click() and a synthetic "c" key (seen live).
  function press(button) {
    const r = button.getBoundingClientRect();
    const at = { bubbles: true, cancelable: true, composed: true, button: 0, view: window,
                 clientX: r.left + r.width / 2, clientY: r.top + r.height / 2 };
    const pointer = { ...at, pointerId: 1, pointerType: 'mouse', isPrimary: true };
    button.dispatchEvent(new PointerEvent('pointerdown', pointer));
    button.dispatchEvent(new MouseEvent('mousedown', at));
    button.dispatchEvent(new PointerEvent('pointerup', pointer));
    button.dispatchEvent(new MouseEvent('mouseup', at));
    button.dispatchEvent(new MouseEvent('click', at));
  }

  // Turns captions on through their button, only when they are known to be
  // off; tried again a few times if Meet didn't take it.
  function turnCaptionsOn() {
    if (!state.captionsWanted || state.captionsOn !== false) return;
    const now = Date.now();
    if (state.keyTries >= CAPTIONS_KEY_TRIES || now - state.lastKeyAt < CAPTIONS_KEY_RETRY_MS) return;
    state.keyTries += 1;
    state.lastKeyAt = now;
    const found = captionsButton();
    if (found) {
      press(found.button);
    } else {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'c', code: 'KeyC', bubbles: true }));
    }
  }

  function readTitle() {
    // Leading symbols go first: a notification count or a marker another
    // tool puts in the tab title ("🐴 Meet – …").
    // Meet wraps a right-to-left title in invisible direction marks.
    let title = document.title.replace(/[‎‏‪-‮⁦-⁩]/g, '')
      .replace(/^\(\d+\)\s*/, '').replace(/^[^\p{L}\p{N}]+/u, '')
      .replace(/^(Google\s+)?Meet\s*[-–:]\s*/i, '').replace(/\s*[-–]\s*Google Meet$/i, '').trim();
    if (title.toLowerCase() === code || /^google meet$/i.test(title)) title = '';
    return title;
  }

  // The tiles: each has data-participant-id; yours is told by isSelfTile.
  // Everyone seen stays on the list (marked not present once gone), so a
  // person who left early is still an attendee.
  function readPeople() {
    const selfEl = document.querySelector('[data-self-name]');
    if (selfEl) state.selfName = selfEl.getAttribute('data-self-name') || state.selfName;
    const present = new Set();
    for (const tile of document.querySelectorAll('[data-participant-id]')) {
      // Real ids look like "spaces/…/devices/…": only names are checked for junk.
      const id = tile.getAttribute('data-participant-id') || '';
      if (!id) continue;
      if (present.has(id)) continue;   // a thumbnail of someone already read from their tile
      const isSelf = isSelfTile(tile);
      const name = (tileName(tile) || (isSelf ? state.selfName : '') || '').trim();
      if (!name || JUNK_NAME.test(name)) continue;   // the effects preview tile has no name
      if (isSelf) state.selfName = name;   // your name in your own captions
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
          notePageChange();
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

  // ── The caption channel ────────────────────────────────────────────────────
  // meet_channel.js runs in the page's world and speaks through a DOM event.

  document.addEventListener('koli-meet-channel', (event) => {
    let message;
    try {
      message = JSON.parse(event.detail);
    } catch (e) {
      return;
    }
    if (message.kind === 'channel') channel.labels.add(message.label);
    else if (message.kind === 'undecoded') channel.counts.undecoded += 1;
    else if (message.kind === 'users') message.users.forEach((u) => channel.users.set(u.device, u));
    else if (message.kind === 'caption') channelCaption(message.caption);
  });

  // One revision of a caption: Meet resends its whole text, numbered. Messages
  // can arrive late (the channel doesn't retransmit forever): an older one is dropped.
  function channelCaption(c) {
    const now = Date.now();
    channel.counts.captions += 1;
    if (document.visibilityState !== 'visible') channel.counts.hidden += 1;
    channel.lastAt = now;
    if (!channel.givenUp) channel.live = true;
    const key = `${c.device}:${c.id}`;
    const known = channel.captions.get(key);
    if (known && BigInt(c.version || 0) < BigInt(known.version || 0)) return;
    channel.captions.set(key, { device: c.device, version: c.version, text: c.text || (known ? known.text : ''),
                                final: c.final || (known ? known.final : false),
                                firstMs: known ? known.firstMs : now, lastMs: now });
  }

  // The last part of a device id: tiles and captions may not agree on the rest.
  function sameDevice(a, b) {
    return a === b || (a.includes('/devices/') && b.includes('/devices/') && a.split('/devices/')[1] === b.split('/devices/')[1]);
  }

  // Who a device is: a tile's name first (what the page shows), else Meet's people list.
  function channelPerson(device) {
    const tile = [...state.people.values()].find((p) => sameDevice(p.id, device));
    if (tile) return { name: tile.name, self: tile.self };
    const user = [...channel.users.values()].find((u) => sameDevice(u.device, device));
    return user ? { name: user.name, self: user.self } : null;
  }

  // Stop trusting the channel in this tab. The page's captions on show are
  // absorbed first: the channel already delivered them.
  function giveUpChannel(now) {
    channel.live = false;
    channel.givenUp = true;
    state.absorbUntil = now + BACKLOG_ABSORB_MS;
  }

  // The channel's captions, in the same shape as captionBlocks() and oldest
  // first. A block's key is a string here, a page element there.
  function channelBlocks(now) {
    const found = [];
    for (const [key, c] of channel.captions) {
      if (now - c.lastMs > CHANNEL_KEEP_MS) {
        channel.captions.delete(key);
        continue;
      }
      if (!c.text) continue;
      let person = channelPerson(c.device);
      if (!person) {
        if (now - c.firstMs < CHANNEL_NAME_WAIT_MS) continue;    // the next look at the tiles may name them
        if (state.visible) {
          giveUpChannel(now);
          return [];
        }
        person = { name: UNNAMED_SPEAKER, self: false };
      }
      if (person.self) channel.selfSeen = true;
      found.push({ el: key, name: person.self ? (state.selfName || person.name) : person.name, self: person.self,
                   text: c.text, final: c.final, firstMs: c.firstMs });
    }
    return found;
  }

  // ── Which captions are read ────────────────────────────────────────────────

  // The page's captions while the page can be believed: the tab on screen and
  // its backlog gone.
  function pageReadable(now) {
    return state.visible && now >= state.absorbUntil;
  }

  // What Meet writes while we're on screen counts as new; the return's burst doesn't.
  function notePageChange() {
    const now = Date.now();
    if (pageReadable(now)) state.pageChangedAt = now;
  }

  // The page's captions on return from hidden: Whisper covered that time, so
  // they are taken as already handed out. Text that grows later is a new block.
  function absorbBacklog(now) {
    for (const f of captionBlocks()) {
      const block = blocks.get(f.el) || newBlock(f, 0, now);
      block.full = f.text;
      block.final = true;
    }
  }

  // What to read this tick. With the channel, the page is read only for your
  // own captions, until the channel has shown it carries them too.
  function captionsToRead(now) {
    if (channel.live && pageReadable(now) && state.pageChangedAt - channel.lastAt > CHANNEL_SILENT_MS) {
      giveUpChannel(now);
    }
    const fromChannel = channel.live ? channelBlocks(now) : [];
    if (!pageReadable(now)) return fromChannel;
    if (!channel.live) return captionBlocks();
    if (channel.selfSeen) return fromChannel;
    return [...fromChannel, ...captionBlocks().filter((f) => isSelfName(f.name))];
  }

  function readCaptions() {
    watchCaptions();
    const now = Date.now();
    const events = [];
    const seen = new Set();
    if (!state.visible) {
      // Nothing new shows on the page now: what it has so far is final.
      for (const [key, block] of blocks) {
        if (typeof key !== 'string') finish(block, events);
      }
    } else if (now < state.absorbUntil) {
      absorbBacklog(now);
    }
    const found = captionsToRead(now);
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
      // A long stretch of one person's talk: its settled start goes out now,
      // and the rest carries on as a new block (see PIECE_MIN_WORDS).
      const piece = settledPiece(block, f.text.slice(block.offset), now);
      if (piece) {
        block.text = f.text.slice(block.offset, block.offset + piece).trim();
        block.lastMs = now;
        finish(block, events);
        block.full = f.text.slice(0, block.offset + piece);
        return;
      }
      const newest = index === found.length - 1;
      if (f.final || now - block.lastMs >= (newest ? NEWEST_IDLE_MS : CAPTION_SETTLE_MS)) {
        finish(block, events);
      } else if (block.text !== block.sentText && now - block.sentAt >= CAPTION_SEND_MS) {
        events.push(captionEvent(block, false));
        block.sentText = block.text;
        block.sentAt = now;
      }
    });
    // Blocks that scrolled away are final as they were last seen. A page block
    // not seen while the page can't be believed may well still be there.
    for (const [el, block] of blocks) {
      const onPage = typeof el !== 'string';
      if (!seen.has(el) && !(onPage && !pageReadable(now))) {
        finish(block, events);
        blocks.delete(el);
      }
    }
    return events;
  }

  // How many characters of a block's text (`rel`, from its offset) have
  // settled: the words it shares with how it read CAPTION_SETTLE_MS ago,
  // less the last PIECE_KEEP_WORDS, cut at a sentence end when one is far
  // enough in. 0 while that is fewer than PIECE_MIN_WORDS words. Hebrew
  // captions come without punctuation, so a word boundary will do.
  function settledPiece(block, rel, now) {
    block.history.push({ at: now, rel });
    while (block.history.length > 1 && now - block.history[1].at >= CAPTION_SETTLE_MS) block.history.shift();
    const before = block.history[0];
    if (now - before.at < CAPTION_SETTLE_MS) return 0;
    let same = 0;
    while (same < before.rel.length && same < rel.length && before.rel[same] === rel[same]) same += 1;
    const words = [...rel.slice(0, same).matchAll(/\S+/g)];
    if (same < rel.length && words.length && words[words.length - 1].index + words[words.length - 1][0].length === same) {
      words.pop();   // a word still being written
    }
    if (words.length < PIECE_MIN_WORDS + PIECE_KEEP_WORDS) return 0;
    const last = words[words.length - 1 - PIECE_KEEP_WORDS];
    let cut = last.index + last[0].length;
    const sentences = [...rel.slice(0, cut).matchAll(/[.?!…](?=\s|$)/g)];
    if (sentences.length) {
      const end = sentences[sentences.length - 1].index + 1;
      if ((rel.slice(0, end).match(/\S+/g) || []).length >= PIECE_MIN_WORDS) cut = end;
    }
    return cut;
  }

  // A block of caption element `f`, starting `offset` characters into its text.
  function newBlock(f, offset, now) {
    const self = f.self === undefined ? isSelfName(f.name) : f.self;
    const block = { id: `${pageId}-${++blockSeq}`, name: f.name, self, offset, full: '',
                    text: '', firstMs: f.firstMs || now, lastMs: now, sentText: '', sentAt: 0, final: false, history: [] };
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

  // `captions_live`: captions are on and can be read right now. Without that,
  // Koli transcribes the audio itself. `channel` is there to show in Koli's log
  // whether Meet's caption channel worked in this call.
  function statusEvent() {
    const now = Date.now();
    const captions = state.captionsOn === true;
    return { type: 'status', code, joined: state.joined, captions, title: state.title,
             captions_live: captions && (channel.live || pageReadable(now)),
             source: channel.live ? 'channel' : 'page', visible: state.visible,
             channel: { labels: [...channel.labels], users: channel.users.size, gave_up: channel.givenUp,
                        self_seen: channel.selfSeen, ...channel.counts },
             self_name: state.selfName, at_ms: now, people: [...state.people.values()] };
  }

  function leave() {
    if (!state.joined) return;
    state.joined = false;
    send([...finishAll(), { type: 'left', code, at_ms: Date.now() }], true);
  }

  // ── The loop ───────────────────────────────────────────────────────────────

  // Coming back from hidden starts the backlog window.
  function noteVisibility() {
    const visible = document.visibilityState === 'visible';
    if (visible && !state.visible) state.absorbUntil = Date.now() + BACKLOG_ABSORB_MS;
    state.visible = visible;
  }

  function tick() {
    noteVisibility();
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
    const summary = JSON.stringify([status.captions, status.captions_live, status.source, status.visible,
                                    status.title, status.people]);
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
