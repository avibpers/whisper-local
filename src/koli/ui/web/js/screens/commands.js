// screens/commands.js
// Commands & AI: voice commands (say a trigger after the command shortcut and
// it presses keys, types text, runs a program or rewrites the selection, with
// optional follow-up steps) and AI transforms (rewrite the selection with a
// local model, from a shortcut). Shipped entries can be changed, switched off
// or reset; your own are kept as-is across updates.

import { html, useEffect, useState } from '../../vendor/preact-htm.module.js';
import { call } from '../api.js';
import { app, closeModal, confirmModal, openModal, saveSetting, toast, useSetting } from '../store.js';
import { HotkeyCapture, Icon, Keys, Toggle } from '../ui.js';

const ACTIONS = [['hotkey', 'Press keys', 'KEYS', 'acc'], ['type', 'Type text', 'TYPE', 'ok'], ['run', 'Run a program', 'RUN', 'warn'], ['rephrase', 'AI rewrite of the selection', 'AI', '']];
const ACTION = Object.fromEntries(ACTIONS.map(([k, label, tag, tone]) => [k, { label, tag, tone }]));
const ORIGIN = { shipped: '', changed: 'changed by you', yours: 'yours' };

function TypeTag({ action }) {
  const a = ACTION[action] || { tag: '?', tone: '' };
  const style = a.tone ? `background:var(--${a.tone}-soft);color:var(--${a.tone})` : 'background:var(--sunk);color:var(--ink2)';
  return html`<span class="typ" style=${style}>${a.tag}</span>`;
}

function StepEditor({ step, onChange, onRemove, first }) {
  const kind = step.delay != null ? 'delay' : ACTIONS.map(([k]) => k).find((k) => step[k] != null) || 'hotkey';
  const setKind = (k) => onChange(k === 'delay' ? { delay: 0.2 } : { [k]: '' });
  const value = kind === 'delay' ? step.delay : step[kind];
  return html`<div class="row-flex" style="padding:10px 12px;border:1px solid var(--line);border-radius:10px">
    <select class="sel" aria-label="Step type" onChange=${(e) => setKind(e.currentTarget.value)}>
      ${[...ACTIONS.map(([k, l]) => [k, l]), ...(first ? [] : [['delay', 'Wait']])].map(([k, l]) => html`<option value=${k} selected=${k === kind}>${l}</option>`)}
    </select>
    ${kind === 'hotkey' && html`<${HotkeyCapture} value=${value} label="Keys" onCommit=${(v) => onChange({ hotkey: v })} />`}
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
            onInput=${(e) => { trigger = e.currentTarget.value; }} placeholder="open downloads" /></label>
          <label class="stack" style="gap:6px"><span class="eyebrow">Or match a pattern (advanced)</span><input class="inp mono" value=${pattern} aria-label="Pattern"
            onInput=${(e) => { pattern = e.currentTarget.value; }} placeholder="regular expression" /></label>
        </div>
        <span class="eyebrow">Does</span>
        <${StepEditor} first=${true} step=${main} onChange=${(s) => { main = s; rerender(); }} />
        ${steps.map((s, i) => html`<${StepEditor} step=${s} onChange=${(n) => { steps[i] = n; rerender(); }} onRemove=${() => { steps.splice(i, 1); rerender(); }} />`)}
        <button type="button" class="btn ghost" style="align-self:flex-start" onClick=${() => { steps.push({ delay: 0.2 }); rerender(); }}><${Icon} name="plus" />Add a step</button>
        ${hasRun && html`<div class="notice warn" style="align-items:center"><span class="grow">Run steps execute with your user rights. A run step that uses ${'${clipboard}'} or ${'${selection}'} always asks first.</span>
          <label class="row-flex" style="color:var(--ink)">Always ask<${Toggle} checked=${confirmRun} label="Always ask before running" onChange=${(v) => { confirmRun = v; rerender(); }} /></label></div>`}
      </div>
      <div class="modal-foot"><button type="button" class="btn" onClick=${closeModal}>Cancel</button>
        <button type="button" class="btn pri" onClick=${() => {
          const out = { trigger: trigger.trim(), match_regex: pattern.trim() || undefined, ...main, then: steps.length ? steps : undefined, confirm: confirmRun || undefined };
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
      ${test && html`<span class=${'chip ' + (test.matched ? 'ok' : '')}>${test.matched ? `Runs “${test.trigger}” (${ACTION[test.action] ? ACTION[test.action].label.toLowerCase() : test.action})` : test.reason || 'No command matches'}</span>`}</div>
    <div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col">Say</th><th scope="col">Does</th><th scope="col">Details</th><th scope="col">On</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead>
      <tbody>${ACTIONS.map(([k, label]) => {
        const rows = items.filter((it) => it.action === k);
        if (!rows.length) return null;
        return html`<tr class="grp"><td colspan="5">${label}</td></tr>
          ${rows.map((it) => html`<tr class=${it.disabled ? 'off' : ''}>
            <td dir="auto">${it.entry.trigger || html`<span class="mono">${it.entry.match_regex}</span>`} ${ORIGIN[it.origin] && html`<span class="chip sm">${ORIGIN[it.origin]}</span>`}</td>
            <td><${TypeTag} action=${it.action} />${it.entry.then && html` <span class="small muted">+${it.entry.then.length} steps</span>`}</td>
            <td>${k === 'hotkey' ? html`<${Keys} hotkey=${it.entry.hotkey} />` : html`<span class=${k === 'run' ? 'mono small' : 'small'} style="color:var(--ink2)" dir="auto">${String(it.entry[k]).slice(0, 80)}</span>`}</td>
            <td><${Toggle} checked=${!it.disabled} label=${`Enable ${it.id}`} onChange=${(v) => call('command_enable', it.id, v).then(reload)} /></td>
            <td class="end"><button type="button" class="btn ghost sm" onClick=${() => openCommandEditor(it, reload)}>Edit</button>
              ${it.origin !== 'shipped' && html`<button type="button" class="btn ghost sm" onClick=${() => remove(it)}>${it.origin === 'yours' ? 'Delete' : 'Reset'}</button>`}</td></tr>`)}`;
      })}</tbody></table></div>
  </section>`;
}

