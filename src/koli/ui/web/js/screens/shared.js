// screens/shared.js
// Pieces several screens use: the audio device list, the microphone picker
// with a live level meter, and the generic setting row that renders any
// schema entry with the right control (toggle, choice, number, text, tags,
// shortcut) and marks settings that apply in the background.

import { html, useEffect, useState } from '../../vendor/preact-htm.module.js';
import { call } from '../api.js';
import { app, getPath, live, saveSetting, toast, useSetting } from '../store.js';
import { ChoiceTags, HotkeyCapture, Icon, NumberField, RangeField, Segmented, Select, TagInput, TextArea, TextField, Toggle } from '../ui.js';

export function useAudio() {
  const version = app.use((s) => s.devicesVersion || 0);
  const [audio, setAudio] = useState(null);
  useEffect(() => { call('audio_devices').then(setAudio).catch(() => {}); }, [version]);
  return audio;
}

// The microphone picker. `path` is the setting it edits; `same` adds "Same as
// dictation" (saved as '') on top, for the meeting microphone.
export function MicSelect({ compact = false, path = 'audio.input_device', same = false }) {
  const audio = useAudio();
  const value = useSetting(path);
  if (!audio) return html`<span class="muted small">Loading…</span>`;
  const options = [...(same ? [['', 'Same as dictation']] : []), ['default', "Computer's default"],
    ...audio.devices.map((d) => [d.id, d.name])];
  return html`<${Select} label="Microphone" value=${value == null ? '' : value} options=${options}
    style=${compact ? 'height:32px;max-width:100%' : 'flex:1 1 240px;max-width:420px'}
    onChange=${(v) => saveSetting(path, v)} />`;
}

// Level meter that listens while it is on screen (Test my microphone).
export function MicTest({ big = false }) {
  const [on, setOn] = useState(false);
  const level = live.use((s) => s.level);
  const levels = live.use((s) => s.levels);
  useEffect(() => () => { if (on) call('mic_test', false); }, [on]);
  const toggle = () => call('mic_test', !on).then(setOn);
  const heard = on && level > 0.01;
  return html`<div class="stack" style="gap:8px">
    <div class="row-flex">
      <button type="button" class="btn" onClick=${toggle}><${Icon} name=${on ? 'pause' : 'mic'} />${on ? 'Stop the test' : 'Test my microphone'}</button>
      ${on && html`<span class=${'chip ' + (heard ? 'ok' : 'warn')}><span class="dot"></span>${heard ? 'Hearing you' : 'Say something…'}</span>`}
    </div>
    ${on && (big
      ? html`<div style="display:flex;align-items:flex-end;gap:3px;height:36px" aria-label="Sound level">
          ${levels.slice(-24).map((l) => html`<span style=${`flex:1;height:100%;border-radius:2px;transform-origin:50% 100%;transform:scaleY(${(Math.max(10, Math.min(100, l * 600)) / 100).toFixed(3)});background:${l > 0.01 ? 'var(--ok)' : 'var(--line2)'}`}></span>`)}</div>`
      : html`<div class="bar-meter"><span style=${`transform:scaleX(${Math.min(1, level * 4)});background:var(--ok)`}></span></div>`)}
  </div>`;
}

export function BgBadge() {
  return html`<span class="bg-badge" title="Dictation keeps working on the old setting until the new one is ready">
    <${Icon} name="refresh" size="sm" />Applies in background</span>`;
}

function AudioHostSelect({ s, value }) {
  const audio = useAudio();
  const hosts = audio ? audio.hosts : [];
  const options = [[null, 'Automatic'], ...hosts.map((h) => [h, h.replace('Windows ', '')])];
  return html`<${Select} label=${s.label} value=${value} options=${options} onChange=${(v) => saveSetting(s.path, v)} />`;
}

