// screens/live.js
// Live (Advanced home): what the engine hears and does right now — recording
// timer and controls, waveform, input level, voice-detection probability with
// its thresholds, the silence countdown, the context (microphone, app, rule,
// language, model), the last dictation step by step, today's numbers and the
// streaming console log.

import { html, useEffect, useMemo, useRef, useState } from '../../vendor/preact-htm.module.js';
import { call } from '../api.js';
import { app, getPath, live, promptModal, saveSetting, setting, textDir, toast, useSetting } from '../store.js';
import { Icon, Keys, Toggle } from '../ui.js';
import { useAudio, MicSelect } from './shared.js';

const LANG_NAMES = { auto: 'Auto-detect', en: 'English', he: 'Hebrew' };

function useRecordingClock(state) {
  const [start, setStart] = useState(null);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (state === 'recording') setStart((s) => s || Date.now());
    else setStart(null);
  }, [state]);
  useEffect(() => {
    if (!start) return undefined;
    const t = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(t);
  }, [start]);
  return start ? (now - start) / 1000 : 0;
}

function fmtClock(sec) {
  const m = Math.floor(sec / 60);
  const s = sec - m * 60;
  return `${m}:${s.toFixed(1).padStart(4, '0')}`;
}

function Waveform({ active }) {
  const levels = live.use((s) => s.levels);
  return html`<div class=${'wave' + (active ? '' : ' idle')} aria-label="Live waveform" role="img">
    ${levels.map((l) => html`<span style=${`transform:scaleY(${(Math.max(3, Math.min(80, 3 + l * 420)) / 80).toFixed(3)})`}></span>`)}
  </div>`;
}

function Meters({ recording }) {
  const level = live.use((s) => s.level);
  const vad = live.use((s) => s.vad);
  const db = level > 0 ? Math.round(20 * Math.log10(level)) : null;
  const timeout = vad ? vad.timeout_s : setting('vad.vad_silence_timeout_seconds');
  return html`<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px">
    <div><div class="eyebrow">Input level</div>
      <div class="bar-meter" style="margin-top:8px"><span style=${`transform:scaleX(${Math.min(1, level * 4)});background:var(--ok)`}></span></div>
      <div class="small muted" style="margin-top:6px">${recording && db != null ? `${db} dB` : 'Not listening'}${setting('audio.whisper_mode.enabled') ? ' · whisper mode on' : ''}</div></div>
    <div><div class="eyebrow">Speech probability</div>
      <div class="bar-meter" style="margin-top:8px;overflow:visible">
        <span style=${`transform:scaleX(${vad ? vad.probability : 0});background:var(--acc)`}></span>
        ${vad && html`<i title=${`Start ${vad.onset}`} style=${`position:absolute;left:${vad.onset * 100}%;top:-4px;width:2px;height:16px;background:var(--ink)`}></i>
          <i title=${`End ${vad.offset}`} style=${`position:absolute;left:${vad.offset * 100}%;top:-4px;width:2px;height:16px;background:var(--mute)`}></i>`}
      </div>
      <div class="small muted" style="margin-top:6px">${vad ? `${vad.probability.toFixed(2)} · ${vad.speaking ? 'speaking' : 'quiet'} (start ${vad.onset} / end ${vad.offset})` : recording ? 'Voice detection is off' : '—'}</div></div>
    <div><div class="eyebrow">Silence auto-stop</div>
      <div class="bar-meter" style="margin-top:8px"><span style=${`transform:scaleX(${vad && timeout ? Math.min(1, vad.silence_s / timeout) : 0});background:var(--warn)`}></span></div>
      <div class="small muted" style="margin-top:6px">${setting('vad.vad_realtime_enabled') ? `${vad ? vad.silence_s.toFixed(1) : '0.0'} s of ${timeout} s silence` : 'Off'}</div></div>
  </div>`;
}

// Its own component so the 10-per-second tick redraws only the digits.
function Clock({ state }) {
  const elapsed = useRecordingClock(state);
  return html`<span class="mono" style="font-size:28px;font-weight:600">${fmtClock(elapsed)}</span>`;
}

