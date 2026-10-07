// bar.js
// The floating bar: a thin handle while idle, and a short status while
// something happens — recording (with level and live text), transcribing,
// delivered, copy-only, nothing heard, command, rephrase, paused, background
// task, no text field. ⋯ (or right-click) opens quick actions. After every
// change the page reports its size so the window fits it exactly.

import { html, render, useEffect, useLayoutEffect, useRef, useState } from '../vendor/preact-htm.module.js';
import { call, on } from './api.js';
import { createStore, prettyKeys } from './store.js';
import { Icon } from './ui.js';

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
  });
  on('profile', (p) => bar.set({ profile: p.active }));
}

// ── Pieces ────────────────────────────────────────────────────────────────

function Wave() {
  const levels = bar.use((s) => s.levels);
  return html`<span class="wv" aria-hidden="true">${levels.map((l) => html`<span style=${`height:${Math.max(3, Math.min(22, 3 + l * 120))}px`}></span>`)}</span>`;
}

function Timer() {
  const [start] = useState(Date.now());
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 250); return () => clearInterval(t); }, []);
  const s = Math.floor((now - start) / 1000);
  return html`<span style="font-family:var(--mono);font-size:12px">${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}</span>`;
}

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
  const POS = ['top-left', 'top-center', 'top-right', 'bottom-left', 'bottom-center', 'bottom-right'];
  return html`<div class="menu" role="menu" aria-label="Quick actions">
    <div class="mhead"><span>Whisper Local</span>
      <span style="display:flex;gap:6px"><button type="button" class="pbtn" onClick=${() => { close(); call('window_open', null); }}>Open app</button>
        <button type="button" class="pbtn" aria-label="Close menu" onClick=${close}><${Icon} name="x" /></button></span></div>
    ${profiles.length > 0 && html`<div class="eyebrow">Profile</div>
      <div class="chips">${profiles.map((p) => html`<button type="button" class=${'cb' + (p.id === profile ? ' on' : '')}
        onClick=${() => call('profile_activate', p.id).then(() => bar.set({ profile: p.id }))}>${p.id}</button>`)}</div>`}
    <div class="eyebrow">Language</div>
    <div class="chips">${[['auto', 'Auto'], ['en', 'English'], ['he', 'עברית']].map(([v, t]) => html`<button type="button"
      class=${'cb' + (lang === v ? ' on' : '')} style="text-transform:none" onClick=${() => set('whisper.language', v)}>${t}</button>`)}
      ${!['auto', 'en', 'he'].includes(lang) && html`<button type="button" class="cb on" style="text-transform:none">${lang}</button>`}
      <button type="button" class="cb" style="text-transform:none" onClick=${() => { close(); call('window_open', 'settings/general/whisper.language'); }}>More…</button></div>
    <div class="sep"></div>
    <button type="button" class="mi" onClick=${() => { close(); call('paste_last'); }}>Paste last dictation<span class="r">${prettyKeys(settings.hotkey.paste_last_hotkey).join(' ')}</span></button>
    <button type="button" class="mi" disabled=${!recent[0]} onClick=${() => { call('copy_text', recent[0]); flash('ok', 'Copied', {}, 1500); close(); }}>Copy last dictation</button>
    ${transforms.length > 0 && html`<button type="button" class="mi" onClick=${() => setShowTransforms(!showTransforms)}>Transform selection<span class="r">${showTransforms ? '▾' : '›'}</span></button>
      ${showTransforms && transforms.map((t) => html`<button type="button" class="mi" style="padding-inline-start:24px"
        onClick=${() => { close(); call('transform_apply', t.id).then((ok) => flash(ok ? 'ok' : 'warn', ok ? `Applied ${t.entry.name}` : 'Select text first')); }}>${t.entry.name}<span class="r">${prettyKeys(t.entry.hotkey).join(' ')}</span></button>`)}`}
    <button type="button" class="mi" onClick=${() => { close(); call('window_open', 'vocabulary'); }}>Add a word…</button>
    <div class="sep"></div>
    <label class="mi">Auto-paste at cursor<input type="checkbox" class="sw" checked=${settings.clipboard.auto_paste} onChange=${(e) => set('clipboard.auto_paste', e.currentTarget.checked)} /></label>
    <label class="mi">Continuous dictation<input type="checkbox" class="sw" checked=${settings.audio.continuous_mode} onChange=${(e) => set('audio.continuous_mode', e.currentTarget.checked)} /></label>
    <label class="mi">Hotkeys active<input type="checkbox" class="sw" checked=${!paused} onChange=${(e) => call('dictation_pause', !e.currentTarget.checked)} /></label>
    ${recent.length > 0 && html`<div class="sep"></div><div class="eyebrow">Recent · click to copy</div>
      ${recent.slice(0, 3).map((r) => html`<button type="button" class="mi" onClick=${() => { call('copy_text', r); flash('ok', 'Copied', {}, 1500); close(); }}>
        <span class="recent" dir="auto">${r}</span></button>`)}`}
    <div class="sep"></div>
    <div class="mi" style="cursor:default"><span>Bar position</span>
      <span class="posgrid" style="margin-inline-start:auto" role="radiogroup" aria-label="Bar position">
        ${POS.map((p) => html`<button type="button" role="radio" aria-checked=${pos === p} aria-label=${p.replace('-', ' ')} class=${pos === p ? 'on' : ''} onClick=${() => set('overlay.position', p)}></button>`)}</span></div>
    <button type="button" class="mi" onClick=${() => call('app_quit')}>Quit Whisper Local</button>
  </div>`;
}

