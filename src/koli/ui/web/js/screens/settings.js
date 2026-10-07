// screens/settings.js
// Advanced Settings: every setting in the schema, grouped into the eleven
// sections, each saved the moment it changes. Custom editors cover what a
// single control can't: the models table, GPU setup, voice-cue table, your
// own writing styles, start on login, backups, the local API, reset and
// uninstall. A search result scrolls to its row and highlights it.

import { html, useEffect, useState } from '../../vendor/preact-htm.module.js';
import { call } from '../api.js';
import { app, confirmModal, fmtBytes, navigate, openModal, closeModal, promptModal, saveSetting, toast, undo, useSetting } from '../store.js';
import { Icon, Select, Toggle } from '../ui.js';
import { BgBadge, MicTest, SettingRow, settingsBySection, TaskProgress, useRunningTasks } from './shared.js';

// ── Models ────────────────────────────────────────────────────────────────

const useTasks = useRunningTasks;

function ModelsTable() {
  const current = useSetting('whisper.model');
  const modelsVersion = useSetting('whisper.models');
  const tasks = app.use((s) => s.tasks);
  const [data, setData] = useState(null);
  const reload = () => call('models_list').then(setData).catch((e) => toast(e.message, { kind: 'err' }));
  const running = Object.values(tasks).filter((t) => t.status === 'running');
  const finishedCount = Object.values(tasks).filter((t) => t.status !== 'running').length;
  useEffect(() => { reload(); }, [current, modelsVersion, finishedCount]);
  if (!data) return html`<div class="empty">Loading models…</div>`;
  // Downloads and model switches are tagged with the model they are for.
  const progressFor = (key) => running.find((t) => t.ref === key && (t.kind === 'download' || t.kind === 'engine')) || null;
  const remove = async (m) => {
    if (!(await confirmModal({ title: `Delete ${m.label}?`, body: `Frees ${fmtBytes(m.size_bytes)}. You can download it again any time.`, confirm: 'Delete', danger: true }))) return;
    call('model_delete', m.key).then(() => { toast(`Deleted ${m.label}`); reload(); }).catch((e) => toast(e.message, { kind: 'err' }));
  };
  const addHf = async () => {
    const repo = await promptModal({ title: 'Add a model from Hugging Face', label: 'Repository (owner/name)', placeholder: 'ivrit-ai/whisper-large-v3-turbo-ct2', confirm: 'Add' });
    if (repo) call('model_add', 'hf', repo).then((k) => { toast(`Added ${k}`); reload(); }).catch((e) => toast(e.message, { kind: 'err' }));
  };
  const addFolder = () => call('pick_folder', 'Choose a CTranslate2 model folder')
    .then((p) => p && call('model_add', 'folder', p)).then((k) => { if (k) { toast(`Added ${k}`); reload(); } })
    .catch((e) => toast(e.message, { kind: 'err' }));
  return html`<div class="tbl-wrap"><table class="tbl">
    <thead><tr><th scope="col" style="width:44px"><span class="sr-only">In use</span></th><th scope="col">Model</th>
      <th scope="col">Size</th><th scope="col">Status</th><th scope="col">In menus</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead>
    <tbody>
      ${data.groups.map((g) => {
        const rows = data.models.filter((m) => m.group === g.id);
        if (!rows.length && g.id !== 'yours') return null;
        return html`<tr class="grp"><td colspan="6">${g.title}</td></tr>
          ${rows.map((m) => {
            const p = progressFor(m.key);
            return html`<tr>
              <td><input type="radio" name="model" checked=${m.key === current} aria-label=${`Use ${m.label}`}
                onChange=${() => saveSetting('whisper.model', m.key)} /></td>
              <td><span style=${m.in_use ? 'font-weight:600' : ''}>${m.label}</span>${m.in_use && html` <span class="chip acc sm">In use</span>`}
                <div class="small muted mono">${m.source}</div></td>
              <td class="mono">${m.cached ? fmtBytes(m.size_bytes) : m.size_hint || ''}</td>
              <td>${p ? html`<${TaskProgress} task=${p} />`
                : m.cached ? html`<span class="chip ok">${m.local ? 'Your folder' : 'On disk'}</span>`
                : html`<button type="button" class="btn ghost sm" onClick=${() => call('model_download', m.key).then(() => toast(`Downloading ${m.label}`))}><${Icon} name="download" size="sm" />Download</button>`}</td>
              <td><${Toggle} checked=${m.enabled} label=${`Show ${m.label} in menus`} onChange=${(v) => call('model_set_visible', m.key, v).then(reload)} /></td>
              <td class="end">${m.deletable && html`<button type="button" class="btn ghost sm" onClick=${() => remove(m)}>Delete</button>`}
                ${g.id === 'yours' && !m.in_use && html`<button type="button" class="btn ghost sm" onClick=${() => call('model_remove', m.key).then(reload)}>Remove</button>`}</td>
            </tr>`;
          })}`;
      })}
      <tr><td colspan="6"><div class="row-flex">
        <button type="button" class="btn" onClick=${addHf}><${Icon} name="plus" />Add from Hugging Face…</button>
        <button type="button" class="btn" onClick=${addFolder}>Add a local folder…</button>
        <span class="small muted">Any CTranslate2 Whisper model.</span></div></td></tr>
    </tbody></table></div>`;
}

