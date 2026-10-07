// screens/diagnostics.js
// Diagnostics: the health check (what --doctor checks) with next steps for
// known problems, the self-test (microphone, model, transcription, clipboard),
// a redacted bug-report zip, the shortcuts in effect, the files worth opening,
// the version and an update check.

import { html, useEffect, useState } from '../../vendor/preact-htm.module.js';
import { call } from '../api.js';
import { app, navigate, saveSetting, toast } from '../store.js';
import { Icon, Keys } from '../ui.js';

const ST = { ok: ['ok', 'check', 'Passed'], warn: ['warn', 'alert', 'Warning'], fail: ['rec', 'alert', 'Problem'], info: ['', 'dots', 'Info'] };

function Health() {
  const [checks, setChecks] = useState(null);
  const [busy, setBusy] = useState(false);
  const run = () => { setBusy(true); call('diag_checks').then(setChecks).catch((e) => toast(e.message, { kind: 'err' })).finally(() => setBusy(false)); };
  useEffect(run, []);
  const counts = checks ? checks.reduce((a, c) => ({ ...a, [c.status]: (a[c.status] || 0) + 1 }), {}) : {};
  const act = (a) => {
    if (a.action === 'gpu_install') call('gpu_install').then(() => toast('Installing GPU support in the background')).catch((e) => toast(e.message, { kind: 'err' }));
    if (a.action === 'open_url') call('app_open_url', a.arg);
  };
  let section = null;
  return html`<section class="card" style="grid-column:span 2;min-width:0" aria-labelledby="h-health">
    <div class="card-head"><div><h2 id="h-health">Health check</h2>
      <p>${checks ? `${counts.ok || 0} passed${counts.warn ? `, ${counts.warn} warning${counts.warn > 1 ? 's' : ''}` : ''}${counts.fail ? `, ${counts.fail} problem${counts.fail > 1 ? 's' : ''}` : ''}` : 'Checking…'}</p></div>
      <button type="button" class="btn pri" disabled=${busy} onClick=${run}><${Icon} name="refresh" />${busy ? 'Checking…' : 'Run again'}</button></div>
    ${checks && checks.map((c) => {
      const head = c.section !== section ? (section = c.section) : null;
      const [tone, icon, label] = ST[c.status] || ST.info;
      return html`${head && html`<div class="subhead"><div class="eyebrow">${head}</div></div>`}
        <div style="display:grid;grid-template-columns:26px minmax(0,1fr) auto;gap:4px 12px;align-items:center;padding:9px 18px;border-top:1px solid var(--line)">
          <span class=${tone ? `chip ${tone}` : 'chip'} style="width:22px;height:22px;padding:0;justify-content:center" aria-label=${label}><${Icon} name=${icon} size="sm" /></span>
          <div style="min-width:0"><span style="font-weight:500">${c.name}</span>${c.detail && html`<div class="small muted" style="overflow-wrap:anywhere">${c.detail}</div>`}</div>
          <div>${c.action && html`<button type="button" class="btn sm" onClick=${() => act(c.action)}>${c.action.label}</button>`}</div>
        </div>`;
    })}
  </section>`;
}

function SelfTest() {
  const tasks = app.use((s) => s.tasks);
  const [taskId, setTaskId] = useState(null);
  const [result, setResult] = useState(null);
  const task = taskId ? tasks[taskId] : null;
  useEffect(() => {
    if (task && task.status !== 'running' && !result) call('task_result', taskId).then((r) => setResult(r && r.result)).catch(() => {});
  }, [task && task.status]);
  const start = () => { setResult(null); call('diag_selftest').then(setTaskId).catch((e) => toast(e.message, { kind: 'err' })); };
  return html`<section class="card" style="min-width:0" aria-labelledby="h-self">
    <div class="card-head"><div><h2 id="h-self">Self-test</h2><p>Listens for 1.5 seconds, runs the model, and checks the clipboard. Say something while it runs.</p></div>
      <button type="button" class="btn" disabled=${task && task.status === 'running'} onClick=${start}>${task && task.status === 'running' ? 'Running…' : 'Start'}</button></div>
    <ol style="list-style:none;margin:0;padding:0 18px 16px;display:flex;flex-direction:column;gap:8px;font-size:13px">
      ${(result || []).map((s, i) => html`<li class="row-flex" style="justify-content:space-between;flex-wrap:nowrap"><span>${i + 1}. ${s.name}<div class="small muted">${s.detail}</div></span>
        <span class=${'chip ' + (s.ok ? 'ok' : 'rec')}>${s.ok ? `passed · ${s.seconds} s` : 'failed'}</span></li>`)}
      ${task && task.status === 'running' && html`<li class="muted">${task.detail || 'Starting…'}</li>`}
      ${!task && !result && html`<li class="muted">Not run yet.</li>`}
    </ol>
  </section>`;
}

