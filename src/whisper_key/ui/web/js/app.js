// app.js
// The app window: loads everything from the Python side once, keeps the stores
// in step with pushed events, and renders the shell (toolbar, sidebar for
// Simple or Advanced mode, the current screen, toasts and modals).

import { Component, html, render, useEffect, useMemo, useState } from '../vendor/preact-htm.module.js';
import { call, isMock, on } from './api.js';
import { app, applySettingLocally, getPath, live, navigate, routeFromHash, saveSetting, setting, toast } from './store.js';
import { Icon, ModalHost, Toasts } from './ui.js';
import { LiveScreen } from './screens/live.js';
import { HomeScreen } from './screens/home.js';
import { HistoryScreen } from './screens/history.js';
import { VocabularyScreen } from './screens/vocabulary.js';
import { CommandsScreen } from './screens/commands.js';
import { AppsScreen } from './screens/apps.js';
import { SettingsScreen } from './screens/settings.js';
import { SimpleSettingsScreen } from './screens/simple.js';
import { DiagnosticsScreen } from './screens/diagnostics.js';
import { WelcomeScreen } from './screens/welcome.js';

const ADVANCED_NAV = [
  ['live', 'Live', 'wave'], ['history', 'History & insights', 'clock'], ['vocabulary', 'Vocabulary', 'book'],
  ['commands', 'Commands & AI', 'command'], ['apps', 'Apps & profiles', 'grid'],
  ['settings', 'Settings', 'sliders'], ['diagnostics', 'Diagnostics', 'pulse'],
];
const SIMPLE_NAV = [
  ['home', 'Home', 'home'], ['history', 'History', 'clock'], ['vocabulary', 'My words', 'book'],
  ['simple-settings', 'Settings', 'sliders'],
];
const SCREENS = {
  live: LiveScreen, home: HomeScreen, history: HistoryScreen, vocabulary: VocabularyScreen,
  commands: CommandsScreen, apps: AppsScreen, settings: SettingsScreen, 'simple-settings': SimpleSettingsScreen,
  diagnostics: DiagnosticsScreen, welcome: WelcomeScreen,
};
const SIMPLE_ROUTES = new Set(['home', 'history', 'vocabulary', 'simple-settings', 'welcome']);

// A page asked for from outside (tray, --settings) in Simple mode goes to the
// Simple screen for the same thing; Advanced-only pages switch to Advanced.
function routeForMode(route) {
  const base = route.split('/')[0];
  const mode = getPath(app.get().settings, 'ui.mode');
  if (!mode || mode === 'advanced' || SIMPLE_ROUTES.has(base)) return route;
  if (base === 'settings') return 'simple-settings';
  if (base === 'live') return 'home';
  saveSetting('ui.mode', 'advanced', { quiet: true });
  return route;
}

// A screen that throws shows a short message (and goes to the log) instead of
// taking the whole window down; moving to another screen clears it.
class ScreenBoundary extends Component {
  componentDidCatch(error) {
    this.setState({ error });
    if (!isMock) call('js_error', `${this.props.route}: ${error && error.message}`, 'screen', 0).catch(() => {});
  }
  componentDidUpdate(prev) {
    if (prev.route !== this.props.route && this.state.error) this.setState({ error: null });
  }
  render({ children }, { error }) {
    if (!error) return children;
    return html`<div class="page"><div class="notice warn" role="alert"><span>This screen ran into a problem: ${error.message}.
      It has been written to the log.</span></div></div>`;
  }
}

// ── Theme ─────────────────────────────────────────────────────────────────