// The graphics card: whether GPU support is installed (with an Install button
// and its progress), and which card to use when there is more than one.
function GpuRow() {
  const [gpu, setGpu] = useState(null);
  const [cards, setCards] = useState([]);
  const installing = useTasks('gpu');
  const switching = useTasks('engine').find((t) => !t.ref);
  const device = useSetting('whisper.device');
  const cardIndex = useSetting('whisper.device_index') || 0;
  useEffect(() => { call('gpu_detect').then(setGpu).catch(() => setGpu({})); }, [installing.length]);
  useEffect(() => { call('gpu_list').then(setCards).catch(() => setCards([])); }, []);
  const install = async () => {
    if (!(await confirmModal({ title: 'Install GPU support?', body: 'Downloads about 1.2 GB of NVIDIA libraries in the background, then switches dictation to the GPU. You can keep dictating meanwhile.', confirm: 'Install' }))) return;
    call('gpu_install').catch((e) => toast(e.message, { kind: 'err' }));
  };
  if (!gpu) return null;
  const status = gpu.name ? `${gpu.ready ? 'Ready to use' : gpu.missing && gpu.missing.length ? `Missing ${gpu.missing.join(', ')}` : 'GPU libraries not installed'}.`
    : 'No supported graphics card found. CPU works fine.';
  const chosen = cards.find((c) => c.index === cardIndex);
  return html`<div class="srow"><div class="slabel"><b>Graphics card</b>
      <small>${cards.length > 1 ? `${cards.length} cards found. ` : chosen || cards[0] ? `${(chosen || cards[0]).name}. ` : gpu.name ? `${gpu.name}. ` : ''}${status}${cards.length > 1 && device !== 'cuda' ? ' Your choice applies when “Run on” is GPU.' : ''}</small></div>
    <div class="sctl" style="flex-direction:column;align-items:flex-end;gap:8px">
      ${cards.length > 1 && html`<${Select} label="Graphics card" value=${cardIndex} style="max-width:320px"
        options=${cards.map((c) => [c.index, `${c.index + 1}. ${c.name}${c.memory_gb ? ` · ${c.memory_gb} GB` : ''}`])}
        onChange=${(v) => saveSetting('whisper.device_index', v)} />`}
      ${installing.length ? html`<${TaskProgress} task=${installing[0]} cancel=${false} />`
        : gpu.name && !gpu.ready && gpu.class !== 'amd_rdna1' && html`<button type="button" class="btn" onClick=${install}>Install GPU support</button>`}
      ${switching && html`<${TaskProgress} task=${switching} />`}
    </div></div>`;
}

// ── Voice cue table and styles ────────────────────────────────────────────

const showEsc = (s) => String(s ?? '').replace(/\n/g, '\\n');
const readEsc = (s) => String(s ?? '').replace(/\\n/g, '\n');

