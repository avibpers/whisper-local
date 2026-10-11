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

export function MicSelect({ compact = false }) {
  const audio = useAudio();
  const value = useSetting('audio.input_device');
  if (!audio) return html`<span class="muted small">Loading…</span>`;
  const options = [['default', "Computer's default"], ...audio.devices.map((d) => [d.id, d.name])];
  return html`<${Select} label="Microphone" value=${value} options=${options}
    style=${compact ? 'height:32px;max-width:100%' : 'flex:1 1 240px;max-width:420px'}
    onChange=${(v) => saveSetting('audio.input_device', v)} />`;
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

function OllamaModel({ s, value }) {
  const [models, setModels] = useState([]);
  const endpoint = useSetting('postprocess.ollama.endpoint');
  useEffect(() => { call('ai_probe', endpoint).then((r) => setModels(r.models || [])).catch(() => {}); }, [endpoint]);
  if (!models.length) return html`<${TextField} label=${s.label} value=${value} onCommit=${(v) => saveSetting(s.path, v)} />`;
  const opts = models.map((m) => [m, m]);
  if (!models.includes(value)) opts.unshift([value, `${value} (not installed)`]);
  return html`<${Select} label=${s.label} value=${value} options=${opts} onChange=${(v) => saveSetting(s.path, v)} />`;
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
    case 'text':
      if (s.source === 'ollama_models') return html`<${OllamaModel} s=${s} value=${value} />`;
      return html`<${TextField} value=${value} label=${s.label} mono=${s.path.includes('endpoint') || s.path.includes('host')} onCommit=${save} />`;
    case 'textarea': return html`<${TextArea} value=${value} label=${s.label} mono=${s.path.includes('ollama')} onCommit=${save} />`;
    case 'tags': return html`<${TagInput} values=${value || []} label=${s.label} onChange=${save}
      locked=${s.path === 'postprocess.voice_editing_phrases' ? ['scratch that', 'delete that', 'strike that'] : []} />`;
    case 'choice_tags': return html`<${ChoiceTags} values=${value || []} options=${s.choices} label=${s.label} onChange=${save} />`;
    case 'hotkey': return html`<${HotkeyCapture} value=${value} label=${s.label} nullable=${s.nullable} onCommit=${save} />`;
    case 'microphone': return html`<${MicSelect} />`;
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
const EXTRA_FOR = { 'audio.noise_suppression.enabled': 'noise' };

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
