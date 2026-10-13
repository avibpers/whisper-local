// screens/commands.js
// Commands & AI: the wake word ("Hey Koli" or your own recorded word, which
// starts a command with no shortcut), voice commands (say a trigger after the
// command shortcut and it presses keys, types text, runs a program or
// rewrites the selection, with optional follow-up steps, in as many phrases
// and languages as you like) and AI transforms (rewrite the selection with a
// local model, from a shortcut). Shipped entries can be changed, switched off
// or reset; your own are kept as-is across updates.

import { html, useEffect, useState } from '../../vendor/preact-htm.module.js';
import { call } from '../api.js';
import { app, closeModal, confirmModal, openModal, saveSetting, toast, useSetting } from '../store.js';
import { HotkeyCapture, Icon, Keys, Toggle } from '../ui.js';
import { KindBadge, SettingRow, splitModel, TaskProgress, useAiCatalog, useRunningTasks } from './shared.js';

const ACTIONS = [['hotkey', 'Press keys', 'KEYS', 'acc'], ['type', 'Type text', 'TYPE', 'ok'], ['run', 'Run a program', 'RUN', 'warn'], ['rephrase', 'AI rewrite of the selection', 'AI', '']];
const ACTION = Object.fromEntries(ACTIONS.map(([k, label, tag, tone]) => [k, { label, tag, tone }]));
const ORIGIN = { shipped: '', changed: 'changed by you', yours: 'yours' };

function TypeTag({ action }) {
  const a = ACTION[action] || { tag: '?', tone: '' };
  const style = a.tone ? `background:var(--${a.tone}-soft);color:var(--${a.tone})` : 'background:var(--sunk);color:var(--ink2)';
  return html`<span class="typ" style=${style}>${a.tag}</span>`;
}

// ── Spoken numbers in keys ────────────────────────────────────────────────
// "d {n}" hears a number 0-9; keys ending in {n} press that number.

const NUMBER = '{n}';
const MODIFIER_KEYS = ['ctrl', 'alt', 'shift', 'win'];
const lastKey = (hotkey) => String(hotkey || '').split('+').pop();

// Keys with their last key swapped for the spoken number, or back to "1".
function withNumberKey(hotkey, on) {
  const parts = String(hotkey || '').split('+').filter(Boolean);
  if (!on) return parts.map((k) => (k === NUMBER ? '1' : k)).join('+');
  if (parts.length && !MODIFIER_KEYS.includes(parts[parts.length - 1])) parts.pop();
  return [...parts, NUMBER].join('+');
}

// ── Where the keys go ─────────────────────────────────────────────────────

const SEND_TO_HELP = {
  windows: 'No app gets the keys, so only Windows and other programs’ shortcuts react. You stay where you are.',
  app: 'Koli switches to the app, presses the keys, and brings you back. Nothing is pressed if it isn’t open.',
};

// The "Send to" choice for keys: the app in front (send_to left out),
// "windows", or an app's .exe picked from the apps open now or typed.
function SendToPicker({ step, onChange }) {
  const target = step.send_to || '';
  const [pickingApp, setPickingApp] = useState(!!target && target !== 'windows');
  const [apps, setApps] = useState([]);
  const [listId] = useState(() => `open-apps-${Math.random().toString(36).slice(2)}`);
  const mode = target === 'windows' ? 'windows' : pickingApp ? 'app' : '';
  useEffect(() => { if (mode === 'app') call('command_open_apps').then(setApps).catch(() => setApps([])); }, [mode]);
  const choose = (next) => {
    setPickingApp(next === 'app');
    const send_to = next === 'windows' ? 'windows' : next === 'app' && target !== 'windows' ? target : undefined;
    onChange({ ...step, send_to });
  };
  return html`<label class="row-flex"><span class="muted">Send to</span>
      <select class="sel" aria-label="Send the keys to" onChange=${(e) => choose(e.currentTarget.value)}>
        <option value="" selected=${mode === ''}>The app you’re in</option>
        <option value="windows" selected=${mode === 'windows'}>Windows (no app)</option>
        <option value="app" selected=${mode === 'app'}>A specific app…</option>
      </select></label>
    ${mode === 'app' && html`<input class="inp mono" style="width:180px" list=${listId} placeholder="chrome.exe" value=${target}
      aria-label="App to send the keys to" onChange=${(e) => onChange({ ...step, send_to: e.currentTarget.value.trim().toLowerCase() || undefined })} />
      <datalist id=${listId}>${apps.map((a) => html`<option value=${a.exe}>${a.title}</option>`)}</datalist>`}
    ${mode && html`<span class="small muted" style="flex-basis:100%">${SEND_TO_HELP[mode]}</span>`}`;
}