function CueTable() {
  const items = useSetting('postprocess.inline_formatting_replacements') || [];
  const save = (next) => saveSetting('postprocess.inline_formatting_replacements', next);
  const update = (i, key, val) => save(items.map((it, n) => (n === i ? { ...it, [key]: val } : it)));
  return html`<div class="tbl-wrap" style="border-top:1px solid var(--line)"><table class="tbl">
    <thead><tr><th scope="col">When you say</th><th scope="col">Insert (\\n = new line)</th><th scope="col"><span class="sr-only">Delete</span></th></tr></thead>
    <tbody>
      ${items.length === 0 && html`<tr><td colspan="3" class="muted">No cue words of your own. The English ones (“comma”, “period”, “new line”…) work by default.</td></tr>`}
      ${items.map((it, i) => html`<tr>
        <td><input class="inp" dir="auto" value=${it.phrase} aria-label="Phrase" onBlur=${(e) => e.currentTarget.value !== it.phrase && update(i, 'phrase', e.currentTarget.value)} /></td>
        <td><input class="inp mono" value=${showEsc(it.replacement)} aria-label="Insert" onBlur=${(e) => readEsc(e.currentTarget.value) !== it.replacement && update(i, 'replacement', readEsc(e.currentTarget.value))} /></td>
        <td class="end"><button type="button" class="btn ghost icon" aria-label=${`Delete ${it.phrase}`} onClick=${() => save(items.filter((_, n) => n !== i))}><${Icon} name="x" /></button></td>
      </tr>`)}
      <tr><td colspan="3"><button type="button" class="btn ghost" onClick=${async () => {
        const phrase = await promptModal({ title: 'Add a cue word', label: 'When you say', confirm: 'Next' });
        if (!phrase) return;
        const rep = await promptModal({ title: 'Add a cue word', label: `Insert for “${phrase}” (\\n for a new line)`, value: ', ', confirm: 'Add' });
        if (rep != null) save([...items, { phrase, replacement: readEsc(rep) }]);
      }}><${Icon} name="plus" />Add a cue</button></td></tr>
    </tbody></table></div>`;
}

const STYLE_KEYS = [['capitalize_first', 'Capitalize first letter'], ['ensure_punctuation', 'End with punctuation'],
  ['strip_trailing_period', 'Drop the final period'], ['inline_formatting', 'Voice punctuation'],
  ['lowercase', 'Lowercase'], ['list_formatting', 'Spoken lists']];

function StylesEditor() {
  const styles = useSetting('postprocess.styles') || {};
  const save = (next) => saveSetting('postprocess.styles', next);
  const edit = (name) => openModal(() => {
    const cur = { ...(app.get().settings.postprocess.styles || {})[name] };
    return html`<div class="modal"><div class="modal-head"><h2>Style “${name}”</h2></div>
      <div class="modal-body">${STYLE_KEYS.map(([k, l]) => html`<label class="row-flex" style="justify-content:space-between">${l}
        <${Toggle} checked=${!!cur[k]} label=${l} onChange=${(v) => { cur[k] = v; save({ ...app.get().settings.postprocess.styles, [name]: { ...cur } }); }} /></label>`)}</div>
      <div class="modal-foot"><button type="button" class="btn danger" onClick=${() => { const n = { ...styles }; delete n[name]; save(n); closeModal(); }}>Delete style</button>
        <button type="button" class="btn pri" onClick=${closeModal}>Done</button></div></div>`;
  });
  return html`<div class="tags">
    ${Object.keys(styles).map((n) => html`<button type="button" class="chip" onClick=${() => edit(n)}>${n}</button>`)}
    <button type="button" class="btn ghost sm" onClick=${async () => {
      const name = await promptModal({ title: 'New style', label: 'Name (used in app rules)', confirm: 'Create' });
      if (name) { await save({ ...styles, [name.trim()]: { capitalize_first: true, ensure_punctuation: true } }); edit(name.trim()); }
    }}><${Icon} name="plus" size="sm" />New style</button></div>`;
}

// ── Extras per section ────────────────────────────────────────────────────

function Autostart() {
  const [state, setState] = useState(null);
  useEffect(() => { call('system_autostart').then(setState).catch(() => {}); }, []);
  if (!state || !state.supported) return null;
  return html`<div class="srow"><div class="slabel"><b>Start on login</b><small>Starts quietly, with just the bubble on the screen edge.</small></div>
    <div class="sctl"><${Toggle} checked=${state.enabled} label="Start on login"
      onChange=${(v) => call('system_set_autostart', v).then((on) => { setState({ ...state, enabled: on }); toast(on ? 'Will start on login' : "Won't start on login"); }).catch((e) => toast(e.message, { kind: 'err' }))} /></div></div>`;
}

function OllamaStatus() {
  const endpoint = useSetting('postprocess.ollama.endpoint');
  const [st, setSt] = useState(null);
  const probe = () => call('ai_probe', endpoint).then(setSt).catch(() => setSt({ reachable: false }));
  useEffect(() => { probe(); }, [endpoint]);
  if (!st) return null;
  return html`<span class="row-flex"><span class=${'chip ' + (st.reachable ? 'ok' : 'rec')}><span class="dot"></span>
    ${st.reachable ? `Ollama reachable · ${st.models.length} models` : 'Ollama not reachable'}</span>
    <button type="button" class="btn sm" onClick=${probe}>Test</button></span>`;
}