function Shortcuts() {
  const [rows, setRows] = useState([]);
  useEffect(() => { call('diag_shortcuts').then(setRows).catch(() => {}); }, []);
  return html`<section class="card" style="min-width:0" aria-labelledby="h-keys">
    <div class="card-head"><div><h2 id="h-keys">Your shortcuts</h2><p>The keys in effect right now.</p></div>
      <button type="button" class="btn ghost sm" onClick=${() => navigate('settings/hotkeys')}>Change</button></div>
    <div style="padding:0 18px 12px">${rows.map((r, i) => html`<div class="row-flex" style="justify-content:space-between;padding:8px 0;font-size:13px;${i ? 'border-top:1px solid var(--line)' : ''}">
      <span>${r.action}</span><${Keys} hotkey=${r.keys.replace(/ /g, '').toLowerCase()} /></div>`)}</div>
  </section>`;
}

function Files() {
  const [p, setP] = useState(null);
  useEffect(() => { call('diag_paths').then(setP).catch(() => {}); }, []);
  const open = (kind) => call('diag_open', kind).catch((e) => toast(e.message, { kind: 'err' }));
  const rows = p ? [['data', 'Data folder', p.data], ['log', 'Log file', 'app.log'], ['model_cache', 'Model cache', p.model_cache || ''],
    ['settings', 'Settings file', 'user_settings.yaml'], ['commands', 'Commands file', 'commands.yaml'],
    ['crashes', 'Crash reports', `${p.crash_count} in the last 7 days`]] : [];
  return html`<section class="card" style="min-width:0" aria-labelledby="h-files">
    <div class="card-head"><div><h2 id="h-files">Files & folders</h2><p>For when you want to look under the hood.</p></div></div>
    <div style="padding:0 18px 16px;display:flex;flex-direction:column;gap:4px">
      ${rows.map(([k, l, d]) => html`<button type="button" class="btn ghost" style="justify-content:space-between" onClick=${() => open(k)}>${l}<span class="mono small muted" style="overflow:hidden;text-overflow:ellipsis">${d}</span></button>`)}
    </div>
  </section>`;
}

function Bundle() {
  return html`<section class="card" style="min-width:0" aria-labelledby="h-bundle">
    <div class="card-head"><div><h2 id="h-bundle">Report a bug</h2><p>A zip with logs, settings and the health check. Usernames, emails, favored words, the AI address and your prompt are blanked out.</p></div></div>
    <div class="row-flex" style="padding:0 18px 16px">
      <button type="button" class="btn" onClick=${() => call('diag_bundle').then((path) => path && toast(`Saved ${path}`)).catch((e) => toast(e.message, { kind: 'err' }))}>Create report zip…</button>
      <button type="button" class="btn ghost" onClick=${() => call('app_open_url', 'https://github.com/drajb/whisper-local/issues/new?template=bug_report.yml')}>Open the bug form</button></div>
  </section>`;
}

export function DiagnosticsScreen() {
  const version = app.use((s) => s.version);
  const webview2 = app.use((s) => s.webview2);
  const check = () => call('system_check_update').then((u) => toast(u.newer ? `Version ${u.latest} is available` : `You're up to date (${u.current})`,
    u.newer ? { action: { label: 'Open', run: () => call('app_open_url', 'https://github.com/drajb/whisper-local/releases/latest') } } : {}))
    .catch((e) => toast(`Couldn't check: ${e.message}`, { kind: 'err' }));
  return html`<div class="page">
    <div class="page-head">
      <div><h1 class="page-title">Diagnostics</h1><p class="page-sub">Version ${version}${webview2 ? ` · WebView2 ${webview2}` : ''} · everything is checked on this computer.</p></div>
      <div class="row-flex"><button type="button" class="btn" onClick=${check}>Check for updates</button>
        <button type="button" class="btn ghost" onClick=${() => navigate('welcome')}>Run setup again</button></div>
    </div>
    <div class="grid-cards"><${Health} /><${SelfTest} /><${Bundle} /><${Shortcuts} /><${Files} /></div>
  </div>`;
}