function NowCard() {
  const st = app.use((s) => s.state);
  const recording = st.state === 'recording';
  const max = useSetting('audio.max_duration');
  const audio = useAudio();
  const streaming = app.use((s) => s.streaming);
  const cur = audio && audio.current;
  const label = st.paused ? 'Hotkeys paused' : recording ? (st.mode === 'command' ? 'Listening for a command' : st.mode === 'rephrase' ? 'Listening for an instruction' : st.hands_free ? 'Recording hands-free · tap the keys to stop' : 'Recording')
    : st.state === 'processing' ? 'Transcribing…' : st.state === 'model_loading' ? 'Loading the model…' : 'Ready';
  return html`<section class="card pad stack" style="grid-column:span 2;min-width:0" aria-label="Current recording">
    <div class="row-flex" style="justify-content:space-between">
      <div class="row-flex" style="align-items:baseline;gap:12px">
        <${Clock} state=${st.state} />
        <span class="muted">${label}${recording && max ? ` · max ${Math.round(max / 60)} min` : ''}</span>
      </div>
      <div class="row-flex">
        ${recording ? html`
          <button type="button" class="btn" onClick=${() => call('dictation_cancel')}><${Keys} hotkey=${setting('hotkey.cancel_combination')} />Cancel</button>
          <button type="button" class="btn pri" onClick=${() => call('dictation_stop', false)}>Stop & paste</button>`
        : html`<button type="button" class="btn" disabled=${st.state !== 'idle'} onClick=${() => call('dictation_start')}
            title="Starts a dictation into whatever app was in front before this window"><${Icon} name="mic" />Start</button>`}
      </div>
    </div>
    <${Waveform} active=${recording} />
    ${streaming && recording && html`<div class="muted" dir="auto">…${streaming.slice(-120)}</div>`}
    <${Meters} recording=${recording} />
    <div class="row-flex small">
      ${cur && html`<span class="chip">${cur.name || 'Default microphone'}${cur.host ? ` · ${cur.host.replace('Windows ', '')}` : ''}</span>
        <span class="chip">${cur.native_rate !== cur.recording_rate ? `${Math.round(cur.native_rate / 1000)} kHz → ` : ''}${Math.round(cur.recording_rate / 1000)} kHz ${cur.channels === 1 ? 'mono' : 'stereo'}</span>
        <span class="chip">${cur.preroll_s * 1000} ms pre-roll</span>
        <span class="chip">Noise suppression ${cur.noise_suppression ? 'on' : 'off'}</span>`}
    </div>
  </section>`;
}

function ContextCard() {
  const fg = app.use((s) => s.foreground);
  const profile = app.use((s) => s.profile);
  const settings = app.use((s) => s.settings);
  const pipeline = app.use((s) => s.pipeline);
  const info = (pipeline.find((p) => p.stage === 'transcribed') || {}).info;
  const g = (p) => getPath(settings, p);
  const lang = g('whisper.language') || 'auto';
  const langs = [['auto', 'Auto'], ['en', 'English'], ['he', 'עברית']];
  if (!langs.some(([v]) => v === lang)) langs.push([lang, lang]);
  return html`<section class="card pad stack" style="min-width:0" aria-label="Context">
    <div class="row-flex" style="justify-content:space-between"><span class="card-title">Context</span>
      ${profile && html`<span class="chip acc">${profile} profile</span>`}</div>
    <dl style="margin:0;display:grid;grid-template-columns:auto minmax(0,1fr);gap:10px 16px;font-size:13px;align-items:center">
      <dt class="muted">Microphone</dt><dd style="margin:0"><${MicSelect} compact=${true} /></dd>
      <dt class="muted">App</dt><dd style="margin:0" dir="auto">${fg.exe ? `${fg.exe}${fg.title ? ` — “${fg.title.slice(0, 40)}”` : ''}` : '—'}</dd>
      <dt class="muted">App rule</dt><dd style="margin:0">${fg.rule || 'None'}</dd>
      <dt class="muted">Language</dt><dd style="margin:0">${LANG_NAMES[lang] || lang}${info && info.language ? ` · last heard ${info.language}${info.language_probability ? ` (${info.language_probability})` : ''}` : ''}</dd>
      <dt class="muted">Model</dt><dd style="margin:0">${g('whisper.model')}, ${g('whisper.device') === 'cuda' ? 'GPU' : 'CPU'} ${g('whisper.compute_type')}</dd>
      <dt class="muted">Delivery</dt><dd style="margin:0">${g('clipboard.auto_paste') ? (g('clipboard.delivery_method') === 'type' ? 'Typed in' : 'Pasted') : 'Copied only'}${g('clipboard.paste_preserve_clipboard') ? ', clipboard restored' : ''}</dd>
    </dl>
    <div class="stack" style="border-top:1px solid var(--line);padding-top:14px;gap:10px">
      <div class="seg" role="radiogroup" aria-label="Language">
        ${langs.map(([v, t]) => html`<button type="button" role="radio" aria-checked=${lang === v} class=${lang === v ? 'on' : ''}
          onClick=${() => saveSetting('whisper.language', v)}>${t}</button>`)}
      </div>
      ${[['clipboard.auto_paste', 'Auto-paste at cursor'], ['audio.continuous_mode', 'Continuous dictation']].map(([p, l]) => html`
        <label class="row-flex" style="justify-content:space-between"><span>${l}</span>
          <${Toggle} checked=${g(p)} label=${l} onChange=${(v) => saveSetting(p, v)} /></label>`)}
      <label class="row-flex" style="justify-content:space-between"><span>Translate to English</span>
        <${Toggle} checked=${g('whisper.task') === 'translate'} label="Translate to English"
          onChange=${(v) => saveSetting('whisper.task', v ? 'translate' : 'transcribe')} /></label>
    </div>
  </section>`;
}

