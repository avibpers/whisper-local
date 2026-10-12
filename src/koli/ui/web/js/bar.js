// bar.js
// Koli's edge bubble: a slim turquoise tab while idle, and a short status
// while something happens — talking (with your wave and live text), writing,
// done, copy-only, nothing heard, command, rephrase, paused, background task,
// no text field, the countdown before an auto-send presses Enter, and a
// detected meeting counting down (Stop, Start now). Hovering during a
// meeting offers Stop. Click (or right-click) opens quick actions. After
// every change the page reports its size so the window fits it exactly.

import { html, render, useEffect, useLayoutEffect, useRef, useState } from '../vendor/preact-htm.module.js';
import { call, on } from './api.js';
import { createStore, prettyKeys } from './store.js';
import { Icon } from './ui.js';
import { KoliMark } from './brand.js';

const bar = createStore({
  settings: null, state: { state: 'idle', mode: 'dictate', paused: false }, flash: null, streaming: '',
  task: null, levels: new Array(16).fill(0), menu: false, hover: false, profile: null,
  sending: null,   // { remaining, total } while an auto-send waits before Enter
  upright: true,   // the resting tab stands on a side or beside a field; flat at the top or bottom
  dragging: false,
  meeting: { state: 'idle' },   // the meeting's state, as the meeting panel gets it
});

const MEETING_ON = new Set(['live', 'paused']);

let flashTimer = null;
function flash(kind, message, extra = {}, ms = 2600) {
  clearTimeout(flashTimer);
  bar.set({ flash: { kind, message, ...extra } });
  flashTimer = setTimeout(() => bar.set({ flash: null }), ms);
}

function wire() {
  on('state', (s) => {
    const prev = bar.get().state;
    bar.set({ state: { ...prev, ...s } });
    if (s.state === 'recording') bar.set({ flash: null, streaming: '' });
  });
  on('level', (p) => {
    const levels = bar.get().levels.slice(1);
    levels.push(p.level);
    bar.set({ levels });
  });
  on('streaming_text', (p) => bar.set({ streaming: p.text }));
  on('bar', (b) => {
    if (b.cmd === 'failure') flash('fail', b.message || "Didn't work");
    if (b.cmd === 'shape') bar.set({ upright: b.upright });
  });
  on('pipeline', (p) => {
    if (p.stage !== 'delivered') return;
    const app = p.app ? p.app.replace('.exe', '') : 'the app';
    const map = {
      pasted: ['ok', `Pasted in ${app}${p.auto_enter ? ' and sent' : ''}`],
      streamed: ['ok', `Typed in ${app}`],
      copied: ['acc', `Copied · ${app} is copy-only`],
      suppressed: ['warn', `Not delivered in ${app}`],
      fallback: ['acc', 'No text field · saved'],
      failed: ['fail', "Couldn't paste"],
      empty: ['warn', 'Nothing to type'],
    };
    if (p.send_pending) return;   // the countdown takes over; its end says what happened
    const [kind, message] = map[p.outcome] || ['ok', 'Done'];
    flash(kind, message, { outcome: p.outcome, text: p.text }, p.outcome === 'pasted' ? 2400 : 4500);
  });
  on('auto_send', (a) => {
    if (a.state === 'counting') {
      bar.set({ sending: { remaining: a.remaining, total: a.total }, flash: null });
      return;
    }
    bar.set({ sending: null });
    if (a.state === 'sent') flash('ok', 'Sent', {}, 1800);
    else if (a.state === 'not_sent') flash('warn', `Not sent · ${a.reason}`, {}, 4000);
    else if (a.state === 'failed') flash('fail', "Couldn't press Enter", {}, 4000);
    else flash('info', `Not sent · ${a.reason}`, {}, 3000);
  });
  on('notify', (n) => {
    if (!n || n.kind === 'fallback') return;
    if (/^(Profile|Language|Hotkeys)/.test(n.message || '')) flash('info', n.message, {}, 2000);
  });
  on('task', (t) => {
    const cur = bar.get().task;
    if (t.status === 'running') bar.set({ task: t });
    else if (cur && cur.id === t.id) {
      bar.set({ task: null });
      // A failure arrives as a 'problem' as well, flashed below.
      if (t.status === 'done' && (t.kind === 'engine' || t.kind === 'audio')) flash('ok', `Done · ${t.label.replace(/^Switching to /, '')}`, {}, 2000);
    }
  });
  // Something that went wrong: shown long enough to read; the app keeps it.
  on('problem', (p) => flash('fail', p.message, { problem: true }, 8000));
  on('settings', (p) => {
    const s = bar.get().settings;
    if (!s) return;
    const keys = p.path.split('.');
    let node = s;
    keys.slice(0, -1).forEach((k) => { node[k] = node[k] || {}; node = node[k]; });
    node[keys[keys.length - 1]] = p.new;
    bar.set({ settings: { ...s } });
    if (p.path === 'ui.theme') applyTheme(bar.get().settings);
    if (p.path === 'overlay.rest_color') applyRestColor(bar.get().settings);
  });
  on('profile', (p) => bar.set({ profile: p.active }));
  on('meeting', (m) => {
    const prev = bar.get().meeting;
    bar.set({ meeting: m });
    if (m.state === 'ended') flash('ok', 'Meeting saved', {}, 2600);
    else if (m.state === 'idle' && m.auto_paused && !prev.auto_paused && prev.state === 'offered') {
      flash('info', "Not starting · it waits until the call is quiet", {}, 3500);
    }
  });
}

