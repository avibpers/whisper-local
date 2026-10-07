// bar.js
// Koli's edge bubble: a slim turquoise tab while idle, and a short status
// while something happens — talking (with your wave and live text), writing,
// done, copy-only, nothing heard, command, rephrase, paused, background task,
// no text field. Click (or right-click) opens quick actions. After every
// change the page reports its size so the window fits it exactly.

import { html, render, useEffect, useLayoutEffect, useRef, useState } from '../vendor/preact-htm.module.js';
import { call, on } from './api.js';
import { createStore, prettyKeys } from './store.js';
import { Icon } from './ui.js';
import { KoliMark } from './brand.js';

const bar = createStore({
  settings: null, state: { state: 'idle', mode: 'dictate', paused: false }, flash: null, streaming: '',
  task: null, levels: new Array(16).fill(0), menu: false, hover: false, profile: null,
});

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
    const [kind, message] = map[p.outcome] || ['ok', 'Done'];
    flash(kind, message, { outcome: p.outcome, text: p.text }, p.outcome === 'pasted' ? 2400 : 4500);
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
      if (t.status === 'failed') flash('fail', `${t.label} failed`, {}, 4500);
      else if (t.status === 'done' && (t.kind === 'engine' || t.kind === 'audio')) flash('ok', `Done · ${t.label.replace(/^Switching to /, '')}`, {}, 2000);
    }
  });
  on('settings', (p) => {
    const s = bar.get().settings;
    if (!s) return;
    const keys = p.path.split('.');
    let node = s;
    keys.slice(0, -1).forEach((k) => { node[k] = node[k] || {}; node = node[k]; });
    node[keys[keys.length - 1]] = p.new;
    bar.set({ settings: { ...s } });
    if (p.path === 'ui.theme') applyTheme(bar.get().settings);
  });
  on('profile', (p) => bar.set({ profile: p.active }));
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

function Menu() {
  const settings = bar.use((s) => s.settings);
  const profile = bar.use((s) => s.profile);
  const paused = bar.use((s) => s.state.paused);
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
  const POS = [['left-edge', 'Left edge'], ['right-edge', 'Right edge'], ['top-left', 'Top left'], ['top-center', 'Top'],
    ['top-right', 'Top right'], ['bottom-left', 'Bottom left'], ['bottom-center', 'Bottom'], ['bottom-right', 'Bottom right']];
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
    <div class="mi" style="cursor:default"><span>Where Koli sits</span>
      <span class="posgrid" role="radiogroup" aria-label="Where Koli sits">
        ${POS.map(([p, label]) => html`<button type="button" role="radio" aria-checked=${pos === p} aria-label=${label} title=${label} class=${pos === p ? 'on' : ''} onClick=${() => set('overlay.position', p)}></button>`)}</span></div>
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
  if (fl) {
    const look = { ok: ['ok', 'check'], fail: ['warn', 'alert'], warn: ['warn', 'alert'], acc: ['acc', 'copy'], info: ['acc', 'check'] }[fl.kind] || ['ok', 'check'];
    return html`<div class="bub">
      <${Badge} tone=${look[0]} icon=${look[1]} /><span class="text">${fl.message}</span>
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
    return html`<div class="bub" style="cursor:pointer" onClick=${openMenu} title="Quick actions">
      <${KoliMark} size=${40} />${settings.hotkey.recording_mode === 'toggle' ? 'Press' : 'Hold'} <span class="keys">${keys}</span>
      <button type="button" class="btn round" aria-label="Quick actions" style="width:34px;height:34px"><${Icon} name="dots" /></button></div>`;
  }
  return html`<div class="bub rest" role="button" tabindex="0" aria-label="Koli is ready. Open quick actions" onClick=${openMenu}></div>`;
}

// True while the bubble is the slim resting tab (painted turquoise).
function isResting(s) {
  return !!s.settings && !s.menu && !s.hover && !s.flash && !s.task && s.state.state !== 'recording'
    && s.state.state !== 'processing' && !s.state.paused;
}

function Bar() {
  const menu = bar.use((s) => s.menu);
  const resting = bar.use(isResting);
  const ref = useRef(null);
  const last = useRef('');
  useEffect(() => { document.documentElement.classList.toggle('rest', resting); }, [resting]);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const report = () => {
      const r = el.getBoundingClientRect();
      const key = `${Math.ceil(r.width)}x${Math.ceil(r.height)}`;
      if (key !== last.current) {
        last.current = key;
        call('bar_resize', Math.ceil(r.width), Math.ceil(r.height)).then((res) => {
          if (res && res.max_height) document.documentElement.style.setProperty('--menu-max', `${res.max_height}px`);
        }).catch(() => {});
      }
    };
    report();
    const ro = new ResizeObserver(report);
    ro.observe(el);
    return () => ro.disconnect();
  });
  return html`<div ref=${ref} style="display:inline-flex"
      onMouseEnter=${() => bar.set({ hover: true })} onMouseLeave=${() => bar.set({ hover: false })}
      onContextMenu=${(e) => { e.preventDefault(); bar.set({ menu: true }); }}
      onKeyDown=${(e) => e.key === 'Escape' && bar.set({ menu: false })}>
    ${menu ? html`<${Menu} />` : html`<${Status} />`}
  </div>`;
}

async function boot() {
  wire();
  render(html`<${Bar} />`, document.getElementById('root'));
  const data = await call('app_bootstrap', 'bar');
  bar.set({ settings: data.settings, state: data.state, profile: data.profile });
  applyTheme(data.settings);
  const running = (data.tasks || []).filter((t) => t.status === 'running');
  if (running.length) bar.set({ task: running[running.length - 1] });
}
boot();