const STAGES = [
  ['captured', '1 · Captured'], ['transcribed', '2 · Whisper'], ['postprocessed', '3 · Clean-up'], ['delivered', '4 · Delivered'],
];
const OUTCOME = { pasted: 'Pasted', copied: 'Copied (copy-only)', suppressed: 'Not delivered (app rule)', fallback: 'Shown here (no text field)',
  failed: 'Delivery failed', empty: 'Nothing left to type', streamed: 'Typed live' };
const STEP_NAMES = { voice_editing: 'scratch that', backtrack: 'backtrack', repeated_words: 'stutters', voice_punctuation: 'voice punctuation',
  smart_formatting: 'smart formatting', corrections: 'corrections', replacements: 'replacements', filler_words: 'fillers', list: 'list',
  lowercase: 'lowercase', snippets: 'snippet', final_period: 'final period', capitalize: 'capital', punctuation: 'punctuation', ai_polish: 'AI polish' };

export async function fixAWord(heard = '') {
  const frm = await promptModal({ title: 'Fix a word everywhere', label: 'What Whisper wrote', value: heard.split(' ').slice(0, 3).join(' '), confirm: 'Next' });
  if (!frm) return;
  const to = await promptModal({ title: 'Fix a word everywhere', label: `Always write “${frm}” as`, confirm: 'Save' });
  if (to == null) return;
  try { await call('history_fix', frm, to); toast(`From now on “${frm}” becomes “${to}”`); } catch (e) { toast(e.message, { kind: 'err' }); }
}

