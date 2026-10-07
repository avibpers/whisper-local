// screens/apps.js
// Apps & profiles: per-app rules (checked top to bottom, first match wins;
// your own rules come before the shipped ones) with a full editor for every
// override, and profiles — named sets of settings switched in one click.

import { html, useEffect, useState } from '../../vendor/preact-htm.module.js';
import { call } from '../api.js';
import { app, closeModal, confirmModal, openModal, toast } from '../store.js';
import { Icon, Segmented, Select, TagInput, Toggle } from '../ui.js';

const TRI = [[null, 'Default'], [true, 'Always'], [false, 'Never']];
const FORMATTING = [['capitalize_first', 'Capitalize first letter'], ['ensure_punctuation', 'End with punctuation'],
  ['strip_trailing_period', 'Drop final period'], ['inline_formatting', 'Voice punctuation'], ['lowercase', 'Lowercase'],
  ['list_formatting', 'Spoken lists']];
const STYLE_OPTS = [[null, 'Default'], ['formal', 'Formal'], ['casual', 'Casual'], ['very_casual', 'Very casual'], ['verbatim', 'Verbatim']];
const LANGS = [[null, 'Default'], ['auto', 'Auto-detect'], ['en', 'English'], ['he', 'Hebrew'], ['ar', 'Arabic'], ['es', 'Spanish'], ['fr', 'French'], ['de', 'German'], ['ru', 'Russian']];

function summary(e) {
  const bits = [];
  if (e.suppress) bits.push('Never deliver');
  if (e.auto_paste === false) bits.push('Copy only');
  if (e.auto_send === true) bits.push('Auto-send');
  if (e.style) bits.push(e.style.replace('_', ' '));
  if (e.language) bits.push(`language ${e.language}`);
  if (e.initial_prompt) bits.push('prompt');
  return bits.join(' · ') || 'No changes';
}