function StepEditor({ step, onChange, onRemove, first, phrase }) {
  const kind = step.delay != null ? 'delay' : ACTIONS.map(([k]) => k).find((k) => step[k] != null) || 'hotkey';
  const setKind = (k) => onChange(k === 'delay' ? { delay: 0.2 } : { [k]: '' });
  const value = kind === 'delay' ? step.delay : step[kind];
  const usesNumber = kind === 'hotkey' && lastKey(value) === NUMBER;
  // A new recording keeps "last key = the number" when it ends in a digit.
  const commitKeys = (keys) => onChange({ ...step, hotkey: usesNumber && /^\d$/.test(lastKey(keys)) ? withNumberKey(keys, true) : keys });
  return html`<div class="row-flex" style="padding:10px 12px;border:1px solid var(--line);border-radius:10px">
    <select class="sel" aria-label="Step type" onChange=${(e) => setKind(e.currentTarget.value)}>
      ${[...ACTIONS.map(([k, l]) => [k, l]), ...(first ? [] : [['delay', 'Wait']])].map(([k, l]) => html`<option value=${k} selected=${k === kind}>${l}</option>`)}
    </select>
    ${kind === 'hotkey' && html`<${HotkeyCapture} value=${value} label="Keys" sendsKeys=${true} onCommit=${commitKeys} />
      ${(usesNumber || (phrase || '').includes(NUMBER)) && html`<label class="row-flex small">Last key = the number you said
        <${Toggle} checked=${usesNumber} label="Last key is the number you said" onChange=${(on) => onChange({ ...step, hotkey: withNumberKey(value, on) })} /></label>`}
      <${SendToPicker} step=${step} onChange=${onChange} />`}
    ${kind === 'delay' && html`<input class="inp num mono" value=${value} aria-label="Seconds" onBlur=${(e) => onChange({ delay: Number(e.currentTarget.value) || 0 })} /><span class="muted">seconds (max 60)</span>`}
    ${(kind === 'type' || kind === 'run' || kind === 'rephrase') && html`<input class=${'inp' + (kind === 'run' ? ' mono' : '')} style="flex:1;min-width:200px" dir="auto"
      aria-label=${ACTION[kind].label} value=${value || ''} placeholder=${kind === 'run' ? 'notepad.exe' : kind === 'rephrase' ? 'Rewrite this to be more concise' : 'Text to type'}
      onBlur=${(e) => onChange({ [kind]: e.currentTarget.value })} />`}
    ${onRemove && html`<button type="button" class="btn ghost icon" aria-label="Remove step" onClick=${onRemove}><${Icon} name="x" /></button>`}
  </div>`;
}

