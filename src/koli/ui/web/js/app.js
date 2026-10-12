// app.js
// Koli's app window: loads everything from the Python side once, keeps the
// stores in step with pushed events, and renders the shell (top bar with the
// Simple or Advanced tabs, the current screen, toasts and modals).

import { Component, html, render, useEffect, useMemo, useState } from '../vendor/preact-htm.module.js';
import { call, isMock, on } from './api.js';
import { app, applySettingLocally, getPath, live, navigate, routeFromHash, saveSetting, setting, toast } from './store.js';
import { Icon, ModalHost, Toasts } from './ui.js';
import { KoliMark } from './brand.js';
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
import { MeetingsScreen } from './screens/meetings.js';

const ADVANCED_NAV = [
  ['live', 'Live', 'wave'], ['history', 'History', 'clock'], ['meetings', 'Meetings', 'people'],
  ['vocabulary', 'Vocabulary', 'book'],
  ['commands', 'Commands', 'command'], ['apps', 'Apps', 'grid'],
  ['settings', 'Settings', 'sliders'], ['diagnostics', 'Diagnostics', 'pulse'],
];
const SIMPLE_NAV = [
  ['home', 'Home', 'home'], ['history', 'History', 'clock'], ['vocabulary', 'My words', 'book'],
  ['simple-settings', 'Settings', 'sliders'],
];
const SCREENS = {
  live: LiveScreen, home: HomeScreen, history: HistoryScreen, meetings: MeetingsScreen, vocabulary: VocabularyScreen,
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
    // A failed job arrives as a 'problem' too, which shows it (see showProblem).
  });
  on('problem', showProblem);
  on('settings', (p) => {
    applySettingLocally(p.path, p.new);
    if (p.path === 'ui.theme') applyTheme();
    if (p.source === 'file') toast(`${p.label} was changed outside the app — applied`);
    if (p.source === 'revert') call('settings_all').then((all) => app.set({ settings: all }));
    app.set({ undo: true });
  });
  on('foreground', (f) => app.set({ foreground: f }));
  on('profile', (p) => app.set({ profile: p.active }));
  on('meeting', (m) => app.set({ meeting: m }));
  on('notify', (n) => {
    if (n.kind === 'fallback') app.set({ notice: n });
    if (n.kind === 'settings_file') toast(n.message, { kind: 'warn', timeout: 0 });
    if (n.kind === 'command') toast(n.message, { kind: 'warn' });
  });
  on('devices_changed', () => app.set((s) => ({ devicesVersion: (s.devicesVersion || 0) + 1 })));
  on('window', (w) => {
    // Until the welcome steps are done, they stay on screen.
    if (w.action === 'navigate' && w.page && setting('ui.welcome_done') !== false) navigate(routeForMode(w.page));
    if (w.action === 'shown') refresh();
    if (w.action === 'frame') app.set({ frame: { custom: w.custom, maximized: w.maximized } });
  });
}

// Something that went wrong and must be seen: a toast that stays until it is
// closed, and closing it tells Koli it was seen. Problems from while the
// window was closed arrive with the bootstrap. Missing CUDA libraries can be
// fixed from the toast.
const shownProblems = new Set();
function showProblem(p) {
  if (!p || shownProblems.has(p.id)) return;
  shownProblems.add(p.id);
  const missingCuda = /cublas|cudnn|CUDA libraries/i.test(p.message);
  toast(p.message, {
    kind: 'err', timeout: 0,
    onDismiss: () => call('problem_dismiss', p.id).catch(() => {}),
    action: missingCuda ? { label: 'Install GPU support', run: () => call('gpu_install').then(() => toast('Installing GPU support in the background')).catch((e) => toast(e.message, { kind: 'err' })) } : null,
  });
}

async function refresh() {
  try {
    const data = await call('app_bootstrap', 'main');
    app.set({ settings: data.settings, state: data.state, logs: data.logs, pipeline: data.pipeline,
      tasks: Object.fromEntries(data.tasks.map((t) => [t.id, t])), recent: data.recent,
      foreground: data.foreground || {}, profile: data.profile, undo: data.undo, frame: data.frame || app.get().frame });
    (data.problems || []).forEach(showProblem);
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
    meeting: data.meeting || { state: 'idle' },
    // The title bar is removed once the page has loaded, which can be after
    // this reply was built: a frame event that already arrived is newer.
    frame: app.get().frame || data.frame || { custom: false, maximized: false },
  });
  applyTheme();
  (data.problems || []).forEach(showProblem);
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