function DynamicSelect({ s, value }) {
  const [options, setOptions] = useState([[value, value]]);
  useEffect(() => {
    if (s.source === 'streaming_models') {
      call('models_list').then((m) => setOptions(m.streaming.map((k) => [k, k]))).catch(() => {});
    }
    if (s.source === 'whisper_models') {
      call('models_list').then((m) => setOptions(m.models.filter((x) => x.enabled).map((x) => [x.key, x.label]))).catch(() => {});
    }
  }, [s.source]);
  return html`<${Select} label=${s.label} value=${value} options=${options} onChange=${(v) => saveSetting(s.path, v)} />`;
}

// ── AI model pickers ──────────────────────────────────────────────────────
// A model is "provider/model". The provider list never changes while the app
// runs; each provider's models are asked once per 30 seconds at most.

let aiCatalog = null;
const aiModelCache = {};

export function useAiCatalog() {
  const [list, setList] = useState(aiCatalog || []);
  useEffect(() => {
    if (!aiCatalog) call('ai_catalog').then((c) => { aiCatalog = c; setList(c); }).catch(() => {});
  }, []);
  return list;
}

export function aiModels(provider, fresh = false) {
  const hit = aiModelCache[provider];
  if (!fresh && hit && Date.now() - hit.at < 30000) return hit.promise;
  const promise = call('ai_models', provider);
  aiModelCache[provider] = { at: Date.now(), promise };
  promise.catch(() => { delete aiModelCache[provider]; });
  return promise;
}

export const splitModel = (spec) => {
  const i = String(spec || '').indexOf('/');
  return i > 0 ? [spec.slice(0, i), spec.slice(i + 1)] : ['', ''];
};

// "Local" or "Cloud" next to a model, so it is always clear where text goes.
export function KindBadge({ kind }) {
  if (!kind) return null;
  return html`<span class=${'chip sm ' + (kind === 'local' ? 'ok' : 'warn')}
    title=${kind === 'local' ? 'Runs on this computer' : 'Sends the text to this provider'}>${kind === 'local' ? 'Local' : 'Cloud'}</span>`;
}

// Provider, then one of its models (or a typed name when the list can't be
// had). `empty` is the label for no model ('' is saved).
export function AiModelPicker({ value, onChange, empty = 'None', label = 'Model' }) {
  const catalog = useAiCatalog();
  const [provider, model] = splitModel(value);
  const [chosen, setChosen] = useState(provider);
  const [models, setModels] = useState(null);
  const [error, setError] = useState('');
  useEffect(() => setChosen(provider), [provider]);
  useEffect(() => {
    setModels(null); setError('');
    if (chosen) aiModels(chosen).then(setModels).catch((e) => { setModels([]); setError(e.message); });
  }, [chosen]);
  const kind = (catalog.find((p) => p.id === chosen) || {}).kind;
  const pickProvider = (p) => { setChosen(p); if (!p) onChange(''); };
  const options = (models || []).map((m) => [m.id, `${m.label}${m.loaded ? ' · loaded' : ''}`]);
  if (model && chosen === provider && models && !models.some((m) => m.id === model)) options.unshift([model, `${model} (not listed)`]);
  return html`<span class="row-flex" style="flex-wrap:wrap;gap:6px;justify-content:flex-end">
    <${Select} label=${`${label}: provider`} value=${chosen} style="max-width:200px"
      options=${[['', empty], ...catalog.map((p) => [p.id, p.label])]} onChange=${pickProvider} />
    ${chosen && (models === null ? html`<span class="small muted">Loading…</span>`
      // Keyed by the value: the list loses its "Choose a model" row once one is
      // picked, and a reused <select> would keep the old position.
      : options.length ? html`<${Select} key=${`${chosen}|${value}`} label=${`${label}: model`} value=${chosen === provider ? model : ''} style="max-width:260px"
          options=${chosen === provider ? options : [['', 'Choose a model'], ...options]} onChange=${(m) => m && onChange(`${chosen}/${m}`)} />`
      : html`<${TextField} label=${`${label}: model name`} mono width="200px" placeholder="model name"
          value=${chosen === provider ? model : ''} onCommit=${(m) => m && onChange(`${chosen}/${m.trim()}`)} />`)}
    <${KindBadge} kind=${kind} />
    ${error && html`<span class="small muted" style="flex-basis:100%;text-align:end">${error}</span>`}
  </span>`;
}