function openCommandEditor(item, reload) {
  const entry = item ? { ...item.entry } : { trigger: '', hotkey: '' };
  const action = ACTIONS.map(([k]) => k).find((k) => entry[k] != null) || 'hotkey';
  let main = { [action]: entry[action] };
  let steps = (entry.then || []).map((s) => ({ ...s }));
  let trigger = entry.trigger || '';
  let otherTriggers = [...(entry.other_triggers || [])];
  let pattern = entry.match_regex || '';
  let confirmRun = !!entry.confirm;
  const Editor = () => {
    const [, force] = useState(0);
    const rerender = () => force((x) => x + 1);
    const hasRun = main.run != null || steps.some((s) => s.run != null);
    return html`<div class="modal wide" role="dialog" aria-modal="true"><div class="modal-head"><h2>${item ? `Edit “${item.id}”` : 'New voice command'}</h2></div>
      <div class="modal-body">
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px">
          <label class="stack" style="gap:6px"><span class="eyebrow">Say</span><input class="inp" dir="auto" value=${trigger} aria-label="Trigger phrase"
            onInput=${(e) => { trigger = e.currentTarget.value; rerender(); }} placeholder="open downloads" />
            <span class="small muted">Put {n} where you say a number 0–9: “d {n}” hears “D 1” and “D two”.</span></label>
          <label class="stack" style="gap:6px"><span class="eyebrow">Or match a pattern (advanced)</span><input class="inp mono" value=${pattern} aria-label="Pattern"
            onInput=${(e) => { pattern = e.currentTarget.value; }} placeholder="regular expression" /></label>
        </div>
        <div class="stack" style="gap:6px"><span class="eyebrow">Also say</span>
          ${otherTriggers.map((p, i) => html`<div class="row-flex"><input class="inp" style="flex:1;min-width:200px" dir="auto" value=${p} aria-label=${`Other phrase ${i + 1}`}
            onInput=${(e) => { otherTriggers[i] = e.currentTarget.value; }} placeholder="תפעיל ספוטיפיי" />
            <button type="button" class="btn ghost icon" aria-label="Remove phrase" onClick=${() => { otherTriggers.splice(i, 1); rerender(); }}><${Icon} name="x" /></button></div>`)}
          <button type="button" class="btn ghost" style="align-self:flex-start" onClick=${() => { otherTriggers.push(''); rerender(); }}><${Icon} name="plus" />Another way to say it</button>
          <span class="small muted">Any language. Each phrase runs the same command.</span></div>
        <span class="eyebrow">Does</span>
        <${StepEditor} first=${true} phrase=${trigger} step=${main} onChange=${(s) => { main = s; rerender(); }} />
        ${steps.map((s, i) => html`<${StepEditor} phrase=${trigger} step=${s} onChange=${(n) => { steps[i] = n; rerender(); }} onRemove=${() => { steps.splice(i, 1); rerender(); }} />`)}
        <button type="button" class="btn ghost" style="align-self:flex-start" onClick=${() => { steps.push({ delay: 0.2 }); rerender(); }}><${Icon} name="plus" />Add a step</button>
        ${hasRun && html`<div class="notice warn" style="align-items:center"><span class="grow">Run steps execute with your user rights. A run step that uses ${'${clipboard}'} or ${'${selection}'} always asks first.</span>
          <label class="row-flex" style="color:var(--ink)">Always ask<${Toggle} checked=${confirmRun} label="Always ask before running" onChange=${(v) => { confirmRun = v; rerender(); }} /></label></div>`}
      </div>
      <div class="modal-foot"><button type="button" class="btn" onClick=${closeModal}>Cancel</button>
        <button type="button" class="btn pri" onClick=${() => {
          const others = otherTriggers.map((p) => p.trim()).filter(Boolean);
          const out = { trigger: trigger.trim(), other_triggers: others.length ? others : undefined, match_regex: pattern.trim() || undefined, ...main, then: steps.length ? steps : undefined, confirm: confirmRun || undefined };
          call('command_save', out, item ? item.id : null).then(() => { closeModal(); toast('Command saved'); reload(); }).catch((e) => toast(e.message, { kind: 'err' }));
        }}>Save</button></div></div>`;
  };
  openModal(() => html`<${Editor} />`);
}