// ── Shell: top bar with tabs ──────────────────────────────────────────────

function TaskChip() {
  const tasks = app.use((s) => s.tasks);
  const running = Object.values(tasks).filter((t) => t.status === 'running');
  if (!running.length) return null;
  const t = running[running.length - 1];
  const pct = t.progress != null ? ` · ${Math.round(t.progress * 100)}%` : '';
  return html`<span class="chip warn" title=${t.detail || 'Runs in the background. Dictation keeps working.'}>
    <${Icon} name="refresh" size="sm" />${t.label}${pct}${running.length > 1 ? ` (+${running.length - 1})` : ''}</span>`;
}

// What Koli is doing; clicking it pauses or resumes the hotkeys.
function StatusChip() {
  const st = app.use((s) => s.state);
  const toggle = () => call('dictation_pause', !st.paused).catch((e) => toast(e.message, { kind: 'err' }));
  const [cls, text] = st.paused ? ['warn', 'Paused'] : st.state === 'recording' ? ['rec', 'Listening']
    : st.state === 'processing' ? ['acc', 'Writing'] : st.state === 'model_loading' ? ['warn', 'Loading model'] : ['ok', 'Ready'];
  return html`<button type="button" class=${`chip ${cls}`} onClick=${toggle}
    title=${st.paused ? 'Hotkeys are paused. Click to resume.' : 'Click to pause the hotkeys'}
    aria-label=${`${text}. ${st.paused ? 'Resume hotkeys' : 'Pause hotkeys'}`}><span class="dot"></span>${text}</button>`;
}

function ProfileSelect() {
  const profile = app.use((s) => s.profile);
  const [profiles, setProfiles] = useState([]);
  useEffect(() => { call('profiles_list').then((r) => setProfiles(r.profiles.filter((p) => !p.disabled))).catch(() => {}); }, [profile]);
  if (!profiles.length) return null;
  return html`<select class="sel" aria-label="Active profile" style="height:38px;border-radius:30px;padding:0 12px" onChange=${(e) => {
    const name = e.currentTarget.value;
    call('profile_activate', name).then(() => { app.set({ profile: name }); toast(`Profile: ${name}`); })
      .catch((err) => toast(err.message, { kind: 'err' }));
  }}>
    ${!profile && html`<option>—</option>`}
    ${profiles.map((p) => html`<option value=${p.id} selected=${p.id === profile}>${p.id.charAt(0).toUpperCase() + p.id.slice(1)}</option>`)}
  </select>`;
}

// Ctrl+K: find any setting or screen.
function SearchPopover() {
  const schema = app.use((s) => s.schema);
  const mode = app.use((s) => getPath(s.settings, 'ui.mode'));
  const [q, setQ] = useState('');
  const results = useMemo(() => {
    const term = q.trim().toLowerCase();
    if (term.length < 2 || !schema) return [];
    const sections = Object.fromEntries(schema.sections.map((s) => [s.id, s.title]));
    const screens = [['Live', 'live'], ['History', 'history'], ['Meetings', 'meetings'], ['Vocabulary · favored words', 'vocabulary'],
      ['Snippets', 'vocabulary'], ['Corrections', 'vocabulary'], ['Voice commands', 'commands'],
      ['Transforms', 'commands'], ['App rules', 'apps'], ['Profiles', 'apps'], ['Diagnostics', 'diagnostics'],
      ['Self-test', 'diagnostics'], ['Report a bug', 'diagnostics']]
      .filter(([label]) => label.toLowerCase().includes(term)).map(([label, route]) => ({ label, route, where: 'Screen' }));
    const settings = schema.settings
      .filter((s) => sections[s.section] && s.mode !== 'hidden' && `${s.label} ${s.help || ''} ${s.path}`.toLowerCase().includes(term))
      .slice(0, 12).map((s) => ({ label: s.label, where: sections[s.section], route: `settings/${s.section}/${s.path}` }));
    return [...screens, ...settings];
  }, [q, schema]);
  const close = () => app.set({ searchOpen: false });
  const go = (r) => {
    if (mode !== 'advanced' && !SIMPLE_ROUTES.has(r.route.split('/')[0])) saveSetting('ui.mode', 'advanced', { quiet: true });
    navigate(r.route);
    close();
  };
  return html`<div class="search-pop" role="dialog" aria-label="Search">
    <label><${Icon} name="search" />
      <input autofocus aria-label="Search settings and screens" placeholder="Search settings, screens…" value=${q}
        ref=${(el) => el && setTimeout(() => el.focus(), 0)}
        onInput=${(e) => setQ(e.currentTarget.value)}
        onKeyDown=${(e) => { if (e.key === 'Enter' && results[0]) go(results[0]); if (e.key === 'Escape') close(); }} />
      <button type="button" class="btn ghost icon" aria-label="Close search" onClick=${close}><${Icon} name="x" size="sm" /></button></label>
    ${results.length > 0 && html`<div class="popover search-results" role="listbox">
      ${results.map((r) => html`<button type="button" class="menu-item" onClick=${() => go(r)}>
        <span class="grow">${r.label}</span><span class="muted small">${r.where}</span></button>`)}
    </div>`}
  </div>`;
}

