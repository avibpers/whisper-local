// screens/templates.js
// Summary templates (Meetings › Summary templates): the list the AI writes
// meeting summaries from, the default one, and an editor for the whole file
// (a YAML header for the layout, then the instructions the AI follows). New
// templates come from a description, from past summaries, or as a copy; the
// AI drafts the first two and nothing is saved until Save.
// Design: docs/design/meeting-summary.md.

import { html, useEffect, useState } from '../../vendor/preact-htm.module.js';
import { call } from '../api.js';
import { confirmModal, toast } from '../store.js';
import { Icon, Select } from '../ui.js';

const ORIGIN = { 'built-in': ['', 'Built in'], skill: ['acc', 'From the Claude skill'], mine: ['ok', 'Mine'] };
const BLOCK_HELP = 'header, tldr, participants, agenda, agenda_details, decisions, action_items, lists, open_questions, parked, '
  + 'next_meeting, speakers, screenshots, topics, sections, notes, transcript, data, quick, page_break';

// One template in the list, with what it can do.
function Row({ t, onEdit, onCopy, onDefault, onDelete, onView }) {
  const [tone, origin] = ORIGIN[t.origin] || ['', t.origin];
  return html`<article class="card pad stack" style="gap:8px">
    <div class="row-flex" style="justify-content:space-between;align-items:flex-start">
      <div style="min-width:0"><div class="card-title">${t.name}
        ${t.default && html` <span class="chip acc">Default</span>`}</div>
        <div class="small muted">${t.description}</div></div>
      <span class=${'chip ' + tone}>${origin}${t.overrides_builtin ? ' · replaces a built-in' : ''}</span></div>
    <div class="tags">${t.sections.map((s) => html`<span class="tag">${s}</span>`)}</div>
    <div class="small muted">Language: ${t.language === 'meeting' ? "the meeting's" : t.language}</div>
    <div class="row-flex">
      ${!t.default && html`<button type="button" class="btn sm" onClick=${() => onDefault(t)}>Make default</button>`}
      ${t.editable ? html`<button type="button" class="btn sm" onClick=${() => onEdit(t)}><${Icon} name="pen" />Edit</button>`
        : html`<button type="button" class="btn sm ghost" onClick=${() => onView(t)}>View</button>`}
      <button type="button" class="btn sm ghost" onClick=${() => onCopy(t)}><${Icon} name="copy" />Duplicate</button>
      ${t.editable && html`<button type="button" class="btn sm ghost danger" onClick=${() => onDelete(t)}><${Icon} name="trash" />Delete</button>`}
    </div></article>`;
}