function VoiceCommands() {
  const enabled = useSetting('voice_commands.enabled');
  const cmdKey = useSetting('hotkey.command_hotkey');
  const [items, setItems] = useState([]);
  const [phrase, setPhrase] = useState('');
  const [test, setTest] = useState(null);
  const reload = () => call('commands_list').then(setItems).catch((e) => toast(e.message, { kind: 'err' }));
  useEffect(() => { reload(); }, []);
  useEffect(() => {
    if (!phrase.trim()) { setTest(null); return undefined; }
    const t = setTimeout(() => call('command_test', phrase).then(setTest).catch(() => {}), 200);
    return () => clearTimeout(t);
  }, [phrase, items]);
  const remove = async (it) => {
    const shipped = it.origin !== 'yours';
    if (!(await confirmModal({ title: shipped ? `Reset “${it.id}”?` : `Delete “${it.id}”?`, body: shipped ? 'Your changes are removed and it works as it ships.' : 'This command is removed.', confirm: shipped ? 'Reset' : 'Delete', danger: !shipped }))) return;
    call('command_delete', it.id).then(reload);
  };
  return html`<section class="card" style="overflow:hidden" aria-labelledby="h-cmd">
    <div class="card-head"><div><h2 id="h-cmd">Voice commands</h2><p>Hold <${Keys} hotkey=${cmdKey} empty="(no shortcut)" />, say a trigger, and it runs. Triggers match anywhere in what you say, longest first.</p></div>
      <div class="row-flex"><label class="row-flex" style="font-weight:500">On<${Toggle} checked=${enabled} label="Voice commands" onChange=${(v) => saveSetting('voice_commands.enabled', v)} /></label>
        <button type="button" class="btn" onClick=${() => openCommandEditor(null, reload)}><${Icon} name="plus" />New command</button></div></div>
    <div class="row-flex" style="padding:0 18px 14px"><input class="inp" style="width:260px" dir="auto" aria-label="Try a phrase" placeholder="Try a phrase…" value=${phrase} onInput=${(e) => setPhrase(e.currentTarget.value)} />
      ${test && html`<span class=${'chip ' + (test.matched ? 'ok' : '')}>${test.matched ? `Runs “${test.trigger}”${test.phrase && test.phrase !== test.trigger ? ` (said as “${test.phrase}”)` : ''} (${ACTION[test.action] ? ACTION[test.action].label.toLowerCase() : test.action})` : test.reason || 'No command matches'}</span>`}</div>
    <div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col">Say</th><th scope="col">Does</th><th scope="col">Details</th><th scope="col">On</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead>
      <tbody>${ACTIONS.map(([k, label]) => {
        const rows = items.filter((it) => it.action === k);
        if (!rows.length) return null;
        return html`<tr class="grp"><td colspan="5">${label}</td></tr>
          ${rows.map((it) => html`<tr class=${it.disabled ? 'off' : ''}>
            <td dir="auto">${it.entry.trigger || html`<span class="mono">${it.entry.match_regex}</span>`} ${ORIGIN[it.origin] && html`<span class="chip sm">${ORIGIN[it.origin]}</span>`}
              ${(it.entry.other_triggers || []).length > 0 && html`<div class="small muted" dir="auto">also: ${it.entry.other_triggers.join(' · ')}</div>`}</td>
            <td><${TypeTag} action=${it.action} />${it.entry.then && html` <span class="small muted">+${it.entry.then.length} steps</span>`}</td>
            <td>${k === 'hotkey' ? html`<${Keys} hotkey=${it.entry.hotkey} />${it.entry.send_to && html` <span class="small muted">to ${it.entry.send_to === 'windows' ? 'Windows' : it.entry.send_to}</span>`}` : html`<span class=${k === 'run' ? 'mono small' : 'small'} style="color:var(--ink2)" dir="auto">${String(it.entry[k]).slice(0, 80)}</span>`}</td>
            <td><${Toggle} checked=${!it.disabled} label=${`Enable ${it.id}`} onChange=${(v) => call('command_enable', it.id, v).then(reload)} /></td>
            <td class="end"><button type="button" class="btn ghost sm" onClick=${() => openCommandEditor(it, reload)}>Edit</button>
              ${it.origin !== 'shipped' && html`<button type="button" class="btn ghost sm" onClick=${() => remove(it)}>${it.origin === 'yours' ? 'Delete' : 'Reset'}</button>`}</td></tr>`)}`;
      })}</tbody></table></div>
  </section>`;
}

// ── Wake word ─────────────────────────────────────────────────────────────
// "Hey Koli" for anyone, or your own word: recorded three times, it wakes
// Koli only in your voice. Test says whether a try would have woken it.