const media = window.matchMedia('(prefers-color-scheme: dark)');
function applyTheme() {
  const theme = setting('ui.theme') || 'light';
  const dark = theme === 'dark' || (theme === 'system' && media.matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
}
media.addEventListener('change', applyTheme);

// ── Events from the app ───────────────────────────────────────────────────

function wireEvents() {
  on('state', (s) => {
    app.set({ state: { ...app.get().state, ...s } });
    // Meters describe the current recording only; clear them when it ends.
    if (s.state && s.state !== 'recording') live.set({ level: 0, vad: null });
  });
  on('level', (p) => {
    const levels = live.get().levels.slice(1);
    levels.push(p.level);
    live.set({ level: p.level, levels });
  });
  on('vad', (v) => live.set({ vad: v }));
  on('streaming_text', (p) => app.set({ streaming: p.text }));
  on('pipeline', (p) => {
    const pipeline = p.stage === 'captured' || p.stage === 'no_audio' ? [p] : [...app.get().pipeline, p];
    app.set({ pipeline, streaming: p.stage === 'captured' ? '' : app.get().streaming });
    if (p.stage === 'delivered' && p.text) {
      app.set((s) => ({ recent: [p.text, ...s.recent.filter((x) => x !== p.text)].slice(0, 10) }));
    }
  });
  on('log', (rec) => {
    const logs = app.get().logs;
    logs.push(rec);
    if (logs.length > 2000) logs.splice(0, logs.length - 2000);
    app.set({ logs });
  });
  on('task', (t) => {
    const prev = app.get().tasks[t.id];
    app.set((s) => ({ tasks: { ...s.tasks, [t.id]: t } }));
    if (prev && prev.status === 'running' && t.status === 'done') toast(`Done · ${t.label}`);
    if (t.status === 'failed' && (!prev || prev.status === 'running')) toast(`${t.label} failed: ${t.error}`, { kind: 'err', timeout: 9000 });
  });
  on('settings', (p) => {
    applySettingLocally(p.path, p.new);
    if (p.path === 'ui.theme') applyTheme();
    if (p.source === 'file') toast(`${p.label} was changed outside the app — applied`);
    if (p.source === 'revert') call('settings_all').then((all) => app.set({ settings: all }));
    app.set({ undo: true });
  });
  on('foreground', (f) => app.set({ foreground: f }));
  on('profile', (p) => app.set({ profile: p.active }));
  on('notify', (n) => {
    if (n.kind === 'fallback') app.set({ notice: n });
    if (n.kind === 'settings_file') toast(n.message, { kind: 'warn', timeout: 0 });
  });
  on('devices_changed', () => app.set((s) => ({ devicesVersion: (s.devicesVersion || 0) + 1 })));
  on('window', (w) => {
    // Until the welcome steps are done, they stay on screen.
    if (w.action === 'navigate' && w.page && setting('ui.welcome_done') !== false) navigate(routeForMode(w.page));
    if (w.action === 'shown') refresh();
  });
}

async function refresh() {
  try {
    const data = await call('app_bootstrap', 'main');
    app.set({ settings: data.settings, state: data.state, logs: data.logs, pipeline: data.pipeline,
      tasks: Object.fromEntries(data.tasks.map((t) => [t.id, t])), recent: data.recent,
      foreground: data.foreground || {}, profile: data.profile, undo: data.undo });
    applyTheme();
  } catch (e) { /* the next event will catch up */ }
}

async function boot() {
  wireEvents();
  const data = await call('app_bootstrap', 'main');
  app.set({
    ready: true, version: data.version, settings: data.settings, schema: data.schema, state: data.state,
    logs: data.logs || [], pipeline: data.pipeline || [], recent: data.recent || [],
    tasks: Object.fromEntries((data.tasks || []).map((t) => [t.id, t])), foreground: data.foreground || {},
    profile: data.profile, undo: data.undo, dev: data.dev, webview2: data.webview2,
  });
  applyTheme();
  const mode = data.settings.ui && data.settings.ui.mode;
  const start = routeFromHash() || (data.settings.ui && !data.settings.ui.welcome_done ? 'welcome' : mode === 'advanced' ? 'live' : 'home');
  navigate(start);
  // Problems from startup (the GPU didn't load, say) stay until dismissed.
  for (const n of data.notices || []) {
    const act = n.action && n.action.action === 'gpu_install'
      ? { label: n.action.label, run: () => call('gpu_install').then(() => toast('Installing GPU support in the background')).catch((e) => toast(e.message, { kind: 'err' })) }
      : null;
    toast(n.message, { kind: 'warn', action: act, timeout: 0 });
  }
  window.addEventListener('hashchange', () => app.set({ route: routeFromHash() || app.get().route }));
}

// ── Shell ─────────────────────────────────────────────────────────────────

function TaskChip() {
  const tasks = app.use((s) => s.tasks);
  const running = Object.values(tasks).filter((t) => t.status === 'running');
  if (!running.length) return null;
  const t = running[running.length - 1];
  const pct = t.progress != null ? ` · ${Math.round(t.progress * 100)}%` : '';
  return html`<span class="chip warn" title=${t.detail || 'Runs in the background. Dictation keeps working.'}>
    <${Icon} name="refresh" size="sm" />${t.label}${pct}${running.length > 1 ? ` (+${running.length - 1})` : ''}</span>`;
}

function StatusChip() {
  const st = app.use((s) => s.state);
  if (st.paused) return html`<span class="chip warn"><span class="dot"></span>Hotkeys paused</span>`;
  if (st.state === 'recording') return html`<span class="chip rec"><span class="dot"></span>Recording</span>`;
  if (st.state === 'processing') return html`<span class="chip acc"><span class="dot"></span>Transcribing</span>`;
  if (st.state === 'model_loading') return html`<span class="chip warn"><span class="dot"></span>Loading model</span>`;
  return html`<span class="chip ok"><span class="dot"></span>Ready</span>`;
}

function Search() {
  const schema = app.use((s) => s.schema);
  const mode = app.use((s) => getPath(s.settings, 'ui.mode'));
  const [q, setQ] = useState('');
  const results = useMemo(() => {
    const term = q.trim().toLowerCase();
    if (term.length < 2 || !schema) return [];
    const sections = Object.fromEntries(schema.sections.map((s) => [s.id, s.title]));
    const screens = [['Live', 'live'], ['History', 'history'], ['Vocabulary · favored words', 'vocabulary'],
      ['Snippets', 'vocabulary'], ['Corrections', 'vocabulary'], ['Voice commands', 'commands'],
      ['Transforms', 'commands'], ['App rules', 'apps'], ['Profiles', 'apps'], ['Diagnostics', 'diagnostics'],
      ['Self-test', 'diagnostics'], ['Report a bug', 'diagnostics']]
      .filter(([label]) => label.toLowerCase().includes(term)).map(([label, route]) => ({ label, route, where: 'Screen' }));
    const settings = schema.settings
      .filter((s) => sections[s.section] && `${s.label} ${s.help || ''} ${s.path}`.toLowerCase().includes(term))
      .slice(0, 12).map((s) => ({ label: s.label, where: sections[s.section], route: `settings/${s.section}/${s.path}` }));
    return [...screens, ...settings];
  }, [q, schema]);
  const go = (r) => {
    if (mode !== 'advanced' && !SIMPLE_ROUTES.has(r.route.split('/')[0])) saveSetting('ui.mode', 'advanced', { quiet: true });
    navigate(r.route);
    setQ('');
  };
  return html`<div class="search">
    <label><${Icon} name="search" />
      <input aria-label="Search settings and screens" placeholder="Search settings, screens…" value=${q}
        onInput=${(e) => setQ(e.currentTarget.value)}
        onKeyDown=${(e) => { if (e.key === 'Enter' && results[0]) go(results[0]); if (e.key === 'Escape') setQ(''); }} />
      <span class="kbd">Ctrl K</span></label>
    ${results.length > 0 && html`<div class="popover search-results" role="listbox">
      ${results.map((r) => html`<button type="button" class="menu-item" onClick=${() => go(r)}>
        <span class="grow">${r.label}</span><span class="muted small">${r.where}</span></button>`)}
    </div>`}
  </div>`;
}

function Toolbar() {
  const theme = app.use((s) => getPath(s.settings, 'ui.theme'));
  const dark = document.documentElement.dataset.theme === 'dark';
  return html`<header class="toolbar">
    <div class="brand"><span class="brand-mark"><${Icon} name="wave" size="sm" /></span>Whisper Local</div>
    <${Search} />
    <div class="row-flex">
      <${TaskChip} /><${StatusChip} />
      <button type="button" class="btn ghost icon" aria-label=${dark ? 'Switch to light mode' : 'Switch to dark mode'}
        title=${theme === 'system' ? 'Following Windows' : ''}
        onClick=${() => saveSetting('ui.theme', dark ? 'light' : 'dark', { quiet: true })}>
        <${Icon} name=${dark ? 'sun' : 'moon'} /></button>
    </div>
  </header>`;
}

function Sidebar({ route }) {
  const mode = app.use((s) => getPath(s.settings, 'ui.mode')) || 'simple';
  const schema = app.use((s) => s.schema);
  const profile = app.use((s) => s.profile);
  const paused = app.use((s) => s.state.paused);
  const recordKey = app.use((s) => getPath(s.settings, 'hotkey.recording_hotkey'));
  const [profiles, setProfiles] = useState([]);
  useEffect(() => { call('profiles_list').then((r) => setProfiles(r.profiles.filter((p) => !p.disabled))).catch(() => {}); }, [profile]);
  const nav = mode === 'advanced' ? ADVANCED_NAV : SIMPLE_NAV;
  const base = route.split('/')[0];
  const switchMode = (m) => {
    saveSetting('ui.mode', m, { quiet: true });
    navigate(m === 'advanced' ? (base === 'simple-settings' ? 'settings' : base === 'home' ? 'live' : base) : (SIMPLE_ROUTES.has(base) ? base : base === 'settings' ? 'simple-settings' : 'home'));
  };
  return html`<nav class="sidebar" aria-label="Main">
    ${nav.map(([id, label, icon]) => html`
      <a class=${'nav-link' + (base === id ? ' on' : '')} href=${`#/${id}`} aria-current=${base === id ? 'page' : undefined}>
        <${Icon} name=${icon} />${label}</a>
      ${id === 'settings' && base === 'settings' && schema && html`<div class="nav-sub">
        ${schema.sections.map((s) => html`<a class=${'nav-link' + (route.split('/')[1] === s.id ? ' on' : '')} href=${`#/settings/${s.id}`}>${s.title}</a>`)}
      </div>`}`)}
    <div class="sidebar-foot">
      ${mode === 'advanced' && html`<div class="card" style="padding:12px;display:flex;flex-direction:column;gap:10px">
        <label class="stack" style="gap:6px"><span class="eyebrow">Profile</span>
          <select class="sel" aria-label="Active profile" onChange=${(e) => {
            const name = e.currentTarget.value;
            call('profile_activate', name).then(() => { app.set({ profile: name }); toast(`Profile: ${name}`); })
              .catch((err) => toast(err.message, { kind: 'err' }));
          }}>
            ${!profile && html`<option>—</option>`}
            ${profiles.map((p) => html`<option value=${p.id} selected=${p.id === profile}>${p.id.charAt(0).toUpperCase() + p.id.slice(1)}</option>`)}
          </select></label>
        <label class="row-flex" style="justify-content:space-between"><span style="font-size:13px;font-weight:500">Hotkeys active</span>
          <input type="checkbox" class="sw" checked=${!paused} aria-label="Hotkeys active"
            onChange=${(e) => call('dictation_pause', !e.currentTarget.checked)} /></label>
      </div>`}
      <div class="small muted" style="padding:0 4px">Hold ${recordKey ? recordKey.split('+').map((k) => html`<span class="kbd" style="margin-inline:2px">${k.charAt(0).toUpperCase() + k.slice(1)}</span>`) : ''} anywhere to dictate.</div>
      <div class="seg" role="radiogroup" aria-label="Interface mode" style="width:100%">
        ${[['simple', 'Simple'], ['advanced', 'Advanced']].map(([v, t]) => html`<button type="button" role="radio"
          aria-checked=${mode === v} class=${mode === v ? 'on' : ''} style="flex:1" onClick=${() => switchMode(v)}>${t}</button>`)}
      </div>
    </div>
  </nav>`;
}

function App() {
  const ready = app.use((s) => s.ready);
  const route = app.use((s) => s.route);
  useEffect(() => {
    const onKey = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        const input = document.querySelector('.search input');
        if (input) input.focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
  if (!ready) return html`<div class="empty" style="padding-top:30vh">Starting Whisper Local…</div>`;
  const base = route.split('/')[0];
  const Screen = SCREENS[base] || LiveScreen;
  if (base === 'welcome') {
    return html`<div class="app"><div class="content"><${ScreenBoundary} route=${route}><${Screen} /></${ScreenBoundary}></div><${Toasts} /><${ModalHost} /></div>`;
  }
  return html`<div class="app">
    <${Toolbar} />
    <div class="body">
      <${Sidebar} route=${route} />
      <main class="content" id="content"><${ScreenBoundary} route=${route}><${Screen} route=${route} /></${ScreenBoundary}></main>
    </div>
    <${Toasts} /><${ModalHost} />
  </div>`;
}

render(html`<${App} />`, document.getElementById('root'));
boot().catch((e) => {
  document.getElementById('root').innerHTML = '';
  render(html`<div class="empty" style="padding-top:20vh">Couldn't connect to Whisper Local: ${e.message}</div>`,
    document.getElementById('root'));
});
if (isMock) document.title = 'Whisper Local (mock)';