// ── Theme ─────────────────────────────────────────────────────────────────

// The bubble follows the app window's light or dark mode.
const media = window.matchMedia('(prefers-color-scheme: dark)');
function applyTheme(settings) {
  const choice = settings && settings.ui && settings.ui.theme;
  const dark = choice === 'dark' || (choice === 'system' && media.matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
}
media.addEventListener('change', () => applyTheme(bar.get().settings));

// The resting tab's own colour; anything that isn't a colour keeps turquoise.
function applyRestColor(settings) {
  const color = ((settings && settings.overlay && settings.overlay.rest_color) || '').trim();
  const root = document.documentElement.style;
  if (color && CSS.supports('color', color)) root.setProperty('--rest', color);
  else root.removeProperty('--rest');
}

// ── Pieces ────────────────────────────────────────────────────────────────

function Wave() {
  const levels = bar.use((s) => s.levels);
  return html`<span class="wv" aria-hidden="true">${levels.map((l) => html`<span style=${`transform:scaleY(${(Math.max(3, Math.min(26, 3 + l * 140)) / 26).toFixed(3)})`}></span>`)}</span>`;
}

function Timer() {
  const [start] = useState(Date.now());
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 250); return () => clearInterval(t); }, []);
  const s = Math.floor((now - start) / 1000);
  return html`<span class="time">${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}</span>`;
}

const Badge = ({ tone, icon }) => html`<span class=${`badge ${tone}`}><${Icon} name=${icon} /></span>`;

// A call was detected: which one, and in auto mode a countdown with Stop and
// Start now (like the auto-send countdown); in ask mode Not now and Start.
function MeetingOffer({ meeting }) {
  const offer = meeting.offer || {};
  const auto = !!offer.starts_at;
  const [now, setNow] = useState(Date.now());
  const [total] = useState(() => (auto ? Math.max(1, offer.starts_at - Date.now() / 1000) : 1));
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 200); return () => clearInterval(t); }, []);
  const what = offer.label && offer.label !== offer.app_name ? `${offer.label} · ${offer.app_name}` : (offer.app_name || 'A call');
  const left = auto ? Math.max(0, offer.starts_at - now / 1000) : 0;
  return html`<div class="bub">
    <span class="badge acc" aria-hidden="true">${auto ? html`<b>${Math.max(1, Math.ceil(left))}</b>` : html`<${Icon} name="mic" />`}</span>
    <span class="text" role="status" title=${`Meeting detected · ${what}`}>${what}</span>
    ${auto && html`<span class="prog"><span style=${`width:${Math.round((left / total) * 100)}%`}></span></span>`}
    <button type="button" class="btn" title=${auto ? "Don't transcribe; it waits until the call is quiet before trying again" : ''}
      onClick=${() => call('meeting_offer', false)}>${auto ? 'Stop' : 'Not now'}</button>
    <button type="button" class="btn pri" onClick=${() => call('meeting_offer', true)}>${auto ? 'Start now' : 'Start'}</button>
  </div>`;
}