function openTransformEditor(item, reload) {
  const e = item ? { ...item.entry } : { name: '', description: '', prompt: '', hotkey: '' };
  openModal(() => html`<div class="modal wide"><div class="modal-head"><h2>${item ? `Edit “${item.id}”` : 'New transform'}</h2></div>
    <div class="modal-body">
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px">
        <label class="stack" style="gap:6px"><span class="eyebrow">Name</span><input class="inp" value=${e.name} onInput=${(ev) => { e.name = ev.currentTarget.value; }} /></label>
        <label class="stack" style="gap:6px"><span class="eyebrow">What it does</span><input class="inp" value=${e.description || ''} onInput=${(ev) => { e.description = ev.currentTarget.value; }} /></label>
        <label class="stack" style="gap:6px"><span class="eyebrow">Model override</span><input class="inp" placeholder="use the AI settings model" value=${e.model || ''} onInput=${(ev) => { e.model = ev.currentTarget.value; }} /></label>
        <label class="stack" style="gap:6px"><span class="eyebrow">Timeout (seconds)</span><input class="inp mono" placeholder="5" value=${e.timeout ?? ''} onInput=${(ev) => { e.timeout = ev.currentTarget.value; }} /></label>
      </div>
      <label class="stack" style="gap:6px"><span class="eyebrow">Instruction</span><textarea class="inp" rows="4" value=${e.prompt} onInput=${(ev) => { e.prompt = ev.currentTarget.value; }}></textarea></label>
    </div>
    <div class="modal-foot"><button type="button" class="btn" onClick=${closeModal}>Cancel</button>
      <button type="button" class="btn pri" onClick=${() => call('transform_save', e, item ? item.id : null).then(() => { closeModal(); toast('Transform saved'); reload(); }).catch((err) => toast(err.message, { kind: 'err' }))}>Save</button></div></div>`);
}

function Transforms() {
  const [items, setItems] = useState([]);
  const [ollama, setOllama] = useState(null);
  const reload = () => call('transforms_list').then(setItems).catch((e) => toast(e.message, { kind: 'err' }));
  useEffect(() => { reload(); call('ai_probe').then(setOllama).catch(() => {}); }, []);
  const setKey = (it, key) => call('transform_save', { ...it.entry, hotkey: key || '' }, it.id).then(() => { toast('Shortcut saved'); reload(); }).catch((e) => toast(e.message, { kind: 'err' }));
  return html`<section class="card" style="overflow:hidden" aria-labelledby="h-tr">
    <div class="card-head"><div><h2 id="h-tr">Transforms</h2><p>Select text anywhere, press a shortcut, and your local AI rewrites it in place.</p></div>
      <div class="row-flex">${ollama && html`<span class=${'chip ' + (ollama.reachable ? 'ok' : 'rec')}><span class="dot"></span>${ollama.reachable ? 'Ollama reachable' : 'Ollama not reachable'}</span>`}
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
    <${VoiceCommands} /><${Transforms} />
  </div>`;
}