function openWakeRecorder(onSaved) {
  const Recorder = () => {
    const [ready, setReady] = useState(false);
    const [heard, setHeard] = useState([]);
    const [busy, setBusy] = useState('');       // 'listening' | 'saving'
    const [error, setError] = useState('');
    const [needed, setNeeded] = useState(3);
    const [spelling, setSpelling] = useState('');   // how you write it, so Whisper expects it
    const tasks = useRunningTasks('meeting');
    // Fresh recordings; waits while the voice model gets ready (downloaded once).
    const start = () => { setHeard([]); setError(''); call('wake_word_prepare').then((r) => setReady(r.ready)).catch((e) => setError(e.message)); };
    useEffect(() => { start(); }, []);
    useEffect(() => {
      if (ready) return undefined;
      const t = setInterval(() => call('wake_word_prepare').then((r) => r.ready && setReady(true)).catch(() => {}), 1500);
      return () => clearInterval(t);
    }, [ready]);
    const record = () => {
      setBusy('listening'); setError('');
      // The first recording starts the set with the spelling as typed now.
      (heard.length ? Promise.resolve() : call('wake_word_prepare', spelling))
        .then(() => call('wake_word_record'))
        .then((r) => { setHeard((h) => [...h, r.heard]); setNeeded(r.needed); })
        .catch((e) => setError(e.message)).finally(() => setBusy(''));
    };
    const save = () => {
      setBusy('saving');
      call('wake_word_save').then((s) => { closeModal(); toast(`Your wake word is “${s.word}”`); onSaved(); })
        .catch((e) => { setError(e.message); setHeard([]); }).finally(() => setBusy(''));
    };
    const done = heard.length >= needed;
    return html`<div class="modal" role="dialog" aria-modal="true"><div class="modal-head"><h2>Record your wake word</h2></div>
      <div class="modal-body">
        <p>Press Record and say your word once, in any language. ${needed} times in all, the way you'll say it. After that it wakes Koli only in your voice, on this microphone. Two or three syllables work best (“Hey Avia”).</p>
        <label class="stack" style="gap:6px"><span class="eyebrow">How do you spell it? (optional)</span>
          <input class="inp" dir="auto" value=${spelling} placeholder="Hey Avia" aria-label="How you spell your wake word" disabled=${heard.length > 0}
            onInput=${(e) => setSpelling(e.currentTarget.value)} />
          <span class="small muted">For a made-up word: Whisper writes it the way you type it here instead of a real word that sounds close (“hey yeah”).</span></label>
        ${!ready && html`<div class="notice"><span class="grow">Getting the voice model ready…</span></div>${tasks.map((t) => html`<${TaskProgress} task=${t} cancel=${false} />`)}`}
        <ol class="stack" style="gap:6px;margin:0;padding-inline-start:20px">
          ${Array.from({ length: needed }, (_, i) => html`<li dir="auto">${heard[i] ? html`<span class="chip ok">Heard “${heard[i]}”</span>`
            : i === heard.length && busy === 'listening' ? html`<span class="chip">Say it now…</span>` : html`<span class="muted">—</span>`}</li>`)}
        </ol>
        ${error && html`<div class="notice warn" role="alert"><span class="grow">${error}</span></div>`}
      </div>
      <div class="modal-foot"><button type="button" class="btn" onClick=${closeModal}>Cancel</button>
        ${heard.length > 0 && html`<button type="button" class="btn" disabled=${!!busy} onClick=${start}>Start over</button>`}
        ${done ? html`<button type="button" class="btn pri" disabled=${!!busy} onClick=${save}>Save</button>`
          : html`<button type="button" class="btn pri" disabled=${!ready || !!busy} onClick=${record}><${Icon} name="mic" />${busy === 'listening' ? 'Listening…' : 'Record'}</button>`}</div></div>`;
  };
  openModal(() => html`<${Recorder} />`);
}