// Where the bubble can sit, laid out as on the screen: [setting, name, row, column].
const POSITIONS = [
  ['top-left', 'Top left', 'top', 'left'], ['top-center', 'Top', 'top', 'center'], ['top-right', 'Top right', 'top', 'right'],
  ['left-edge', 'Left edge', 'mid', 'left'], ['right-edge', 'Right edge', 'mid', 'right'],
  ['bottom-left', 'Bottom left', 'bottom', 'left'], ['bottom-center', 'Bottom', 'bottom', 'center'], ['bottom-right', 'Bottom right', 'bottom', 'right'],
];

// A small screen with each spot where the bubble really goes: slim tabs flush
// with the sides, pills in the top and bottom corners and middles. Hovering a
// spot names it.
function PositionPicker({ value, onPick }) {
  const [hint, setHint] = useState(null);
  const current = POSITIONS.find(([p]) => p === value);
  return html`<div class="mi pos-row" style="cursor:default">
    <span class="pos-label">Where Koli sits<span class="muted">${hint || (current ? current[1] : value)}</span></span>
    <span class="posgrid" role="radiogroup" aria-label="Where Koli sits" onMouseLeave=${() => setHint(null)}>
      ${POSITIONS.map(([p, label, row, col]) => html`<button type="button" role="radio" aria-checked=${value === p} aria-label=${label} title=${label}
        class=${`${row} ${col}${value === p ? ' on' : ''}`} onMouseEnter=${() => setHint(label)} onClick=${() => onPick(p)}></button>`)}</span></div>`;
}

// Colours for the resting tab: [setting, name, what the swatch shows]. Empty
// is Koli turquoise, which follows light and dark mode.
const REST_COLORS = [['', 'Koli turquoise', 'var(--brand)'], ['#7C5CFF', 'Violet'], ['#22C55E', 'Green'],
  ['#F97316', 'Orange'], ['#EC4899', 'Pink'], ['#1E252B', 'Night'], ['#FFFFFF', 'White']];

// How the tab looks while Koli waits: colour, how solid it is, and whether it
// follows the text field.
function RestLook({ overlay, set }) {
  const color = (overlay.rest_color || '').toLowerCase();
  const own = color && !REST_COLORS.some(([c]) => c.toLowerCase() === color);
  const [opacity, setOpacity] = useState(overlay.rest_opacity ?? 100);
  useEffect(() => setOpacity(overlay.rest_opacity ?? 100), [overlay.rest_opacity]);
  return html`<div class="mi" style="cursor:default"><span>Colour</span>
      <span class="swatches" role="radiogroup" aria-label="Resting colour">
        ${REST_COLORS.map(([c, label, paint]) => html`<button type="button" role="radio" aria-checked=${color === c.toLowerCase()}
          aria-label=${label} title=${label} class=${'swatch' + (color === c.toLowerCase() ? ' on' : '')}
          style=${`background:${paint || c}`} onClick=${() => set('overlay.rest_color', c)}></button>`)}
        <label class=${'swatch any' + (own ? ' on' : '')} title="Any colour" style=${own ? `background:${color}` : ''}>
          <input type="color" aria-label="Any colour" value=${own ? color : '#15b4d4'}
            onChange=${(e) => set('overlay.rest_color', e.currentTarget.value)} /></label></span></div>
    <div class="mi" style="cursor:default"><span>When asleep</span>
      <input type="range" class="range" min="20" max="100" step="5" value=${opacity} aria-label="Opacity when asleep"
        onInput=${(e) => setOpacity(Number(e.currentTarget.value))}
        onChange=${(e) => set('overlay.rest_opacity', Number(e.currentTarget.value))} />
      <span class="r">${opacity}%</span></div>
    <label class="mi"><${Badge} tone="acc" icon="pen" />Follow the text field<input type="checkbox" class="sw"
      checked=${!!overlay.follow_focus} onChange=${(e) => set('overlay.follow_focus', e.currentTarget.checked)} /></label>`;
}