// The whole template file in one editor; checked on save.
function Editor({ draft, onDone }) {
  const [text, setText] = useState(draft.text);
  const [error, setError] = useState(draft.problems && draft.problems.length ? draft.problems.join('; ') : '');
  const [busy, setBusy] = useState(false);
  const save = () => {
    setBusy(true);
    call('summary_template_save', text, draft.replaces || '').then((row) => { toast(`Saved "${row.name}"`); onDone(row); })
      .catch((e) => setError(e.message)).finally(() => setBusy(false));
  };
  return html`<section class="card pad stack" aria-label="Template editor">
    <div class="row-flex" style="justify-content:space-between">
      <span class="card-title">${draft.title}</span>
      <div class="row-flex">
        <button type="button" class="btn ghost" onClick=${() => onDone(null)}>${draft.readOnly ? 'Close' : 'Cancel'}</button>
        ${!draft.readOnly && html`<button type="button" class="btn pri" disabled=${busy} onClick=${save}>Save</button>`}</div></div>
    <p class="small muted" style="margin:0">The part between the two <code>---</code> lines sets the layout: <code>id</code>, <code>name</code>,
      <code>language</code> (<code>meeting</code> or a code like <code>he</code>), the <code>title</code>, <code>date</code> or
      <code>datetime</code>, and <code>blocks</code>, the sections in order (${BLOCK_HELP}). Below it are the instructions the AI follows;
      <code>{{common_rules}}</code> adds the shared rules.</p>
    ${draft.readOnly && html`<p class="small" style="margin:0">Built-in templates can't be changed. Duplicate one to make your own version.</p>`}
    ${error && html`<div class="field-err" role="alert">${error}</div>`}
    <textarea class="inp mono" rows="26" dir="ltr" spellcheck="false" aria-label="Template file" readOnly=${!!draft.readOnly}
      value=${text} onInput=${(e) => { setText(e.currentTarget.value); setError(''); }}></textarea>
  </section>`;
}

// A new template: described, learnt from past summaries, or copied.
function NewTemplate({ templates, onDraft, onCancel }) {
  const [kind, setKind] = useState('describe');
  const [text, setText] = useState('');
  const [name, setName] = useState('');
  const [source, setSource] = useState('standard');
  const [files, setFiles] = useState([]);
  const [busy, setBusy] = useState(false);
  const write = () => {
    setBusy(true);
    call('summary_template_draft', kind, text, name).then((d) => onDraft({ ...d, title: 'New template, written by the AI' }))
      .catch((e) => toast(e.message, { kind: 'err' })).finally(() => setBusy(false));
  };
  const copy = () => call('summary_template_copy', source, name).then((d) => onDraft({ ...d, title: 'New template' }))
    .catch((e) => toast(e.message, { kind: 'err' }));
  const open = () => call('summary_template_read_files').then((r) => {
    if (!r) return;
    setFiles((f) => [...f, ...r.names]);
    setText((t) => (t ? `${t}\n\n${r.text}` : r.text));
  }).catch((e) => toast(e.message, { kind: 'err' }));
  return html`<section class="card pad stack" aria-label="New template">
    <div class="row-flex" style="justify-content:space-between"><span class="card-title">New template</span>
      <button type="button" class="btn ghost" onClick=${onCancel}>Cancel</button></div>
    <div class="seg" role="tablist" aria-label="How to make it">
      ${[['describe', 'Describe it'], ['examples', 'From past summaries'], ['copy', 'Copy of a template']].map(([k, label]) => html`
        <button type="button" role="tab" aria-selected=${kind === k} class=${kind === k ? 'on' : ''} onClick=${() => setKind(k)}>${label}</button>`)}</div>
    <label class="stack" style="gap:6px"><span class="eyebrow">Name</span>
      <input class="inp" type="text" value=${name} placeholder="Weekly team sync" onInput=${(e) => setName(e.currentTarget.value)} /></label>
    ${kind === 'describe' && html`<label class="stack" style="gap:6px"><span class="eyebrow">What should the summary hold?</span>
      <textarea class="inp" rows="9" dir="auto" value=${text} onInput=${(e) => setText(e.currentTarget.value)}
        placeholder=${'For example:\nFixed title: Board Meeting Summary\nDate (no time)\nParticipants\nMeeting Agenda\nFor each agenda item: the discussion, decisions made, action items\nPoints for the next meeting, only if something was left open'}></textarea></label>`}
    ${kind === 'examples' && html`<div class="stack" style="gap:6px"><span class="eyebrow">Past summaries you like</span>
      <span class="small muted">Paste one or more, or open files (.md, .txt, .html, or a Koli summary .json). The AI works out what they share:
        headings and their order, length, tone, how tasks are written.</span>
      <div class="row-flex"><button type="button" class="btn sm" onClick=${open}><${Icon} name="folder" />Open files…</button>
        ${files.length > 0 && html`<span class="small muted">${files.join(', ')}</span>`}</div>
      <textarea class="inp" rows="12" dir="auto" value=${text} onInput=${(e) => setText(e.currentTarget.value)}
        placeholder="Paste summaries here, one after another"></textarea></div>`}
    ${kind === 'copy' && html`<label class="stack" style="gap:6px"><span class="eyebrow">Start from</span>
      <${Select} label="Start from" value=${source} options=${templates.map((t) => [t.id, t.name])} onChange=${setSource} /></label>`}
    <div class="row-flex">
      ${kind === 'copy' ? html`<button type="button" class="btn pri" onClick=${copy}>Open in the editor</button>`
        : html`<button type="button" class="btn pri" disabled=${busy || !text.trim()} onClick=${write}>
          ${busy ? html`<span class="spin" aria-hidden="true"></span>Writing the template…` : 'Write it with the AI'}</button>`}
      ${kind !== 'copy' && html`<span class="small muted">You review it in the editor before it is saved.</span>`}
    </div></section>`;
}

export function TemplatesView({ onBack }) {
  const [list, setList] = useState([]);
  const [draft, setDraft] = useState(null);
  const [making, setMaking] = useState(false);
  const reload = () => call('summary_templates').then(setList).catch((e) => toast(e.message, { kind: 'err' }));
  useEffect(() => { reload(); }, []);
  const edit = (t, readOnly) => call('summary_template', t.id)
    .then((d) => setDraft({ text: d.text, replaces: readOnly ? '' : t.id, readOnly, title: readOnly ? t.name : `Edit "${t.name}"` }))
    .catch((e) => toast(e.message, { kind: 'err' }));
  const copy = (t) => call('summary_template_copy', t.id).then((d) => setDraft({ ...d, title: `Copy of "${t.name}"` }))
    .catch((e) => toast(e.message, { kind: 'err' }));
  const makeDefault = (t) => call('summary_template_default', t.id).then(() => { toast(`"${t.name}" is the default now`); reload(); })
    .catch((e) => toast(e.message, { kind: 'err' }));
  const remove = async (t) => {
    if (await confirmModal({ title: `Delete "${t.name}"?`, body: 'The template file is removed. Summaries already written with it stay.',
      confirm: 'Delete', danger: true })) {
      call('summary_template_delete', t.id).then(() => { toast('Template deleted'); reload(); }).catch((e) => toast(e.message, { kind: 'err' }));
    }
  };
  const done = () => { setDraft(null); setMaking(false); reload(); };
  return html`<div class="page wide">
    <div class="page-head">
      <div><button type="button" class="btn ghost sm" onClick=${onBack}>← Meetings</button>
        <h1 class="page-title">Summary templates</h1>
        <p class="page-sub">The AI writes each meeting's summary from a template: which sections, in which order, with which headings.
          The default one is used when a meeting ends.</p></div>
      ${!draft && !making && html`<button type="button" class="btn pri" onClick=${() => setMaking(true)}><${Icon} name="plus" />New template</button>`}
    </div>
    ${draft && html`<${Editor} draft=${draft} onDone=${done} />`}
    ${!draft && making && html`<${NewTemplate} templates=${list} onDraft=${(d) => { setMaking(false); setDraft(d); }} onCancel=${() => setMaking(false)} />`}
    ${!draft && !making && html`<div class="stack">${list.map((t) => html`<${Row} t=${t} onEdit=${(x) => edit(x, false)} onView=${(x) => edit(x, true)}
      onCopy=${copy} onDefault=${makeDefault} onDelete=${remove} />`)}</div>`}
  </div>`;
}