function WakeWord() {
  const enabled = useSetting('wake_word.enabled');
  const commandsOn = useSetting('voice_commands.enabled');
  const phrases = useSetting('wake_word.phrases');
  const schema = app.use((s) => s.schema);
  const [status, setStatus] = useState(null);
  const [testing, setTesting] = useState(false);
  const [test, setTest] = useState(null);
  const reload = () => call('wake_word_status').then(setStatus).catch(() => setStatus(null));
  useEffect(() => { reload(); }, [JSON.stringify(phrases || [])]);
  const runTest = () => {
    setTesting(true); setTest(null);
    call('wake_word_test').then(setTest).catch((e) => setTest({ error: e.message })).finally(() => setTesting(false));
  };
  const reset = async () => {
    if (!(await confirmModal({ title: 'Back to “Hey Koli”?', body: 'Your word and your voice recordings are removed. “Hey Koli” then works for any voice.', confirm: 'Reset' }))) return;
    call('wake_word_reset').then(() => { setTest(null); reload(); }).catch((e) => toast(e.message, { kind: 'err' }));
  };
  const rows = ['wake_word.sensitivity', 'wake_word.voice_match', 'wake_word.command_timeout_seconds', 'wake_word.follow_up_seconds']
    .filter((p) => p !== 'wake_word.voice_match' || (status && status.personal))
    .map((p) => schema && schema.settings.find((x) => x.path === p)).filter(Boolean);
  const testText = test && (test.error || [
    test.woke ? 'Woke Koli' : 'Wouldn’t wake Koli',
    test.heard ? `heard “${test.heard}”` : 'heard nothing',
    test.similarity != null ? `voice ${test.similarity} (needs ${test.needs})` : test.needs != null ? 'voice unknown on this microphone' : '',
  ].filter(Boolean).join(' · '));
  return html`<section class="card" style="overflow:hidden" aria-labelledby="h-wake">
    <div class="card-head"><div><h2 id="h-wake">Wake word</h2><p>Say “Hey Koli, next song”, or “Hey Koli”, a pause, and the command. No shortcut.</p></div>
      <div class="row-flex"><label class="row-flex" style="font-weight:500">On<${Toggle} checked=${enabled} label="Wake word" onChange=${(v) => saveSetting('wake_word.enabled', v)} /></label></div></div>
    <div class="stack" style="padding:0 18px 14px;gap:10px">
      ${enabled && !commandsOn && html`<div class="notice warn"><span class="grow">Voice commands are off, so the wake word has nothing to run. Turn them on below.</span></div>`}
      ${status && html`<div class="row-flex" style="flex-wrap:wrap">
        <span class="chip ok" dir="auto">${status.personal ? `Wakes on “${status.word}” · only your voice` : 'Wakes on “Hey Koli” · any voice'}</span>
        ${status.can_record && html`<button type="button" class="btn" onClick=${() => openWakeRecorder(reload)}><${Icon} name="mic" />${status.personal ? 'Record again' : 'Record your own word'}</button>`}
        <button type="button" class="btn" disabled=${testing} onClick=${runTest}>${testing ? 'Say it now…' : 'Test'}</button>
        ${status.personal && html`<button type="button" class="btn ghost" onClick=${reset}>Back to “Hey Koli”</button>`}
      </div>`}
      ${testText && html`<span class=${'chip ' + (test.woke ? 'ok' : '')} style="align-self:flex-start" dir="auto" role="status">${testText}</span>`}
      ${status && status.personal && !status.recorded_here && html`<div class="notice warn"><span class="grow">Your word isn't recorded on this microphone (${status.mic}), so it can't wake Koli here. Record it again with this microphone.</span></div>`}
      ${status && status.personal && status.spellings.length > 1 && html`<span class="small muted" dir="auto">Also matches: ${status.spellings.slice(1).join(' · ')}</span>`}
      <span class="small muted">It doesn't listen while you dictate, during a meeting, while Koli is paused or while the PC is locked.</span>
    </div>
    ${rows.map((s) => html`<${SettingRow} s=${s} />`)}
  </section>`;
}

function openTransformEditor(item, reload) {
  const e = item ? { ...item.entry } : { name: '', description: '', prompt: '', hotkey: '' };
  openModal(() => html`<div class="modal wide"><div class="modal-head"><h2>${item ? `Edit “${item.id}”` : 'New transform'}</h2></div>
    <div class="modal-body">
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px">
        <label class="stack" style="gap:6px"><span class="eyebrow">Name</span><input class="inp" value=${e.name} onInput=${(ev) => { e.name = ev.currentTarget.value; }} /></label>
        <label class="stack" style="gap:6px"><span class="eyebrow">What it does</span><input class="inp" value=${e.description || ''} onInput=${(ev) => { e.description = ev.currentTarget.value; }} /></label>
        <label class="stack" style="gap:6px"><span class="eyebrow">Model override</span><input class="inp mono" placeholder="provider/model, e.g. lmstudio/qwen3-8b" value=${e.model || ''} onInput=${(ev) => { e.model = ev.currentTarget.value; }} /></label>
        <label class="stack" style="gap:6px"><span class="eyebrow">Timeout (seconds)</span><input class="inp mono" placeholder="the Rewrite limit" value=${e.timeout ?? ''} onInput=${(ev) => { e.timeout = ev.currentTarget.value; }} /></label>
      </div>
      <label class="stack" style="gap:6px"><span class="eyebrow">Instruction</span><textarea class="inp" rows="4" value=${e.prompt} onInput=${(ev) => { e.prompt = ev.currentTarget.value; }}></textarea></label>
    </div>
    <div class="modal-foot"><button type="button" class="btn" onClick=${closeModal}>Cancel</button>
      <button type="button" class="btn pri" onClick=${() => call('transform_save', e, item ? item.id : null).then(() => { closeModal(); toast('Transform saved'); reload(); }).catch((err) => toast(err.message, { kind: 'err' }))}>Save</button></div></div>`);
}