function Menu() {
  const settings = bar.use((s) => s.settings);
  const profile = bar.use((s) => s.profile);
  const paused = bar.use((s) => s.state.paused);
  const meeting = bar.use((s) => s.meeting);
  const [profiles, setProfiles] = useState([]);
  const [transforms, setTransforms] = useState([]);
  const [recent, setRecent] = useState([]);
  const [showTransforms, setShowTransforms] = useState(false);
  useEffect(() => {
    call('profiles_list').then((d) => { setProfiles(d.profiles.filter((p) => !p.disabled)); bar.set({ profile: d.active }); }).catch(() => {});
    call('transforms_list').then((t) => setTransforms(t.filter((x) => !x.disabled))).catch(() => {});
    call('recent').then(setRecent).catch(() => {});
  }, []);
  if (!settings) return null;
  const lang = settings.whisper.language;
  const set = (path, value) => call('settings_set', path, value).catch(() => {});
  const close = () => bar.set({ menu: false });
  const pos = settings.overlay.position;
  const lower = settings.audio.lower_volume || {};
  return html`<div class="menu" role="menu" aria-label="Quick actions">
    <div class="mhead"><span class="name"><${KoliMark} size=${30} wave=${false} />koli</span>
      <span style="display:flex;gap:6px"><button type="button" class="btn" onClick=${() => { close(); call('window_open', null); }}>Open Koli</button>
        <button type="button" class="btn round" style="width:34px;height:34px" aria-label="Close menu" onClick=${close}><${Icon} name="x" /></button></span></div>
    ${profiles.length > 1 && html`<div class="eyebrow">Profile</div>
      <div class="seg">${profiles.map((p) => html`<button type="button" class=${p.id === profile ? 'on' : ''}
        onClick=${() => call('profile_activate', p.id).then(() => bar.set({ profile: p.id }))}>${p.id}</button>`)}</div>`}
    <div class="eyebrow">Language</div>
    <div class="seg" role="group" aria-label="Language">${[['auto', 'Auto'], ['en', 'English'], ['he', 'עברית']].map(([v, t]) => html`<button type="button"
      class=${lang === v ? 'on' : ''} style="text-transform:none" onClick=${() => set('whisper.language', v)}>${t}</button>`)}
      ${!['auto', 'en', 'he'].includes(lang) && html`<button type="button" class="on" style="text-transform:none">${lang}</button>`}</div>
    <div class="sep"></div>
    ${MEETING_ON.has(meeting.state)
      ? html`<button type="button" class="mi" onClick=${() => { close(); call('meeting_end'); }}><${Badge} tone="warn" icon="x" />Stop meeting</button>`
      : html`<button type="button" class="mi" onClick=${() => { close(); call('meeting_start'); }}><${Badge} tone="acc" icon="mic" />Start a meeting</button>`}
    <button type="button" class="mi" onClick=${() => { close(); call('paste_last'); }}><${Badge} tone="acc" icon="copy" />Paste last dictation<span class="r">${prettyKeys(settings.hotkey.paste_last_hotkey).join(' ')}</span></button>
    ${transforms.length > 0 && html`<button type="button" class="mi" onClick=${() => setShowTransforms(!showTransforms)}><${Badge} tone="purple" icon="pen" />Transform selection<span class="r">${showTransforms ? '▾' : '›'}</span></button>
      ${showTransforms && transforms.map((t) => html`<button type="button" class="mi" style="padding-inline-start:52px"
        onClick=${() => { close(); call('transform_apply', t.id).then((ok) => flash(ok ? 'ok' : 'warn', ok ? `Applied ${t.entry.name}` : 'Select text first')); }}>${t.entry.name}<span class="r">${prettyKeys(t.entry.hotkey).join(' ')}</span></button>`)}`}
    <button type="button" class="mi" onClick=${() => { close(); call('window_open', 'vocabulary'); }}><${Badge} tone="purple" icon="plus" />Teach Koli a word…</button>
    <div class="sep"></div>
    <label class="mi"><${Badge} tone="warn" icon="pause" />Pause my music<input type="checkbox" class="sw" checked=${settings.audio.pause_media_on_record} onChange=${(e) => set('audio.pause_media_on_record', e.currentTarget.checked)} /></label>
    <label class="mi"><${Badge} tone="quiet" icon="down" />Turn the computer down<input type="checkbox" class="sw" checked=${!!lower.enabled} onChange=${(e) => set('audio.lower_volume.enabled', e.currentTarget.checked)} /></label>
    <label class="mi"><${Badge} tone="quiet" icon="refresh" />Continuous dictation<input type="checkbox" class="sw" checked=${settings.audio.continuous_mode} onChange=${(e) => set('audio.continuous_mode', e.currentTarget.checked)} /></label>
    <label class="mi"><${Badge} tone="ok" icon="check" />Hotkeys on<input type="checkbox" class="sw" checked=${!paused} onChange=${(e) => call('dictation_pause', !e.currentTarget.checked)} /></label>
    ${recent.length > 0 && html`<div class="sep"></div><div class="eyebrow">Recent · click to copy</div>
      ${recent.slice(0, 3).map((r) => html`<button type="button" class="mi" onClick=${() => { call('copy_text', r); flash('ok', 'Copied', {}, 1500); close(); }}>
        <span class="recent" dir="auto">${r}</span></button>`)}`}
    <div class="sep"></div>
    <${PositionPicker} value=${pos} onPick=${(p) => set('overlay.position', p)} />
    <${RestLook} overlay=${settings.overlay} set=${set} />
    <div class="sep"></div>
    <button type="button" class="mi" onClick=${() => call('app_quit')}><${Badge} tone="quiet" icon="x" />Quit Koli</button>
  </div>`;
}