function LastDictation() {
  const pipeline = app.use((s) => s.pipeline);
  const by = Object.fromEntries(pipeline.map((p) => [p.stage, p]));
  if (!pipeline.length) {
    return html`<section class="card pad" style="grid-column:span 2"><span class="card-title">Last dictation</span>
      <p class="muted" style="margin:8px 0 0">Nothing yet. Hold your record shortcut and talk.</p></section>`;
  }
  const cap = by.captured || {};
  const tr = by.transcribed || {};
  const pp = by.postprocessed || {};
  const dl = by.delivered || {};
  const fail = by.error || by.empty || by.no_audio;
  const heard = tr.raw || pp.raw || '';
  const typed = dl.text || pp.text || '';
  const detail = {
    captured: cap.duration != null ? `${cap.duration} s${cap.mode && cap.mode !== 'dictate' ? ` · ${cap.mode}` : ''}` : '',
    transcribed: tr.info ? `${tr.info.language || '?'}${tr.info.language_probability ? ` ${tr.info.language_probability}` : ''} · ${tr.info.seconds} s · beam ${tr.info.beam_size}` : by.transcribing ? 'Working…' : '',
    postprocessed: pp.steps ? (pp.steps.length ? pp.steps.map((s) => STEP_NAMES[s] || s.replace('style:', 'style ')).join(', ') : 'No changes') : '',
    delivered: dl.outcome ? `${OUTCOME[dl.outcome] || dl.outcome}${dl.app ? ` · ${dl.app}` : ''}${dl.auto_enter ? ' + Enter' : ''}` : '',
  };
  return html`<section class="card pad stack" style="grid-column:span 2;min-width:0" aria-label="Last dictation">
    <div class="row-flex" style="justify-content:space-between">
      <span class="card-title">Last dictation <span class="muted" style="font-weight:400">· ${new Date((cap.t || Date.now() / 1000) * 1000).toLocaleTimeString()}</span></span>
      <div class="row-flex">
        <button type="button" class="btn" disabled=${!typed} onClick=${() => call('copy_text', typed).then(() => toast('Copied'))}><${Icon} name="copy" />Copy</button>
        <button type="button" class="btn" disabled=${!typed} onClick=${() => call('paste_last').catch((e) => toast(e.message, { kind: 'err' }))}>Paste again</button>
        <button type="button" class="btn" disabled=${!heard} onClick=${() => fixAWord(heard)}>Fix a word everywhere…</button>
      </div>
    </div>
    ${fail ? html`<div class="notice err"><${Icon} name="alert" /><span>${fail.stage === 'no_audio' ? 'No audio was captured — is the microphone muted?'
      : fail.stage === 'empty' ? 'No speech was detected in that recording.' : `Something went wrong: ${fail.error}`}</span></div>` : html`
    <ol class="trace">${STAGES.map(([id, title]) => html`<li class=${by[id] ? (id === 'delivered' && dl.outcome === 'failed' ? 'fail' : id === 'delivered' ? 'done' : '') : ''}>
      <div class="eyebrow">${title}</div><div class="small" style="margin-top:4px">${detail[id] || '…'}</div></li>`)}</ol>`}
    ${(heard || typed) && html`<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px">
      <div style="padding:12px 14px;border:1px dashed var(--line2);border-radius:10px"><div class="eyebrow">Heard</div>
        <p dir=${textDir(heard)} style="margin:6px 0 0;color:var(--ink2);white-space:pre-wrap">${heard}</p></div>
      <div style="padding:12px 14px;border:1px solid var(--line);border-radius:10px"><div class="eyebrow">Typed</div>
        <p dir=${textDir(typed)} style="margin:6px 0 0;font-size:15px;white-space:pre-wrap">${typed}</p></div>
    </div>`}
  </section>`;
}

function TodayCard() {
  const [data, setData] = useState(null);
  const pipeline = app.use((s) => s.pipeline);
  const delivered = pipeline.some((p) => p.stage === 'delivered');
  useEffect(() => { call('insights', 7).then(setData).catch(() => {}); }, [delivered]);
  const t = data && data.today;
  return html`<section class="card pad stack" style="min-width:0" aria-label="Today">
    <span class="card-title">Today</span>
    <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px">
      ${[[t ? t.count : '—', 'dictations'], [t ? t.words.toLocaleString() : '—', 'words'],
        [t ? `${Math.round(t.minutes_saved)} min` : '—', 'saved vs typing'], [t && t.wpm ? `${t.wpm} wpm` : '—', 'speaking pace']]
        .map(([v, l]) => html`<div><div class="stat">${v}</div><div class="small muted">${l}</div></div>`)}
    </div>
    <a href="#/history" class="small" style="font-weight:500">Open history & insights</a>
  </section>`;
}