function Transforms() {
  const [items, setItems] = useState([]);
  const rewriteModel = useSetting('ai.rewrite.model');
  const defaultModel = useSetting('ai.model');
  const model = rewriteModel || defaultModel;
  const catalog = useAiCatalog();
  const kind = (catalog.find((p) => p.id === splitModel(model)[0]) || {}).kind;
  const reload = () => call('transforms_list').then(setItems).catch((e) => toast(e.message, { kind: 'err' }));
  useEffect(() => { reload(); }, []);
  const setKey = (it, key) => call('transform_save', { ...it.entry, hotkey: key || '' }, it.id).then(() => { toast('Shortcut saved'); reload(); }).catch((e) => toast(e.message, { kind: 'err' }));
  return html`<section class="card" style="overflow:hidden" aria-labelledby="h-tr">
    <div class="card-head"><div><h2 id="h-tr">Transforms</h2><p>Select text anywhere, press a shortcut, and the AI rewrites it in place.</p></div>
      <div class="row-flex">${model ? html`<span class="chip"><span class="mono">${model}</span></span><${KindBadge} kind=${kind} />`
        : html`<span class="chip rec"><span class="dot"></span>No AI model chosen</span>`}
        <a class="btn" href="#/settings/ai">AI settings</a>
        <button type="button" class="btn" onClick=${() => openTransformEditor(null, reload)}><${Icon} name="plus" />New transform</button></div></div>
    <div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col">Name</th><th scope="col">What it does</th><th scope="col">Shortcut</th><th scope="col">Model</th><th scope="col">On</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead>
      <tbody>${items.map((it) => html`<tr class=${it.disabled ? 'off' : ''}>
        <td><b style="font-weight:500">${it.entry.name}</b> ${ORIGIN[it.origin] && html`<span class="chip sm">${ORIGIN[it.origin]}</span>`}</td>
        <td style="color:var(--ink2)">${it.entry.description || ''}</td>
        <td><${HotkeyCapture} value=${it.entry.hotkey} label=${`${it.entry.name} shortcut`} nullable=${true} onCommit=${(v) => setKey(it, v)} /></td>
        <td class="muted">${it.entry.model || 'default'}</td>
        <td><${Toggle} checked=${!it.disabled} label=${`Enable ${it.entry.name}`} onChange=${(v) => call('transform_enable', it.id, v).then(reload)} /></td>
        <td class="end"><button type="button" class="btn ghost sm" onClick=${() => call('transform_apply', it.id).then((ok) => toast(ok ? 'Applied to your selection' : 'Select some text in another app first')).catch((e) => toast(e.message, { kind: 'err' }))}>Run</button>
          <button type="button" class="btn ghost sm" onClick=${() => openTransformEditor(it, reload)}>Edit</button>
          ${it.origin !== 'shipped' && html`<button type="button" class="btn ghost sm" onClick=${() => call('transform_delete', it.id).then(reload)}>${it.origin === 'yours' ? 'Delete' : 'Reset'}</button>`}</td></tr>`)}
      </tbody></table></div>
  </section>`;
}

export function CommandsScreen() {
  return html`<div class="page">
    <div><h1 class="page-title">Commands & AI</h1><p class="page-sub">Shipped commands update with the app; yours are kept.</p></div>
    <${WakeWord} /><${VoiceCommands} /><${Transforms} />
  </div>`;
}
