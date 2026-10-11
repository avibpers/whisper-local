// meeting.js
// The meeting panel: who said what as it happens. Shows an offer when a call
// is detected in "ask" mode, the live transcript with speaker colours, the
// paused and excluded states, the resumed/new notices with their undo, and
// "Saved" at the end. The header drags the window; the corner handle resizes
// it; collapse shrinks it to a pill and reports the pill's size.

import { html, render, useEffect, useLayoutEffect, useRef, useState } from '../vendor/preact-htm.module.js';
import { call, on } from './api.js';
import { createStore } from './store.js';
import { Icon } from './ui.js';

const panel = createStore({
  settings: null, meeting: { state: 'idle' }, turns: [], pending: null, menu: false, collapsed: false,
  shownInSharing: false, stuck: true,   // stuck: the list follows new turns
  editing: null, draft: '',             // the speaker name being edited inline
});

function dir(text) { return /[֐-׿؀-ۿ]/.test(text || '') ? 'rtl' : 'ltr'; }
function clock(s) { s = Math.max(0, Math.floor(s)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; }
function loadTurns(id) { call('meeting_get', id).then((d) => panel.set({ turns: d.turns })).catch(() => {}); }

function wire() {
  on('meeting', (m) => {
    const prev = panel.get().meeting;
    const patch = { meeting: m };
    if (m.id !== prev.id) { patch.turns = []; patch.pending = null; }
    panel.set(patch);
    if (m.id && m.id !== prev.id) loadTurns(m.id);
  });
  on('meeting_turn', (p) => {
    const s = panel.get();
    if (p.id !== s.meeting.id) return;
    if (p.reload) { loadTurns(p.id); return; }
    if ('pending' in p) { panel.set({ pending: p.pending }); return; }
    panel.set({ turns: [...s.turns, p.turn], pending: null });
  });
  on('settings', (p) => {
    const s = panel.get().settings;
    if (!s) return;
    const keys = p.path.split('.');
    let node = s;
    keys.slice(0, -1).forEach((k) => { node[k] = node[k] || {}; node = node[k]; });
    node[keys[keys.length - 1]] = p.new;
    panel.set({ settings: { ...s } });
  });
}

// ── Dragging and resizing ─────────────────────────────────────────────────

let seq = 0;
function useDrag(action) {
  const drag = useRef(null);
  const send = (d, done) => call(action, ++seq, d.dx, d.dy, done).catch(() => {});
  const end = () => { const d = drag.current; drag.current = null; if (d && d.moving) { cancelAnimationFrame(d.frame); send(d, true); } };
  return {
    onPointerDown: (e) => {
      if (e.button !== 0 || e.target.closest('button, input, label')) return;
      drag.current = { x: e.screenX, y: e.screenY, dx: 0, dy: 0, moving: false, frame: 0, id: e.pointerId };
    },
    onPointerMove: (e) => {
      const d = drag.current; if (!d) return;
      d.dx = e.screenX - d.x; d.dy = e.screenY - d.y;
      if (!d.moving) { if (Math.hypot(d.dx, d.dy) < 4) return; d.moving = true; e.currentTarget.setPointerCapture(d.id); }
      if (!d.frame) d.frame = requestAnimationFrame(() => { d.frame = 0; if (drag.current === d) send(d, false); });
    },
    onPointerUp: end, onPointerCancel: end,
  };
}

function Grip() {
  const start = useRef(null);
  return html`<svg class="grip" viewBox="0 0 14 14" aria-label="Resize"
    onPointerDown=${(e) => { start.current = { x: e.screenX, y: e.screenY, w: window.innerWidth, h: window.innerHeight }; e.currentTarget.setPointerCapture(e.pointerId); }}
    onPointerMove=${(e) => { const s = start.current; if (!s) return; call('meeting_resize', s.w + (e.screenX - s.x), s.h + (e.screenY - s.y)).catch(() => {}); }}
    onPointerUp=${() => { start.current = null; }}><path d="M13 1L1 13M13 7l-6 6M13 13h0" stroke="currentColor" stroke-width="1.5" fill="none"/></svg>`;
}

// ── Pieces ────────────────────────────────────────────────────────────────

function Timer({ since, paused }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(t); }, []);
  if (!since) return null;
  return html`<span class="sub">${clock((now - new Date(since).getTime()) / 1000)}${paused ? ' · paused' : ''}</span>`;
}