function Console() {
  const logs = app.use((s) => s.logs);
  const [level, setLevel] = useState('all');
  const [filter, setFilter] = useState('');
  const [paused, setPaused] = useState(false);
  const [frozen, setFrozen] = useState(null);
  const box = useRef(null);
  const stick = useRef(true);
  const source = paused && frozen ? frozen : logs;
  const shown = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return source.filter((r) => (level === 'all' || (level === 'warn' ? (r.cls === 'warn' || r.cls === 'error') : r.cls === level))
      && (!f || `${r.mod} ${r.msg}`.toLowerCase().includes(f))).slice(-800);
  }, [source, source.length, level, filter]);
  useEffect(() => { if (stick.current && box.current) box.current.scrollTop = box.current.scrollHeight; });
  return html`<section class="card" style="min-width:0;overflow:hidden" aria-label="Console">
    <div class="row-flex" style="justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--line)">
      <div class="row-flex"><span class="card-title">Console</span>
        <span class=${'chip ' + (paused ? 'warn' : 'ok')}><span class="dot"></span>${paused ? 'Paused' : 'Streaming'}</span></div>
      <div class="row-flex">
        <div class="seg" role="radiogroup" aria-label="Log level">
          ${[['all', 'All'], ['info', 'Info'], ['warn', 'Warnings'], ['debug', 'Debug']].map(([v, t]) => html`
            <button type="button" role="radio" aria-checked=${level === v} class=${level === v ? 'on' : ''} onClick=${() => setLevel(v)}>${t}</button>`)}
        </div>
        <input class="inp" aria-label="Filter the log" placeholder="Filter" style="width:150px" value=${filter} onInput=${(e) => setFilter(e.currentTarget.value)} />
        <button type="button" class="btn" onClick=${() => { setFrozen(paused ? null : logs.slice()); setPaused(!paused); }}>
          <${Icon} name=${paused ? 'play' : 'pause'} />${paused ? 'Resume' : 'Pause'}</button>
        <button type="button" class="btn ghost" onClick=${() => call('log_clear').then(() => app.set({ logs: [] }))}>Clear</button>
        <button type="button" class="btn ghost" onClick=${() => call('diag_open', 'log').catch((e) => toast(e.message, { kind: 'err' }))}>Open app.log</button>
      </div>
    </div>
    <div class="log" role="log" aria-live="off" ref=${box}
      onScroll=${(e) => { const el = e.currentTarget; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40; }}>
      ${shown.length === 0 && html`<div class="empty">No log lines${level !== 'all' || filter ? ' match' : ' yet'}.${setting('logging.level') !== 'DEBUG' && level === 'debug' ? ' Debug lines appear when Settings › Privacy & logs › Log detail is Debug.' : ''}</div>`}
      ${shown.map((r) => html`<div class="logrow" key=${r.seq}><span class="muted">${r.t}</span><span class=${'lv-' + r.cls}>${r.lv}</span>
        <span class="muted" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.mod}</span>
        <span style="overflow-wrap:anywhere;white-space:pre-wrap" dir="auto">${r.msg}</span></div>`)}
    </div>
  </section>`;
}

function FallbackNotice() {
  const notice = app.use((s) => s.notice);
  if (!notice) return null;
  return html`<div class="notice info" style="align-items:center">
    <${Icon} name="doc" /><div class="grow"><b>${notice.message || 'Your dictation'}</b>
      <p dir="auto" style="margin:4px 0 0;color:var(--ink)">${notice.text}</p></div>
    <button type="button" class="btn" onClick=${() => call('copy_text', notice.text).then(() => toast('Copied'))}>Copy</button>
    <button type="button" class="btn ghost" onClick=${() => app.set({ notice: null })}>Dismiss</button>
  </div>`;
}

export function LiveScreen() {
  const fg = app.use((s) => s.foreground);
  const st = app.use((s) => s.state);
  return html`<div class="page">
    <div class="page-head">
      <div><h1 class="page-title">Live</h1><p class="page-sub">What the engine hears and does, as it happens.</p></div>
      <div class="row-flex">
        ${st.state === 'recording' && html`<span class="chip rec"><span class="dot"></span>${st.mode === 'dictate' ? 'Recording' : st.mode}</span>`}
        ${fg.exe && html`<span class="chip">${fg.exe}${fg.rule ? ` · rule “${fg.rule}”` : ''}</span>`}
      </div>
    </div>
    <${FallbackNotice} />
    <div class="grid-cards">
      <${NowCard} /><${ContextCard} /><${TodayCard} /><${LastDictation} />
    </div>
    <${Console} />
  </div>`;
}