// Minimize, maximize/restore and close, drawn by the page when the window has
// no Windows title bar. Close hides the window; Koli keeps running in the bar.
function WindowControls() {
  const frame = app.use((s) => s.frame);
  if (!frame || !frame.custom) return null;
  const run = (name) => call(name).catch((e) => toast(e.message, { kind: 'err' }));
  const maxLabel = frame.maximized ? 'Restore' : 'Maximize';
  return html`<div class="win-controls" role="group" aria-label="Window">
    <button type="button" class="win-btn" aria-label="Minimize" title="Minimize" onClick=${() => run('window_minimize')}>
      <${Icon} name="win-min" size="sm" /></button>
    <button type="button" class="win-btn" aria-label=${maxLabel} title=${maxLabel} onClick=${() => run('window_toggle_maximize')}>
      <${Icon} name=${frame.maximized ? 'win-restore' : 'win-max'} size="sm" /></button>
    <button type="button" class="win-btn close" aria-label="Close" title="Close (Koli keeps running)" onClick=${() => run('window_close')}>
      <${Icon} name="x" size="sm" /></button>
  </div>`;
}

// The welcome steps have no top bar; this strip moves the window and holds
// the window buttons there.
function TitleStrip() {
  const custom = app.use((s) => s.frame && s.frame.custom);
  return custom ? html`<div class="titlebar-strip"><${WindowControls} /></div>` : null;
}

// Header monitor: memory on the graphics card Koli runs on and how much of it
// is Koli's, or Koli's memory and processor use on the CPU. Polled every few
// seconds while the window shows; the tooltip has the details.
function ResourceChip() {
  const [r, setR] = useState(null);
  useEffect(() => {
    let alive = true;
    const poll = () => {
      if (document.visibilityState === 'visible') call('resources').then((d) => { if (alive) setR(d); }).catch(() => {});
    };
    poll();
    const timer = setInterval(poll, 3000);
    return () => { alive = false; clearInterval(timer); };
  }, []);
  if (!r) return null;
  const gb = (mb) => (mb / 1024).toFixed(1);
  const models = `Models: ${(r.models || []).join(', ') || 'none'}`;
  if (r.device === 'cuda' && r.gpu) {
    const share = r.gpu.used_mb / r.gpu.total_mb;
    const mine = r.koli_gpu_mb != null ? gb(r.koli_gpu_mb) : null;
    const tip = [`${r.gpu.name} (card ${r.gpu_index})`, `In use on the card: ${gb(r.gpu.used_mb)} of ${gb(r.gpu.total_mb)} GB`,
      `Koli: ${mine != null ? `${mine} GB` : 'unknown'}`, models].join('\n');
    return html`<span class=${'chip res' + (share > 0.85 ? ' warn' : '')} title=${tip} aria-label=${tip}>
      ${mine != null ? `Koli ${mine} · ` : ''}GPU ${gb(r.gpu.used_mb)}/${gb(r.gpu.total_mb)} GB
      <span class="res-bar" aria-hidden="true"><span style=${`width:${Math.min(100, Math.round(share * 100))}%`}></span></span></span>`;
  }
  const tip = ['Koli runs on the processor', `Memory: ${gb(r.ram_mb)} GB · CPU ${r.cpu_percent}%`, models].join('\n');
  return html`<span class="chip res" title=${tip} aria-label=${tip}>RAM ${gb(r.ram_mb)} GB · CPU ${r.cpu_percent}%</span>`;
}