// Inline edit of the name; never a browser prompt.
function Who({ speaker, meeting }) {
  const editing = panel.use((s) => s.editing);
  const draft = panel.use((s) => s.draft);
  if (editing === speaker.id) {
    const commit = () => { call('meeting_rename', speaker.id, draft, meeting.id).catch(() => {}); panel.set({ editing: null }); };
    return html`<input class="who edit" autofocus value=${draft} aria-label="Speaker name"
      onInput=${(e) => panel.set({ draft: e.currentTarget.value })} onBlur=${commit}
      onKeyDown=${(e) => { if (e.key === 'Enter') commit(); if (e.key === 'Escape') panel.set({ editing: null }); }} />`;
  }
  return html`<button type="button" class="who" style=${`color:var(--sp${speaker.colour % 8})`} title="Rename"
    onClick=${() => panel.set({ editing: speaker.id, draft: speaker.name })}>${speaker.name}</button>`;
}

function Turns() {
  const turns = panel.use((s) => s.turns);
  const pending = panel.use((s) => s.pending);
  const meeting = panel.use((s) => s.meeting);
  const stuck = panel.use((s) => s.stuck);
  const box = useRef(null);
  const speakers = Object.fromEntries((meeting.speakers || []).map((sp) => [sp.id, sp]));
  const who = (id) => speakers[id] || { id, name: id === '?' ? 'Speaker ?' : id, colour: 7 };
  useLayoutEffect(() => { if (stuck && box.current) box.current.scrollTop = box.current.scrollHeight; });
  const onScroll = (e) => { const el = e.currentTarget; panel.set({ stuck: el.scrollHeight - el.scrollTop - el.clientHeight < 24 }); };
  const last = turns.slice(-200);
  return html`<div class="turns" ref=${box} onScroll=${onScroll} aria-live="polite">
    ${last.length === 0 && !pending && html`<div class="empty">Listening…</div>`}
    ${last.map((t) => html`<div class="turn"><${Who} speaker=${who(t.speaker)} meeting=${meeting} /><p dir=${dir(t.text)}>${t.text}</p></div>`)}
    ${pending && html`<div class="turn pending"><span class="who" style=${`color:var(--sp${pending.stream === 'mic' ? 0 : 7})`}>${pending.stream === 'mic' ? 'You' : '…'}</span><p>…</p></div>`}
  </div>`;
}

function Menu({ close }) {
  const settings = panel.use((s) => s.settings);
  const shown = panel.use((s) => s.shownInSharing);
  const hide = !!(settings && settings.meeting && settings.meeting.panel.hide_from_sharing);
  return html`<div class="menu" role="menu">
    <label class="mi">Hide from screen sharing<input type="checkbox" checked=${hide} onChange=${(e) => call('settings_set', 'meeting.panel.hide_from_sharing', e.currentTarget.checked)} /></label>
    ${hide && html`<label class="mi">Show it this meeting<input type="checkbox" checked=${shown} onChange=${(e) => { panel.set({ shownInSharing: e.currentTarget.checked }); call('meeting_show_in_sharing', e.currentTarget.checked); }} /></label>`}
    <button type="button" class="mi" onClick=${() => { close(); call('window_open', 'meetings'); }}>Open in Koli</button>
    <button type="button" class="mi" onClick=${() => { close(); call('meeting_new'); }}>New meeting</button>
    <button type="button" class="mi" onClick=${() => { close(); call('meeting_end'); }}>End meeting</button>
  </div>`;
}