function PrivacyExtras() {
  const [count, setCount] = useState(null);
  useEffect(() => { call('history_query', '', '', 'all', 0, 1).then((r) => setCount(r.all)).catch(() => {}); }, []);
  const clear = async () => {
    if (!(await confirmModal({ title: 'Clear your transcript history?', body: `Deletes all ${count} saved dictations from this computer. Stats stay.`, confirm: 'Clear history', danger: true }))) return;
    call('history_clear').then(() => { setCount(0); toast('History cleared'); });
  };
  return html`<div class="srow"><div class="slabel"><b>Transcript history</b><small>The last 2,000 dictations, for search and corrections.</small></div>
    <div class="sctl">${count != null && html`<span class="chip">${count.toLocaleString()} kept</span>`}
      <button type="button" class="btn danger" disabled=${!count} onClick=${clear}>Clear history</button>
      <button type="button" class="btn" onClick=${() => call('diag_open', 'data')}>Open data folder</button></div></div>`;
}

function SystemExtras() {
  const server = useSetting('server.enabled');
  const [status, setStatus] = useState(null);
  const tasks = app.use((s) => s.tasks);
  useEffect(() => { call('server_status').then(setStatus).catch(() => {}); }, [server, Object.keys(tasks).length]);
  const reset = async () => {
    if (!(await confirmModal({ title: 'Reset all settings?', body: 'Every setting goes back to its default. Your favored words are kept. You can undo this right after.', confirm: 'Reset', danger: true }))) return;
    call('settings_reset_all').then(() => call('settings_all')).then((all) => { app.set({ settings: all }); toast('Settings reset', { action: { label: 'Undo', run: undo } }); });
  };
  const uninstall = async () => {
    const plan = await call('system_uninstall_plan');
    let removeModels = false;
    openModal(() => html`<div class="modal"><div class="modal-head"><h2>Uninstall Koli?</h2></div>
      <div class="modal-body"><p style="margin:0">This removes ${plan.data_dir} (${plan.data_size || 'settings and data'})${plan.autostart ? ' and the start-on-login entry' : ''}, then closes the app.</p>
        ${plan.models.length > 0 && html`<label class="row-flex" style="justify-content:space-between">Also delete ${plan.models.length} downloaded models (${plan.models_size})
          <input type="checkbox" class="sw" aria-label="Also delete models" onChange=${(e) => { removeModels = e.currentTarget.checked; }} /></label>`}
        <p class="small muted" style="margin:0">To remove the program itself afterwards: pip uninstall koli, or delete the .exe.</p></div>
      <div class="modal-foot"><button type="button" class="btn" onClick=${closeModal}>Cancel</button>
        <button type="button" class="btn danger" onClick=${() => { closeModal(); call('system_uninstall', removeModels); }}>Uninstall</button></div></div>`);
  };
  const act = (name, okMsg) => () => call(name).then((r) => r && toast(okMsg)).catch((e) => toast(e.message, { kind: 'err' }));
  return html`
    ${status && status.running && html`<div class="srow"><div class="slabel"><b>Local API address</b><small>Point Cursor, Open WebUI or n8n here.</small></div>
      <div class="sctl"><span class="mono small">${status.url}</span><button type="button" class="btn sm" onClick=${() => call('copy_text', status.url).then(() => toast('Copied'))}>Copy</button></div></div>`}
    <div class="subhead"><div class="eyebrow">Backup & move</div></div>
    <div class="srow"><div class="slabel"><b>Settings backup</b><small>Settings, commands, profiles, app rules and transforms.</small></div>
      <div class="sctl"><button type="button" class="btn" onClick=${act('system_export_settings', 'Backup saved')}>Export…</button>
        <button type="button" class="btn" onClick=${act('system_import_settings', 'Backup restored and applied')}>Import…</button></div></div>
    <div class="srow"><div class="slabel"><b>Model for an offline computer</b><small>Copy the downloaded model to a USB drive or network share.</small></div>
      <div class="sctl"><button type="button" class="btn" onClick=${act('system_export_model', 'Exporting the model…')}>Export model…</button>
        <button type="button" class="btn" onClick=${() => call('system_import_model', false).then((r) => r && toast('Importing the model…')).catch((e) => toast(e.message, { kind: 'err' }))}>Import model…</button></div></div>
    <div class="srow"><div class="slabel"><b>Reset all settings</b><small>Your favored words are kept.</small></div>
      <div class="sctl"><button type="button" class="btn danger" onClick=${reset}>Reset…</button></div></div>
    <div class="srow"><div class="slabel"><b>Uninstall</b><small>Removes settings, data and the login entry. Models are asked about separately.</small></div>
      <div class="sctl"><button type="button" class="btn danger" onClick=${uninstall}>Uninstall…</button></div></div>`;
}