function Status() {
  const st = bar.use((s) => s.state);
  const fl = bar.use((s) => s.flash);
  const task = bar.use((s) => s.task);
  const streaming = bar.use((s) => s.streaming);
  const hover = bar.use((s) => s.hover);
  const settings = bar.use((s) => s.settings);
  const sending = bar.use((s) => s.sending);
  const upright = bar.use((s) => s.upright);
  const meeting = bar.use((s) => s.meeting);
  const openMenu = () => bar.set({ menu: true });
  if (!settings) return html`<div class="bub rest" aria-label="Koli is starting"></div>`;
  const keys = prettyKeys(settings.hotkey.recording_hotkey).join(' ');

  if (st.state === 'recording') {
    const command = st.mode === 'command';
    const rephrase = st.mode === 'rephrase';
    const auto = st.mode === 'auto';  // started by a focused text field, not a hotkey
    const handsFree = st.hands_free || (settings.audio && settings.audio.continuous_mode);
    return html`<div class="bub">
      ${command ? html`<${Badge} tone="purple" icon="command" /><span>Command</span>`
        : rephrase ? html`<${Badge} tone="purple" icon="pen" /><span>Rephrase</span>`
        : auto ? html`<${Badge} tone="acc" icon="mic" /><span title="Started because a text field got focus. Typing cancels it.">Listening</span>`
        : handsFree ? html`<span class="badge purple" title="Recording without holding the keys. Tap them again to stop."><${Icon} name="lock" /></span>` : ''}
      <${Wave} />
      ${streaming && !command ? html`<span class="text" dir="auto">…${streaming.slice(-48)}</span>` : html`<${Timer} />`}
      <button type="button" class="btn" onClick=${() => call('dictation_cancel')}>Cancel</button>
      <button type="button" class="btn round pri" aria-label=${command ? 'Run the command' : 'Stop and write'} onClick=${() => call('dictation_stop', false)}>
        <svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="2.5"></rect></svg></button>
    </div>`;
  }
  if (st.state === 'processing') {
    return html`<div class="bub" style="padding-inline-end:16px"><${Badge} tone="acc" icon="pen" />Writing it down · <${Timer} /></div>`;
  }
  // The text is pasted; Enter waits so it can be read. The bubble never takes
  // focus, so these buttons leave the keyboard in the chat.
  if (sending) {
    const pct = Math.round((sending.remaining / sending.total) * 100);
    return html`<div class="bub">
      <span class="badge acc" aria-hidden="true"><b>${sending.remaining}</b></span>
      <span class="text" role="status">Sending in ${sending.remaining}s</span>
      <span class="prog"><span style=${`width:${pct}%`}></span></span>
      <button type="button" class="btn" title="Esc or typing also stops it" onClick=${() => call('auto_send_cancel')}>Cancel</button>
      <button type="button" class="btn pri" onClick=${() => call('auto_send_now')}>Send now</button>
    </div>`;
  }
  if (meeting.state === 'offered') return html`<${MeetingOffer} meeting=${meeting} />`;
  if (fl) {
    const look = { ok: ['ok', 'check'], fail: ['warn', 'alert'], warn: ['warn', 'alert'], acc: ['acc', 'copy'], info: ['acc', 'check'] }[fl.kind] || ['ok', 'check'];
    return html`<div class="bub">
      <${Badge} tone=${look[0]} icon=${look[1]} /><span class="text" title=${fl.message}>${fl.message}</span>
      ${fl.problem && html`<button type="button" class="btn" onClick=${() => call('window_open', null)}>Details</button>`}
      ${fl.outcome === 'pasted' && html`<button type="button" class="btn" onClick=${() => { call('copy_text', fl.text); flash('ok', 'Copied', {}, 1200); }}>Copy</button>
        <button type="button" class="btn" onClick=${() => call('paste_last')}>Again</button>`}
      ${fl.outcome === 'copied' && html`<button type="button" class="btn" onClick=${() => call('window_open', 'apps')}>Why?</button>`}
      ${fl.outcome === 'fallback' && html`<button type="button" class="btn" onClick=${() => call('window_open', 'live')}>Open</button>`}
      ${!fl.outcome && html`<span style="width:4px"></span>`}
    </div>`;
  }
  if (st.paused) {
    return html`<div class="bub"><${Badge} tone="quiet" icon="pause" /><span>Koli is paused</span>
      <button type="button" class="btn" onClick=${() => call('dictation_pause', false)}>Resume</button></div>`;
  }
  if (task) {
    const pct = task.progress != null ? task.progress * 100 : null;
    return html`<div class="bub" style="padding-inline-end:16px"><${Badge} tone="acc" icon="refresh" />
      <span class="text">${task.label}</span>${pct != null && html`<span class="prog"><span style=${`width:${pct}%`}></span></span>`}</div>`;
  }
  if (hover) {
    const inMeeting = MEETING_ON.has(meeting.state);
    return html`<div class="bub" style="cursor:pointer" onClick=${openMenu} title="Quick actions">
      <${KoliMark} size=${40} />${settings.hotkey.recording_mode === 'toggle' ? 'Press' : 'Hold'} <span class="keys">${keys}</span>
      ${inMeeting && html`<button type="button" class="btn" title="Stop the meeting and save it"
        onClick=${(e) => { e.stopPropagation(); call('meeting_end'); }}><${Icon} name="x" />Stop meeting</button>`}
      <button type="button" class="btn round" aria-label="Quick actions" style="width:34px;height:34px"><${Icon} name="dots" /></button></div>`;
  }
  // Upright on a side of the screen or beside a field, flat at the top or bottom.
  return html`<div class=${upright ? 'bub rest' : 'bub rest flat'} role="button" tabindex="0" aria-label="Koli is ready. Open quick actions" onClick=${openMenu}></div>`;
}