function Status() {
  const st = bar.use((s) => s.state);
  const fl = bar.use((s) => s.flash);
  const task = bar.use((s) => s.task);
  const streaming = bar.use((s) => s.streaming);
  const hover = bar.use((s) => s.hover);
  const settings = bar.use((s) => s.settings);
  const menuBtn = html`<button type="button" class="pbtn" aria-label="Quick actions" onClick=${() => bar.set({ menu: true })}><${Icon} name="dots" /></button>`;
  if (!settings) return html`<div class="pill rest" aria-label="Starting"><i></i></div>`;
  const keys = prettyKeys(settings.hotkey.recording_hotkey).join(' ');

  if (st.state === 'recording') {
    const command = st.mode === 'command';
    const rephrase = st.mode === 'rephrase';
    return html`<div class="pill" style=${command || rephrase ? 'box-shadow:inset 0 0 0 2px var(--bar-acc)' : ''}>
      ${command ? html`<${Icon} name="command" /><span class="acc">Command</span>` : rephrase ? html`<${Icon} name="pen" /><span class="acc">Rephrase</span>` : html`<span class="recdot"></span>`}
      <${Wave} />
      ${streaming && !command ? html`<span class="text" dir="auto">…${streaming.slice(-48)}</span>` : html`<${Timer} />`}
      <button type="button" class="pbtn" onClick=${() => call('dictation_cancel')}>Cancel</button>
      <button type="button" class="pbtn solid" onClick=${() => call('dictation_stop', false)}>${command ? 'Run' : 'Stop'}</button>
    </div>`;
  }
  if (st.state === 'processing') {
    return html`<div class="pill" style="padding-inline-end:14px"><span class="dots"><span></span><span></span><span></span></span>Transcribing</div>`;
  }
  if (fl) {
    const icon = { ok: 'check', fail: 'alert', warn: 'alert', acc: 'copy', info: 'check' }[fl.kind] || 'check';
    const tone = { ok: 'ok', fail: 'warn', warn: 'warn', acc: 'acc', info: 'acc' }[fl.kind] || '';
    return html`<div class="pill">
      <span class=${tone}><${Icon} name=${icon} /></span><span class="text">${fl.message}</span>
      ${fl.outcome === 'pasted' && html`<button type="button" class="pbtn" onClick=${() => { call('copy_text', fl.text); flash('ok', 'Copied', {}, 1200); }}>Copy</button>
        <button type="button" class="pbtn" onClick=${() => call('paste_last')}>Again</button>`}
      ${fl.outcome === 'copied' && html`<button type="button" class="pbtn" onClick=${() => call('window_open', 'apps')}>Rule</button>`}
      ${fl.outcome === 'fallback' && html`<button type="button" class="pbtn" onClick=${() => call('window_open', 'live')}>Open</button>`}
      ${!fl.outcome && html`<span style="width:6px"></span>`}
    </div>`;
  }
  if (st.paused) {
    return html`<div class="pill"><${Icon} name="pause" /><span>Hotkeys paused</span>
      <button type="button" class="pbtn" onClick=${() => call('dictation_pause', false)}>Resume</button>${menuBtn}</div>`;
  }
  if (task) {
    const pct = task.progress != null ? task.progress * 100 : null;
    return html`<div class="pill" style="padding-inline-end:12px"><span class="warn"><${Icon} name="refresh" /></span>
      <span class="text">${task.label}</span>${pct != null && html`<span class="prog"><span style=${`width:${pct}%`}></span></span>`}</div>`;
  }
  if (hover) {
    return html`<div class="pill"><${Icon} name="mic" />${settings.hotkey.recording_mode === 'toggle' ? 'Press' : 'Hold'} <span class="pk">${keys}</span>${menuBtn}</div>`;
  }
  return html`<div class="pill rest" aria-label="Whisper Local is ready"><i></i></div>`;
}

function Bar() {
  const menu = bar.use((s) => s.menu);
  const ref = useRef(null);
  const last = useRef('');
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const report = () => {
      const r = el.getBoundingClientRect();
      const key = `${Math.ceil(r.width)}x${Math.ceil(r.height)}`;
      if (key !== last.current) { last.current = key; call('bar_resize', Math.ceil(r.width), Math.ceil(r.height)).catch(() => {}); }
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
  const running = (data.tasks || []).filter((t) => t.status === 'running');
  if (running.length) bar.set({ task: running[running.length - 1] });
}
boot();