// ── Sections ──────────────────────────────────────────────────────────────

function SectionCard({ section, schema, highlight }) {
  const rows = settingsBySection(schema, section.id);
  const groups = [];
  for (const s of rows) {
    const g = s.group || '';
    let bucket = groups.find((x) => x.name === g);
    if (!bucket) { bucket = { name: g, items: [] }; groups.push(bucket); }
    bucket.items.push(s);
  }
  const resetSection = async () => {
    if (!(await confirmModal({ title: `Reset ${section.title}?`, body: 'Every setting in this section goes back to its default. You can undo it.', confirm: 'Reset' }))) return;
    await call('settings_reset_section', section.id);
    app.set({ settings: await call('settings_all') });
    toast(`${section.title} reset`, { action: { label: 'Undo', run: undo } });
  };
  const renderRow = (s) => {
    if (s.control === 'model_table') return s.path === 'whisper.model' ? html`<${ModelsTable} key="models" />` : null;
    if (s.control === 'cue_table') return html`<${CueTable} />`;
    if (s.control === 'styles') return html`<div class="srow"><div class="slabel"><b>${s.label}</b><small>${s.help}</small></div><div class="sctl"><${StylesEditor} /></div></div>`;
    if (s.path === 'postprocess.ollama.enabled') return html`<${SettingRow} s=${s} highlight=${highlight === s.path} extra=${html`<${OllamaStatus} />`} />`;
    if (s.path === 'whisper.device') return html`<${SettingRow} s=${s} highlight=${highlight === s.path} /><${GpuRow} />`;
    return html`<${SettingRow} s=${s} highlight=${highlight === s.path} />`;
  };
  return html`<section class="card" id=${'sec-' + section.id} aria-labelledby=${'h-' + section.id} style="scroll-margin-top:16px">
    <div class="card-head"><div><h2 id=${'h-' + section.id}>${section.title}</h2><p>${section.desc}</p></div>
      <button type="button" class="btn ghost sm" onClick=${resetSection}>Reset section</button></div>
    ${section.id === 'general' && html`<${Autostart} />`}
    ${section.id === 'audio' && html`<div class="srow"><div class="slabel"><b>Check your microphone</b><small>Shows the level while you talk.</small></div><div class="sctl"><${MicTest} /></div></div>`}
    ${groups.map((g) => html`${g.name && html`<div class="subhead"><div class="eyebrow">${g.name}</div></div>`}${g.items.map(renderRow)}`)}
    ${section.id === 'hotkeys' && html`<div class="srow"><div class="slabel"><b>Transform shortcuts</b><small>Polish, Prompt engineer and the rest have their own keys.</small></div>
      <div class="sctl"><a class="btn" href="#/commands">Edit in Commands & AI</a></div></div>`}
    ${section.id === 'privacy' && html`<${PrivacyExtras} />`}
    ${section.id === 'system' && html`<${SystemExtras} />`}
  </section>`;
}

export function SettingsScreen({ route }) {
  const schema = app.use((s) => s.schema);
  const canUndo = app.use((s) => s.undo);
  const [, sectionId, highlight] = (route || '').split('/');
  useEffect(() => {
    const target = highlight ? document.getElementById('s-' + highlight) : sectionId ? document.getElementById('sec-' + sectionId) : null;
    if (target) target.scrollIntoView({ block: highlight ? 'center' : 'start' });
  }, [sectionId, highlight]);
  if (!schema) return null;
  return html`<div class="page">
    <div class="page-head">
      <div><h1 class="page-title">Settings</h1><p class="page-sub">Every change saves the moment you make it. There is no Save button.</p></div>
      ${canUndo && html`<button type="button" class="btn" onClick=${undo}>Undo last change</button>`}
    </div>
    <div class="notice warn"><${Icon} name="refresh" /><span>Settings marked <b>Applies in background</b> need part of the app to reload. That happens behind the scenes: dictation keeps working on the old setting and switches over when ready.</span></div>
    ${schema.sections.map((sec) => html`<${SectionCard} key=${sec.id} section=${sec} schema=${schema} highlight=${highlight} />`)}
  </div>`;
}

export { useTasks };