// True while the bubble is the slim resting tab (painted in its resting colour).
function isResting(s) {
  return !!s.settings && !s.menu && !s.hover && !s.flash && !s.task && !s.sending && s.state.state !== 'recording'
    && s.state.state !== 'processing' && !s.state.paused && s.meeting.state !== 'offered';
}

// ── Dragging ──────────────────────────────────────────────────────────────

let dragSeq = 0;   // numbers every drag message, so the host can drop late ones

// Press on the bubble and drag to move it; a press that doesn't move stays a
// click. Moves go out once per frame, measured from where the press began.
function useDrag(enabled) {
  const drag = useRef(null);
  const swallowClick = useRef(false);
  const send = (d, done) => call('bar_drag', ++dragSeq, d.dx, d.dy, done).catch(() => {});
  const end = () => {
    const d = drag.current;
    drag.current = null;
    if (!d || !d.moving) return;
    cancelAnimationFrame(d.frame);
    send(d, true);
    // A click comes right after the release, if at all (not when the pointer
    // ended up outside); either way the next tick is a fresh start.
    swallowClick.current = true;
    setTimeout(() => { swallowClick.current = false; }, 0);
    bar.set({ dragging: false });
  };
  return {
    onPointerDown: (e) => {
      if (!enabled || e.button !== 0 || e.target.closest('button, input, label, select')) return;
      drag.current = { x: e.screenX, y: e.screenY, dx: 0, dy: 0, moving: false, frame: 0, id: e.pointerId };
    },
    onPointerMove: (e) => {
      const d = drag.current;
      if (!d) return;
      d.dx = e.screenX - d.x;
      d.dy = e.screenY - d.y;
      if (!d.moving) {
        if (Math.hypot(d.dx, d.dy) < 4) return;
        // Captured only now: a plain click keeps its own target.
        d.moving = true;
        e.currentTarget.setPointerCapture(d.id);
        bar.set({ dragging: true });
      }
      if (!d.frame) d.frame = requestAnimationFrame(() => { d.frame = 0; if (drag.current === d) send(d, false); });
    },
    onPointerUp: end,
    onPointerCancel: end,
    // The click that ends a drag must not open the menu.
    onClickCapture: (e) => {
      if (!swallowClick.current) return;
      swallowClick.current = false;
      e.stopPropagation();
      e.preventDefault();
    },
  };
}