function RuleEditor({ item, onSaved, onDeleted }) {
  const [e, setE] = useState({ ...item.entry });
  useEffect(() => setE({ ...item.entry }), [item]);
  const set = (k, v) => setE((cur) => { const n = { ...cur }; if (v === null || v === undefined || v === '') delete n[k]; else n[k] = v; return n; });
  const save = () => call('rule_save', e, item.isNew ? null : item.id).then((id) => { toast('Rule saved'); onSaved(id); }).catch((err) => toast(err.message, { kind: 'err' }));
  const pasteMode = e.auto_paste === false ? false : e.auto_paste === true ? true : null;
  const Row = ({ label, children }) => html`<div class="row-flex" style="justify-content:space-between;padding:10px 0;border-top:1px solid var(--line)"><span>${label}</span>${children}</div>`;
  return html`<div style="padding:14px 18px 18px" aria-label="Rule editor">
    <div class="row-flex" style="justify-content:space-between;margin-bottom:8px">
      <input class="inp" style="font-weight:600;width:240px" aria-label="Rule name" value=${e.name || item.id} placeholder="Rule name" onInput=${(ev) => set('name', ev.currentTarget.value)} />
      ${item.origin && html`<span class="chip">${item.origin === 'yours' ? 'Yours' : 'Shipped · your edits are kept on update'}</span>`}</div>
    <div style="padding:10px 0;border-top:1px solid var(--line)"><div style="font-weight:500">Applies to</div>
      <div class="small muted" style="margin-bottom:6px">Program name or window title. Browser apps match by title.</div>
      <${TagInput} values=${e.match || []} label="Programs and titles" onChange=${(v) => set('match', v)} /></div>
    <div class="eyebrow" style="margin-top:10px">Delivery</div>
    <${Row} label="Press Enter after pasting"><${Segmented} label="Auto-send" value=${e.auto_send ?? null} options=${TRI} onChange=${(v) => set('auto_send', v)} /></${Row}>
    <${Row} label="Paste at cursor"><${Segmented} label="Paste" value=${pasteMode} options=${[[null, 'Default'], [true, 'Paste'], [false, 'Copy only']]} onChange=${(v) => set('auto_paste', v)} /></${Row}>
    <${Row} label="Don't deliver at all"><${Toggle} checked=${!!e.suppress} label="Suppress" onChange=${(v) => set('suppress', v || null)} /></${Row}>
    <div class="eyebrow" style="margin-top:10px">Listening</div>
    <${Row} label="Language"><${Select} label="Language" value=${e.language ?? null} options=${LANGS} onChange=${(v) => set('language', v)} /></${Row}>
    <${Row} label="Output"><${Segmented} label="Output" value=${e.task ?? null} options=${[[null, 'Default'], ['transcribe', 'Transcribe'], ['translate', 'Translate']]} onChange=${(v) => set('task', v)} /></${Row}>
    <div style="padding:10px 0;border-top:1px solid var(--line)"><div style="margin-bottom:6px">Extra context prompt</div>
      <textarea class="inp" rows="2" aria-label="Rule prompt" value=${e.initial_prompt || ''} onInput=${(ev) => set('initial_prompt', ev.currentTarget.value)}></textarea></div>
    <div class="eyebrow" style="margin-top:10px">Formatting</div>
    <${Row} label="Writing style"><${Segmented} label="Style" value=${e.style ?? null} options=${STYLE_OPTS} onChange=${(v) => set('style', v)} /></${Row}>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));column-gap:24px">
      ${FORMATTING.map(([k, l]) => html`<${Row} label=${l}><${Select} label=${l} value=${e[k] ?? null} options=${[[null, 'From style'], [true, 'On'], [false, 'Off']]} onChange=${(v) => set(k, v)} /></${Row}>`)}
    </div>
    <div class="row-flex" style="justify-content:flex-end;margin-top:12px">
      ${!item.isNew && item.origin !== 'shipped' && html`<button type="button" class="btn danger" onClick=${async () => {
        if (await confirmModal({ title: item.origin === 'yours' ? 'Delete this rule?' : 'Reset this rule?', body: item.origin === 'yours' ? 'It stops applying right away.' : 'Your changes are removed; it works as it ships.', confirm: item.origin === 'yours' ? 'Delete' : 'Reset', danger: true })) {
          call('rule_delete', item.id).then(onDeleted);
        }
      }}>${item.origin === 'yours' ? 'Delete rule' : 'Reset to shipped'}</button>`}
      <button type="button" class="btn pri" onClick=${save}>Save rule</button></div>
  </div>`;
}