// The fallback list: each model with Remove, then a picker to add one.
function AiModelList({ s, value }) {
  const items = value || [];
  const save = (next) => saveSetting(s.path, next).catch(() => {});
  return html`<div class="stack" style="gap:6px;align-items:flex-end">
    ${items.map((spec, i) => html`<span class="row-flex">
      <span class="mono small">${i + 1}. ${spec}</span>
      <button type="button" class="btn ghost icon" aria-label=${`Remove ${spec}`} onClick=${() => save(items.filter((_, n) => n !== i))}><${Icon} name="x" /></button></span>`)}
    <${AiModelPicker} key=${items.length} value="" empty="Add a fallback…" label="Fallback" onChange=${(v) => v && save([...items, v])} />
  </div>`;
}

function SoundControl({ s, value }) {
  const pick = () => call('pick_file', `Choose the ${s.label.toLowerCase()}`, ['Sound files (*.wav;*.mp3)'])
    .then((p) => p && saveSetting(s.path, p)).catch((e) => toast(e.message, { kind: 'err' }));
  return html`<span class="row-flex"><span class="mono small muted" title=${value}>${String(value || '').split(/[\\/]/).pop()}</span>
    <button type="button" class="btn ghost sm" onClick=${() => call('sound_preview', s.path).catch((e) => toast(e.message, { kind: 'err' }))}><${Icon} name="play" size="sm" />Play</button>
    <button type="button" class="btn ghost sm" onClick=${pick}>Choose…</button></span>`;
}

// The control for one schema entry.
export function SettingControl({ s }) {
  const value = useSetting(s.path);
  const save = (v) => saveSetting(s.path, v).catch(() => {});
  switch (s.control) {
    case 'toggle': return html`<${Toggle} checked=${value} label=${s.label} onChange=${save} />`;
    case 'segmented': return html`<${Segmented} value=${value} options=${s.choices} label=${s.label} onChange=${save} />`;
    case 'select':
      if (s.source) return html`<${DynamicSelect} s=${s} value=${value} />`;
      return html`<${Select} value=${value} options=${s.choices} label=${s.label} onChange=${save} />`;
    case 'number': return html`<${NumberField} value=${value} label=${s.label} min=${s.min} max=${s.max} step=${s.step} unit=${s.unit} onCommit=${save} />`;
    case 'range': return html`<${RangeField} value=${value} label=${s.label} min=${s.min} max=${s.max} step=${s.step} unit=${s.unit} onCommit=${save} />`;
    case 'text': return html`<${TextField} value=${value} label=${s.label} mono=${s.path.includes('address') || s.path.includes('host')} onCommit=${save} />`;
    case 'textarea': return html`<${TextArea} value=${value} label=${s.label} mono=${s.path.startsWith('ai.')} onCommit=${save} />`;
    case 'ai_model': return html`<${AiModelPicker} value=${value} label=${s.label} empty=${s.path === 'ai.model' ? 'None' : 'Same as default'} onChange=${save} />`;
    case 'ai_models': return html`<${AiModelList} s=${s} value=${value} />`;
    case 'tags': return html`<${TagInput} values=${value || []} label=${s.label} onChange=${save}
      locked=${s.path === 'postprocess.voice_editing_phrases' ? ['scratch that', 'delete that', 'strike that'] : []} />`;
    case 'choice_tags': return html`<${ChoiceTags} values=${value || []} options=${s.choices} label=${s.label} onChange=${save} />`;
    case 'hotkey': return html`<${HotkeyCapture} value=${value} label=${s.label} nullable=${s.nullable} onCommit=${save} />`;
    case 'microphone': return html`<${MicSelect} path=${s.path} same=${s.path !== 'audio.input_device'} />`;
    case 'audiohost': return html`<${AudioHostSelect} s=${s} value=${value} />`;
    case 'sound': return html`<${SoundControl} s=${s} value=${value} />`;
    case 'readonly': return html`<span class="muted small">${typeof value === 'object' ? `${Object.keys(value || {}).length} defined in user_settings.yaml` : String(value)}</span>`;
    default: return html`<span class="muted small">Edited on its own screen</span>`;
  }
}