function TopBar({ route }) {
  const mode = app.use((s) => getPath(s.settings, 'ui.mode')) || 'simple';
  const theme = app.use((s) => getPath(s.settings, 'ui.theme'));
  const searchOpen = app.use((s) => s.searchOpen);
  const customFrame = app.use((s) => s.frame && s.frame.custom);
  const dark = document.documentElement.dataset.theme === 'dark';
  const nav = mode === 'advanced' ? ADVANCED_NAV : SIMPLE_NAV;
  const base = route.split('/')[0];
  const switchMode = (m) => {
    saveSetting('ui.mode', m, { quiet: true });
    navigate(m === 'advanced' ? (base === 'simple-settings' ? 'settings' : base === 'home' ? 'live' : base) : (SIMPLE_ROUTES.has(base) ? base : base === 'settings' ? 'simple-settings' : 'home'));
  };
  return html`<header class=${customFrame ? 'topbar has-win' : 'topbar'}>
    <div class="brand"><${KoliMark} size=${36} />koli</div>
    <nav class="tabs" aria-label="Main">
      ${nav.map(([id, label]) => html`<a class=${'tab' + (base === id ? ' on' : '')} href=${`#/${id}`} aria-current=${base === id ? 'page' : undefined}>${label}</a>`)}
    </nav>
    <div class="top-actions">
      <${ResourceChip} /><${TaskChip} /><${StatusChip} />
      ${mode === 'advanced' && html`<${ProfileSelect} />`}
      <button type="button" class="btn ghost icon" aria-label="Search (Ctrl K)" title="Search · Ctrl K" onClick=${() => app.set({ searchOpen: !searchOpen })}><${Icon} name="search" /></button>
      <label class="mode-switch">Advanced <input type="checkbox" class="sw" checked=${mode === 'advanced'} onChange=${(e) => switchMode(e.currentTarget.checked ? 'advanced' : 'simple')} /></label>
      <button type="button" class="btn ghost icon" aria-label=${dark ? 'Switch to light mode' : 'Switch to dark mode'}
        title=${theme === 'system' ? 'Following Windows' : ''}
        onClick=${() => saveSetting('ui.theme', dark ? 'light' : 'dark', { quiet: true })}>
        <${Icon} name=${dark ? 'sun' : 'moon'} /></button>
    </div>
    ${searchOpen && html`<${SearchPopover} />`}
    <${WindowControls} />
  </header>`;
}

// Settings sections as a row of tabs under the top bar.
function SettingsNav({ route }) {
  const schema = app.use((s) => s.schema);
  if (!schema) return null;
  const current = route.split('/')[1];
  return html`<nav class="subnav" aria-label="Settings sections">
    ${schema.sections.map((s) => html`<a class=${current === s.id ? 'on' : ''} href=${`#/settings/${s.id}`} aria-current=${current === s.id ? 'page' : undefined}>${s.title}</a>`)}
  </nav>`;
}

function App() {
  const ready = app.use((s) => s.ready);
  const route = app.use((s) => s.route);
  useEffect(() => {
    const onKey = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        app.set({ searchOpen: true });
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
  if (!ready) return html`<div class="empty" style="padding-top:30vh">Starting Koli…</div>`;
  const base = route.split('/')[0];
  const Screen = SCREENS[base] || LiveScreen;
  if (base === 'welcome') {
    return html`<div class="app"><${TitleStrip} /><div class="content"><${ScreenBoundary} route=${route}><${Screen} /></${ScreenBoundary}></div><${Toasts} /><${ModalHost} /></div>`;
  }
  return html`<div class="app">
    <${TopBar} route=${route} />
    <div class="body">
      ${base === 'settings' && html`<${SettingsNav} route=${route} />`}
      <main class="content" id="content"><${ScreenBoundary} route=${route}><${Screen} route=${route} /></${ScreenBoundary}></main>
    </div>
    <${Toasts} /><${ModalHost} />
  </div>`;
}

render(html`<${App} />`, document.getElementById('root'));
boot().catch((e) => {
  document.getElementById('root').innerHTML = '';
  render(html`<div class="empty" style="padding-top:20vh">Couldn't connect to Koli: ${e.message}</div>`,
    document.getElementById('root'));
});
if (isMock) document.title = 'Koli (mock)';