function Rules() {
  const fg = app.use((s) => s.foreground);
  const [data, setData] = useState({ rules: [], matching: null, foreground: {} });
  const [sel, setSel] = useState(null);
  const reload = (keep) => call('rules_list').then((d) => { setData(d); if (keep) setSel(d.rules.find((r) => r.id === keep) || d.rules[0]); }).catch((e) => toast(e.message, { kind: 'err' }));
  useEffect(() => { reload(); }, []);
  useEffect(() => { if (!sel && data.rules.length) setSel(data.rules.find((r) => r.id === data.matching) || data.rules[0]); }, [data]);
  const own = data.rules.filter((r) => r.origin === 'yours');
  const move = (id, dir) => {
    const ids = own.map((r) => r.id);
    const i = ids.indexOf(id);
    const j = i + dir;
    if (j < 0 || j >= ids.length) return;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    call('rules_reorder', ids).then(() => reload(sel && sel.id));
  };
  const forThisApp = () => {
    const exe = (fg && fg.exe) || (data.foreground && data.foreground.exe);
    if (!exe) { toast('Switch to the app first, then come back here.'); return; }
    setSel({ id: 'new', isNew: true, origin: null, entry: { match: [exe], name: exe.replace('.exe', '') } });
  };
  return html`<section class="card" aria-labelledby="h-rules">
    <div class="card-head"><div><h2 id="h-rules">App rules</h2><p>Checked top to bottom when you finish speaking. The first match wins. Your own rules come before the shipped ones.</p></div>
      <div class="row-flex">${(fg.exe || (data.foreground || {}).exe) && html`<span class="chip">Last app: ${fg.exe || data.foreground.exe}</span>`}
        <button type="button" class="btn" onClick=${forThisApp}>Rule for that app</button>
        <button type="button" class="btn" onClick=${() => setSel({ id: 'new', isNew: true, origin: null, entry: { match: [] } })}><${Icon} name="plus" />New rule</button></div></div>
    <div style="display:flex;flex-wrap:wrap;border-top:1px solid var(--line)">
      <div style="flex:1 1 300px;min-width:0;border-inline-end:1px solid var(--line)" role="list" aria-label="Rules in order">
        ${data.rules.map((r) => html`<div role="listitem" class="row-flex" style=${`padding:10px 14px;border-bottom:1px solid var(--line);flex-wrap:nowrap;cursor:pointer;${sel && sel.id === r.id ? 'background:var(--acc-soft)' : ''}`}
            onClick=${() => setSel(r)}>
          ${r.origin === 'yours' ? html`<span class="row-flex" style="flex-direction:column;gap:0">
            <button type="button" class="btn ghost icon sm" style="height:18px;min-height:18px" aria-label="Move up" onClick=${(ev) => { ev.stopPropagation(); move(r.id, -1); }}><${Icon} name="up" size="sm" /></button>
            <button type="button" class="btn ghost icon sm" style="height:18px;min-height:18px" aria-label="Move down" onClick=${(ev) => { ev.stopPropagation(); move(r.id, 1); }}><${Icon} name="down" size="sm" /></button></span>`
          : html`<span style="width:30px"></span>`}
          <div class="grow"><b style="font-weight:500">${r.entry.name || r.id}</b>
            ${data.matching === r.id && html` <span class="chip ok sm">matching now</span>`}${r.origin === 'yours' && html` <span class="chip sm">yours</span>`}
            <div class="small muted">${summary(r.entry)}</div></div>
          <span onClick=${(ev) => ev.stopPropagation()}><${Toggle} checked=${!r.disabled} label=${`Enable ${r.id}`} onChange=${(v) => call('rule_enable', r.id, v).then(() => reload(sel && sel.id))} /></span>
        </div>`)}
      </div>
      <div style="flex:2 1 420px;min-width:0">
        ${sel ? html`<${RuleEditor} item=${sel} onSaved=${(id) => reload(id)} onDeleted=${() => { setSel(null); reload(); }} />` : html`<div class="empty">Pick a rule.</div>`}
      </div>
    </div>
  </section>`;
}

function chips(o) {
  const out = [];
  const w = o.whisper || {};
  if (w.language) out.push(w.language);
  if (w.model) out.push(w.model);
  if (w.task === 'translate') out.push('translate');
  if (w.initial_prompt) out.push('prompt');
  if (o.clipboard && o.clipboard.auto_paste === false) out.push('copy only');
  if (o.hotkey && o.hotkey.recording_mode) out.push(o.hotkey.recording_mode.replace(/_/g, ' '));
  if (o.voice_commands && o.voice_commands.enabled === false) out.push('commands off');
  if (o.streaming && o.streaming.streaming_enabled === false) out.push('preview off');
  return out;
}