function Bar() {
  const menu = bar.use((s) => s.menu);
  const resting = bar.use(isResting);
  const ref = useRef(null);
  const last = useRef('');
  const drag = useDrag(!menu);
  useEffect(() => { document.documentElement.classList.toggle('rest', resting); }, [resting]);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const report = () => {
      const r = el.getBoundingClientRect();
      const key = `${Math.ceil(r.width)}x${Math.ceil(r.height)}:${resting}`;
      if (key !== last.current) {
        last.current = key;
        call('bar_resize', Math.ceil(r.width), Math.ceil(r.height), resting).then((res) => {
          if (res && res.max_height) document.documentElement.style.setProperty('--menu-max', `${res.max_height}px`);
        }).catch(() => {});
      }
    };
    report();
    const ro = new ResizeObserver(report);
    ro.observe(el);
    return () => ro.disconnect();
  });
  return html`<div ref=${ref} style="display:inline-flex" ...${drag}
      onMouseEnter=${() => bar.set({ hover: true })} onMouseLeave=${() => { if (!bar.get().dragging) bar.set({ hover: false }); }}
      onContextMenu=${(e) => { e.preventDefault(); bar.set({ menu: true }); }}
      onKeyDown=${(e) => e.key === 'Escape' && bar.set({ menu: false })}>
    ${menu ? html`<${Menu} />` : html`<${Status} />`}
  </div>`;
}

async function boot() {
  wire();
  render(html`<${Bar} />`, document.getElementById('root'));
  const data = await call('app_bootstrap', 'bar');
  bar.set({ settings: data.settings, state: data.state, profile: data.profile, upright: data.upright !== false,
            meeting: data.meeting || { state: 'idle' } });
  applyTheme(data.settings);
  applyRestColor(data.settings);
  const running = (data.tasks || []).filter((t) => t.status === 'running');
  if (running.length) bar.set({ task: running[running.length - 1] });
}
boot();