function Panel() {
  const m = panel.use((s) => s.meeting);
  const menu = panel.use((s) => s.menu);
  const collapsed = panel.use((s) => s.collapsed);
  const drag = useDrag('meeting_drag');
  const pillRef = useRef(null);
  useLayoutEffect(() => {
    if (!collapsed || !pillRef.current) return;
    const r = pillRef.current.getBoundingClientRect();
    call('meeting_collapse', true, Math.ceil(r.width), Math.ceil(r.height)).catch(() => {});
  }, [collapsed, m.state]);
  const title = m.title || 'Meeting';
  if (m.state === 'offered') {
    const app = m.source && m.source.app ? m.source.app.replace('.exe', '') : 'A call';
    return html`<div class="panel"><div class="head" ...${drag}><span class="dot offered"></span><span class="title">Call detected</span></div>
      <div class="offer"><div>${app} has sound. Transcribe it?</div>
        <div class="row"><button type="button" class="btn" onClick=${() => call('meeting_offer', false)}>Not now</button>
          <button type="button" class="btn pri" onClick=${() => call('meeting_offer', true)}>Start</button></div></div></div>`;
  }
  if (m.state === 'ended') {
    return html`<div class="panel"><div class="head"><span class="dot ended"></span><span class="title">Saved · ${clock(m.duration_s || 0)}</span></div></div>`;
  }
  if (collapsed) {
    return html`<div ref=${pillRef} class="pill" ...${drag} onClick=${() => { panel.set({ collapsed: false }); call('meeting_collapse', false); }} title="Expand">
      <span class=${'dot ' + m.state}></span>${title}<${Timer} since=${m.started_at} paused=${m.state === 'paused'} /></div>`;
  }
  const paused = m.state === 'paused';
  return html`<div class="panel" onKeyDown=${(e) => e.key === 'Escape' && panel.set({ menu: false })}>
    <div class="head" ...${drag}>
      <span class=${'dot ' + m.state}></span><span class="title" title=${title}>${title}</span>
      <${Timer} since=${m.started_at} paused=${paused} />
      ${m.behind_s > 5 && html`<span class="sub" title="Transcription is catching up">· ${Math.round(m.behind_s)} s behind</span>`}
      <button type="button" class="btn icon" aria-label="Menu" onClick=${() => panel.set({ menu: !menu })}><${Icon} name="dots" /></button>
      <button type="button" class="btn icon" aria-label="Collapse" onClick=${() => panel.set({ collapsed: true })}><${Icon} name="down" /></button>
      <button type="button" class="btn icon" aria-label="End meeting" title="End meeting" onClick=${() => call('meeting_end')}><${Icon} name="x" /></button>
    </div>
    ${menu && html`<${Menu} close=${() => panel.set({ menu: false })} />`}
    ${m.notice === 'resumed' && html`<div class="notice"><span class="grow">Resumed · ${title}</span>${m.can_split && html`<button type="button" class="btn" onClick=${() => call('meeting_split')}>Start new instead</button>`}</div>`}
    ${m.notice === 'new' && html`<div class="notice"><span class="grow">New meeting</span>${m.can_continue && html`<button type="button" class="btn" onClick=${() => call('meeting_continue_previous')}>Continue previous instead</button>`}</div>`}
    ${m.excluded && html`<div class="notice"><span class="grow">You're excluded · your microphone isn't in the transcript</span></div>`}
    <${Turns} />
    <div class="foot">
      <button type="button" class=${'btn' + (m.excluded ? ' on' : '')} onClick=${() => call('meeting_exclude', !m.excluded)}><${Icon} name="mic" />${m.excluded ? 'Include me' : 'Exclude me'}</button>
      <button type="button" class="btn" onClick=${() => call(paused ? 'meeting_resume' : 'meeting_pause')}><${Icon} name=${paused ? 'play' : 'pause'} />${paused ? 'Resume' : 'Pause'}</button>
      <span class="grow"></span>
      <button type="button" class="btn icon" aria-label="Open in Koli" title="Open in Koli" onClick=${() => call('window_open', 'meetings')}><${Icon} name="doc" /></button>
    </div>
    <${Grip} />
  </div>`;
}

async function boot() {
  wire();
  render(html`<${Panel} />`, document.getElementById('root'));
  const data = await call('app_bootstrap', 'meeting');
  panel.set({ settings: data.settings, meeting: data.meeting || { state: 'idle' }, turns: data.turns || [],
              collapsed: !!(data.settings.meeting && data.settings.meeting.panel.collapsed) });
}
boot();