function openProfileEditor(item, reload) {
  const entry = item ? JSON.parse(JSON.stringify(item.entry)) : { description: '', overrides: { whisper: {} } };
  let name = item ? item.id : '';
  let text = JSON.stringify(entry.overrides || {}, null, 2);
  let desc = entry.description || '';
  openModal(() => html`<div class="modal wide"><div class="modal-head"><h2>${item ? `Edit profile “${item.id}”` : 'New profile'}</h2></div>
    <div class="modal-body">
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px">
        <label class="stack" style="gap:6px"><span class="eyebrow">Name</span><input class="inp" value=${name} disabled=${item && item.origin !== 'yours'} onInput=${(e) => { name = e.currentTarget.value; }} /></label>
        <label class="stack" style="gap:6px"><span class="eyebrow">Description</span><input class="inp" value=${desc} onInput=${(e) => { desc = e.currentTarget.value; }} /></label>
      </div>
      <label class="stack" style="gap:6px"><span class="eyebrow">Settings it changes</span>
        <textarea class="inp mono" rows="12" value=${text} onInput=${(e) => { text = e.currentTarget.value; }}></textarea></label>
      <p class="small muted" style="margin:0">Sections and keys as in Settings, for example {"whisper": {"language": "he", "model": "small"}, "clipboard": {"auto_paste": true}}.</p>
    </div>
    <div class="modal-foot"><button type="button" class="btn" onClick=${closeModal}>Cancel</button>
      <button type="button" class="btn pri" onClick=${() => {
        let overrides;
        try { overrides = JSON.parse(text || '{}'); } catch (err) { toast(`That isn't valid JSON: ${err.message}`, { kind: 'err' }); return; }
        call('profile_save', { description: desc, overrides }, name, item ? item.id : null)
          .then(() => { closeModal(); toast('Profile saved'); reload(); }).catch((err) => toast(err.message, { kind: 'err' }));
      }}>Save</button></div></div>`);
}

function Profiles() {
  const active = app.use((s) => s.profile);
  const [items, setItems] = useState([]);
  const reload = () => call('profiles_list').then((d) => { setItems(d.profiles); app.set({ profile: d.active }); }).catch(() => {});
  useEffect(() => { reload(); }, []);
  const use = (id) => call('profile_activate', id).then(() => { app.set({ profile: id }); toast(`Profile: ${id}`); }).catch((e) => toast(e.message, { kind: 'err' }));
  return html`<section class="card" aria-labelledby="h-prof">
    <div class="card-head"><div><h2 id="h-prof">Profiles</h2><p>One click swaps model, language, prompt and delivery. Also in the floating bar's quick actions.</p></div>
      <button type="button" class="btn" onClick=${() => openProfileEditor(null, reload)}><${Icon} name="plus" />New profile</button></div>
    <div style="padding:0 18px 18px;display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px">
      ${items.filter((p) => !p.disabled).map((p) => html`<article style=${`border:${p.id === active ? '2px solid var(--acc)' : '1px solid var(--line)'};border-radius:12px;padding:${p.id === active ? 13 : 14}px;display:flex;flex-direction:column;gap:8px`}>
        <div class="row-flex" style="justify-content:space-between"><b style="font-weight:600;text-transform:capitalize">${p.id}</b>
          ${p.id === active ? html`<span class="chip acc">Active</span>` : html`<button type="button" class="btn sm" onClick=${() => use(p.id)}>Use</button>`}</div>
        <p class="small muted" style="margin:0">${p.entry.description || ''}</p>
        <div class="row-flex">${chips(p.entry.overrides || {}).map((c) => html`<span class="chip">${c}</span>`)}</div>
        <div class="row-flex"><button type="button" class="btn ghost sm" onClick=${() => openProfileEditor(p, reload)}>Edit</button>
          ${p.origin === 'yours' && html`<button type="button" class="btn ghost sm" onClick=${() => call('profile_delete', p.id).then(reload)}>Delete</button>`}
          ${p.origin === 'changed' && html`<button type="button" class="btn ghost sm" onClick=${() => call('profile_delete', p.id).then(reload)}>Reset</button>`}
          ${p.origin !== 'yours' && p.id !== active && html`<button type="button" class="btn ghost sm" onClick=${() => call('profile_enable', p.id, false).then(reload)}>Hide</button>`}</div>
      </article>`)}
    </div>
    ${items.some((p) => p.disabled) && html`<div class="row-flex small muted" style="padding:0 18px 16px">Hidden: ${items.filter((p) => p.disabled).map((p) => html`<button type="button" class="btn ghost sm" onClick=${() => call('profile_enable', p.id, true).then(reload)}>Show ${p.id}</button>`)}</div>`}
  </section>`;
}

export function AppsScreen() {
  return html`<div class="page">
    <div><h1 class="page-title">Apps & profiles</h1><p class="page-sub">Behave differently per app, or switch a whole set of settings at once.</p></div>
    <${Rules} /><${Profiles} />
  </div>`;
}