const WIDE = new Set(['textarea', 'tags', 'choice_tags']);

// Progress of one background task: a bar (moving when the total isn't known
// yet), what it is doing ("Downloading 120 of 466 MB") and a Cancel button.
export function TaskProgress({ task, cancel = true }) {
  if (!task) return null;
  const pct = task.progress != null ? Math.round(task.progress * 100) : null;
  return html`<div class="task-progress" role="status" aria-label=${task.label}>
    <div class="row-flex" style="justify-content:space-between;gap:8px;font-size:12.5px">
      <span class="muted" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${task.detail || task.label}</span>
      <span class="row-flex" style="gap:6px">${pct != null && html`<b class="mono">${pct}%</b>`}
        ${cancel && html`<button type="button" class="btn ghost sm" onClick=${() => call('task_cancel', task.id)}>Cancel</button>`}</span>
    </div>
    <div class=${'bar-meter' + (pct == null ? ' busy' : '')}><span style=${pct != null ? `transform:scaleX(${task.progress});background:var(--acc)` : 'background:var(--acc)'}></span></div>
  </div>`;
}

// The running tasks of one kind (or all).
export function useRunningTasks(kind) {
  const tasks = app.use((s) => s.tasks);
  return Object.values(tasks).filter((t) => t.status === 'running' && (!kind || t.kind === kind));
}

// An optional package a setting needs (noise suppression → noisereduce):
// says so while it is missing and installs it in the background.
const EXTRA_FOR = { 'audio.noise_suppression.enabled': 'noise', 'meeting.enabled': 'loopback' };

export function ExtraInstall({ name }) {
  const [installed, setInstalled] = useState(true);
  const tasks = app.use((s) => s.tasks);
  const running = Object.values(tasks).find((t) => t.kind === 'install' && t.status === 'running');
  useEffect(() => { call('extra_status', name).then((r) => setInstalled(r.installed)).catch(() => {}); }, [name, running && running.id]);
  if (installed) return null;
  return html`<div class="notice warn" style="margin-top:8px;align-items:center">
    <span class="grow">${running ? (running.detail || 'Installing…') : "This needs an extra package that isn't installed yet, so it has no effect for now."}</span>
    ${!running && html`<button type="button" class="btn sm" onClick=${() => call('extra_install', name).catch((e) => toast(e.message, { kind: 'err' }))}>Install</button>`}
  </div>`;
}

export function SettingRow({ s, highlight = false, extra = null }) {
  const needs = EXTRA_FOR[s.path];
  const on = useSetting(s.path);
  if (needs && on && !extra) extra = html`<${ExtraInstall} name=${needs} />`;
  return html`<div class=${'srow' + (WIDE.has(s.control) ? ' top' : '') + (highlight ? ' hl' : '')} id=${'s-' + s.path}>
    <div class="slabel"><b>${s.label}${s.apply === 'background' && html`<${BgBadge} />`}</b>
      ${s.help && html`<small>${s.help}</small>`}
      ${s.apply === 'next_launch' && html`<small>Applies next time you open the app.</small>`}</div>
    <div class=${'sctl' + (WIDE.has(s.control) ? ' wide' : '')}><${SettingControl} s=${s} />${extra}</div>
  </div>`;
}

export function settingsBySection(schema, section) {
  // Hidden settings are edited by their own controls (e.g. the card picker).
  return schema ? schema.settings.filter((s) => s.section === section && s.mode !== 'hidden') : [];
}

export const pathValue = (path) => getPath(app.get().settings, path);
