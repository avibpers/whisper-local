// screens/vocabulary.js
// Vocabulary ("My words" in Simple mode): favored words Whisper leans toward,
// with suggestions from history and a folder scan; corrections (one spelling,
// many mishearings, typed or recorded: "when I say this, I mean this") and
// snippets (spoken shortcuts); plus, in Advanced mode, replacements
// (find/replace with options). Each edit applies to the next dictation.
// Two tabs: Mine (all of the above, typed by you) and Learned (what Practice
// added, with the "how you speak" notes).

import { html, useEffect, useState } from '../../vendor/preact-htm.module.js';
import { call } from '../api.js';
import { app, closeModal, confirmModal, navigate, openModal, toast, useSetting } from '../store.js';
import { Icon, Select, TagInput, TextArea, TextField, Toggle } from '../ui.js';

function Words() {
  const words = useSetting('whisper.hotwords') || [];
  const [suggestions, setSuggestions] = useState([]);
  useEffect(() => { call('history_suggest').then((s) => setSuggestions(s.slice(0, 8))).catch(() => {}); }, [words.length]);
  const save = (list) => call('settings_set', 'whisper.hotwords', list).then((r) => app.set((s) => ({ settings: { ...s.settings, whisper: { ...s.settings.whisper, hotwords: r.value } } })))
    .catch((e) => toast(e.message, { kind: 'err' }));
  const scan = async () => {
    const res = await call('vocab_scan');
    if (!res) return;
    const picked = new Set(res.candidates.slice(0, 20).map((c) => c.word));
    openModal(() => html`<div class="modal wide"><div class="modal-head"><h2>Words found in ${res.files} files</h2></div>
      <div class="modal-body"><p class="muted" style="margin:0">Pick the names and terms to favor.</p>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:6px 18px;max-height:50vh;overflow:auto">
        ${res.candidates.map((c) => html`<label class="row-flex" style="justify-content:space-between"><span dir="auto">${c.word} <span class="muted small">${c.count}×</span></span>
          <input type="checkbox" class="sw" checked=${picked.has(c.word)} aria-label=${c.word} onChange=${(e) => (e.currentTarget.checked ? picked.add(c.word) : picked.delete(c.word))} /></label>`)}</div></div>
      <div class="modal-foot"><button type="button" class="btn" onClick=${closeModal}>Cancel</button>
        <button type="button" class="btn pri" onClick=${() => { closeModal(); call('vocab_add', [...picked]).then((list) => { toast(`Added ${picked.size} words`); save(list); }); }}>Add selected</button></div></div>`);
  };
  return html`<section class="card" aria-labelledby="h-words">
    <div class="card-head"><div><h2 id="h-words">Favored words</h2><p>Whisper leans toward these while listening. Best for names, products and jargon.</p></div>
      <div class="row-flex"><button type="button" class="btn" onClick=${() => scan().catch((e) => toast(e.message, { kind: 'err' }))}><${Icon} name="folder" />Import from a folder…</button></div></div>
    <div style="padding:0 18px 16px"><${TagInput} values=${words} label="Favored words" placeholder="Add a word, press Enter" onChange=${save} /></div>
    ${suggestions.length > 0 && html`<div class="row-flex" style="margin:0 18px 16px;padding:12px 14px;border-radius:10px;border:1px dashed var(--line2)">
      <span class="eyebrow">Suggested from your history</span>
      ${suggestions.map((s) => html`<button type="button" class="btn ghost sm" dir="auto" onClick=${() => save([...words, s.word])}>+ ${s.word} <span class="muted">· ${s.count}×</span></button>`)}</div>`}
  </section>`;
}

function Corrections() {
  const data = useSetting('postprocess.corrections') || {};
  const rows = Object.entries(data);
  const save = (next) => call('vocab_set_corrections', next).then((v) => app.set((s) => ({ settings: { ...s.settings, postprocess: { ...s.settings.postprocess, corrections: v } } })))
    .catch((e) => toast(e.message, { kind: 'err' }));
  const [draft, setDraft] = useState('');
  const [recording, setRecording] = useState(null);   // the term being recorded
  // Say it once: what Whisper writes becomes a mishearing of `canonical`.
  const record = (canonical) => {
    setRecording(canonical);
    return call('vocab_record_correction', canonical)
      .then((r) => {
        app.set((s) => ({ settings: { ...s.settings, postprocess: { ...s.settings.postprocess, corrections: r.corrections } } }));
        toast(r.already_right ? `Whisper already writes “${canonical}”` : r.added ? `Heard “${r.heard}” → now written “${canonical}”` : `“${r.variant}” is already there`);
        return true;
      })
      .catch((e) => { toast(e.message, { kind: 'err' }); return false; })
      .finally(() => setRecording(null));
  };
  const recordButton = (canonical, label) => html`<button type="button" class="btn ghost sm" disabled=${recording !== null}
    onClick=${() => record(canonical)}><${Icon} name="mic" />${recording === canonical ? 'Say it now…' : label}</button>`;
  return html`<section class="card" aria-labelledby="h-corr">
    <div class="card-head"><div><h2 id="h-corr">Corrections</h2><p>When you say this, it's written like this. Record yourself saying it, or type what Whisper writes instead. The longest match wins.</p></div></div>
    <div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col" style="width:30%">Write it as</th><th scope="col">When Whisper hears</th><th scope="col"><span class="sr-only">Delete</span></th></tr></thead>
      <tbody>${rows.map(([canonical, variants]) => html`<tr>
        <td><b style="font-weight:500" dir="auto">${canonical}</b></td>
        <td><${TagInput} values=${variants} label=${`Mishearings of ${canonical}`} onChange=${(v) => save({ ...data, [canonical]: v })} /></td>
        <td class="end">${recordButton(canonical, 'Record')}<button type="button" class="btn ghost icon" aria-label=${`Delete ${canonical}`} onClick=${() => { const n = { ...data }; delete n[canonical]; save(n); }}><${Icon} name="x" /></button></td></tr>`)}
        <tr><td colspan="3"><div class="row-flex"><input class="inp" placeholder="Correct spelling, e.g. CAPEX" aria-label="New correction" dir="auto" value=${draft}
          onInput=${(e) => setDraft(e.currentTarget.value)} onKeyDown=${(e) => { if (e.key === 'Enter' && draft.trim()) { save({ ...data, [draft.trim()]: [] }); setDraft(''); } }} />
          <button type="button" class="btn" disabled=${!draft.trim()} onClick=${() => { save({ ...data, [draft.trim()]: [] }); setDraft(''); }}><${Icon} name="plus" />Add</button>
          <button type="button" class="btn" disabled=${!draft.trim() || recording !== null}
            onClick=${() => record(draft.trim()).then((ok) => ok && setDraft(''))}><${Icon} name="mic" />${recording !== null && recording === draft.trim() ? 'Say it now…' : 'Record how you say it'}</button>
          <span class="small muted">Type how it should be written, then record yourself saying it.</span></div></td></tr>
      </tbody></table></div>
  </section>`;
}

function Replacements() {
  const items = useSetting('postprocess.replacements') || [];
  const save = (next) => call('vocab_set_replacements', next).then((v) => app.set((s) => ({ settings: { ...s.settings, postprocess: { ...s.settings.postprocess, replacements: v } } })))
    .catch((e) => toast(e.message, { kind: 'err' }));
  const update = (i, patch) => save(items.map((it, n) => (n === i ? { ...it, ...patch } : it)));
  return html`<section class="card" aria-labelledby="h-repl">
    <div class="card-head"><div><h2 id="h-repl">Replacements</h2><p>Exact find-and-replace with options. “Fix a word everywhere” adds rows here.</p></div>
      <button type="button" class="btn" onClick=${() => save([...items, { from: 'new text', to: '' }])}><${Icon} name="plus" />Add replacement</button></div>
    <div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col">Find</th><th scope="col">Replace with</th><th scope="col">Whole word</th><th scope="col">Match case</th><th scope="col">Pattern</th><th scope="col"><span class="sr-only">Delete</span></th></tr></thead>
      <tbody>
        ${items.length === 0 && html`<tr><td colspan="6" class="muted">None yet.</td></tr>`}
        ${items.map((it, i) => html`<tr>
          <td><${TextField} value=${it.from} label="Find" mono=${!!it.regex} width="100%" onCommit=${(v) => update(i, { from: v })} /></td>
          <td><${TextField} value=${it.to} label="Replace with" mono=${!!it.regex} width="100%" onCommit=${(v) => update(i, { to: v })} /></td>
          <td><${Toggle} checked=${it.whole_word !== false} label="Whole word" onChange=${(v) => update(i, { whole_word: v })} /></td>
          <td><${Toggle} checked=${!!it.case_sensitive} label="Match case" onChange=${(v) => update(i, { case_sensitive: v })} /></td>
          <td><${Toggle} checked=${!!it.regex} label="Regular expression" onChange=${(v) => update(i, { regex: v })} /></td>
          <td class="end"><button type="button" class="btn ghost icon" aria-label="Delete" onClick=${() => save(items.filter((_, n) => n !== i))}><${Icon} name="x" /></button></td></tr>`)}
      </tbody></table></div>
  </section>`;
}

function Snippets() {
  const items = useSetting('postprocess.snippets') || [];
  const save = (next) => call('vocab_set_snippets', next).then((v) => app.set((s) => ({ settings: { ...s.settings, postprocess: { ...s.settings.postprocess, snippets: v } } })))
    .catch((e) => toast(e.message, { kind: 'err' }));
  const update = (i, patch) => save(items.map((it, n) => (n === i ? { ...it, ...patch } : it)));
  return html`<section class="card" aria-labelledby="h-snip">
    <div class="card-head"><div><h2 id="h-snip">Snippets</h2><p>Say a trigger mid-dictation and the full text is typed. <span class="mono">${'${date}'}</span>, <span class="mono">${'${time}'}</span> and <span class="mono">${'${clipboard}'}</span> are filled in.</p></div>
      <button type="button" class="btn" onClick=${() => save([...items, { trigger: 'my signature', expansion: 'Best,\n' }])}><${Icon} name="plus" />Add snippet</button></div>
    <div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col">Say</th><th scope="col">Types</th><th scope="col">Works</th><th scope="col"><span class="sr-only">Delete</span></th></tr></thead>
      <tbody>
        ${items.length === 0 && html`<tr><td colspan="4" class="muted">None yet.</td></tr>`}
        ${items.map((it, i) => html`<tr>
          <td><${TextField} value=${it.trigger} label="Trigger" onCommit=${(v) => update(i, { trigger: v })} /></td>
          <td><${TextArea} value=${it.expansion} label="Expansion" rows=${2} onCommit=${(v) => update(i, { expansion: v })} /></td>
          <td><div class="seg" role="radiogroup" aria-label="When it works">${[['anywhere', 'Anywhere'], ['alone', 'Alone']].map(([v, t]) => html`<button type="button" role="radio"
            aria-checked=${(it.mode || 'anywhere') === v} class=${(it.mode || 'anywhere') === v ? 'on' : ''} onClick=${() => update(i, { mode: v })}>${t}</button>`)}</div></td>
          <td class="end"><button type="button" class="btn ghost icon" aria-label="Delete" onClick=${() => save(items.filter((_, n) => n !== i))}><${Icon} name="x" /></button></td></tr>`)}
      </tbody></table></div>
  </section>`;
}

// ── Learned (from Practice) ───────────────────────────────────────────────
// What practice rounds added, apart from what you typed: delete one, several
// or all. Each item applies to its language, on every microphone.

const KIND_NAMES = { favored: 'Favored word', correction: 'Correction', prompt: 'Prompt term' };

// "he" → "Hebrew · עברית", from the schema's language list.
function languageName(code) {
  const schema = app.get().schema;
  const s = schema && schema.settings.find((x) => x.path === 'whisper.languages');
  const hit = s && s.choices && s.choices.find(([c]) => c === code);
  return hit ? hit[1] : code;
}

function Learned() {
  const [data, setData] = useState(null);
  const [picked, setPicked] = useState(new Set());
  const [language, setLanguage] = useState('');
  const [mic, setMic] = useState('');
  const reload = () => call('learned_list').then((d) => { setData(d); setPicked(new Set()); }).catch((e) => toast(e.message, { kind: 'err' }));
  useEffect(() => { reload(); }, []);
  if (!data) return html`<div class="empty">Loading…</div>`;
  const shown = data.items.filter((i) => (!language || i.language === language) && (!mic || i.mic === mic));
  const languages = [...new Set(data.items.map((i) => i.language).filter(Boolean))];
  const mics = [...new Set(data.items.map((i) => i.mic).filter(Boolean))];
  const toggle = (id) => { const n = new Set(picked); if (n.has(id)) n.delete(id); else n.add(id); setPicked(n); };
  const allPicked = shown.length > 0 && shown.every((i) => picked.has(i.id));
  const remove = async (ids) => {
    if (!(await confirmModal({ title: `Delete ${ids.length} learned ${ids.length === 1 ? 'item' : 'items'}?`, body: 'The next dictation goes without them.', confirm: 'Delete', danger: true }))) return;
    call('learned_delete', ids).then((n) => { toast(`Deleted ${n}`); reload(); }).catch((e) => toast(e.message, { kind: 'err' }));
  };
  const removeAll = async () => {
    if (!(await confirmModal({ title: 'Start fresh?', body: 'Deletes everything practice taught Koli, the “how you speak” notes and the practice recordings. Your own words and your answers stay.', confirm: 'Delete all learned', danger: true }))) return;
    call('learned_delete_all').then(() => { toast('Learned vocabulary cleared'); reload(); }).catch((e) => toast(e.message, { kind: 'err' }));
  };
  const what = (i) => (i.kind === 'correction' ? html`<span><bdi class="mono">${i.heard.join(', ')}</bdi> → <b style="font-weight:500"><bdi>${i.term}</bdi></b></span>`
    : html`<b style="font-weight:500"><bdi>${i.term}</bdi></b>${i.profile ? html` <span class="chip sm">${i.profile}</span>` : ''}`);
  return html`
    ${Object.keys(data.notes).length > 0 && html`<section class="card" aria-labelledby="h-notes">
      <div class="card-head"><div><h2 id="h-notes">How you speak</h2><p>Written by the AI model after practice. Smart correction will use it.</p></div></div>
      ${Object.entries(data.notes).map(([l, note]) => html`<div class="srow"><div class="slabel"><b><bdi>${languageName(l)}</bdi></b><small dir="auto">${note}</small></div></div>`)}</section>`}
    <section class="card" aria-labelledby="h-learned">
      <div class="card-head"><div><h2 id="h-learned">Learned from practice</h2>
        <p>${data.items.length ? `${data.items.length} items from ${data.sessions.length} ${data.sessions.length === 1 ? 'round' : 'rounds'}. Each one applies to its language, on every microphone.`
          : 'Nothing yet. Practice reads a few texts with you and suggests words to add here.'}</p></div>
        ${data.items.length > 0 && html`<button type="button" class="btn danger" onClick=${removeAll}>Delete all…</button>`}</div>
      ${data.items.length > 0 && html`<div class="row-flex" style="padding:0 18px 12px;flex-wrap:wrap">
        <${Select} label="Language" value=${language} options=${[['', 'All languages'], ...languages.map((l) => [l, languageName(l)])]} onChange=${setLanguage} />
        ${mics.length > 1 && html`<${Select} label="Microphone" value=${mic} options=${[['', 'All microphones'], ...mics.map((m) => [m, m])]} onChange=${setMic} />`}
        <span class="grow"></span>
        ${picked.size > 0 && html`<button type="button" class="btn" onClick=${() => remove([...picked])}><${Icon} name="trash" />Delete ${picked.size}</button>`}</div>
      <div class="tbl-wrap"><table class="tbl"><thead><tr>
        <th scope="col" style="width:40px"><input type="checkbox" aria-label="Select all shown" checked=${allPicked}
          onChange=${() => setPicked(allPicked ? new Set() : new Set(shown.map((i) => i.id)))} /></th>
        <th scope="col">Word</th><th scope="col">Kind</th><th scope="col">Language</th><th scope="col">Microphone</th><th scope="col">Why</th>
        <th scope="col"><span class="sr-only">Delete</span></th></tr></thead>
        <tbody>${shown.map((i) => html`<tr>
          <td><input type="checkbox" aria-label=${`Select ${i.term}`} checked=${picked.has(i.id)} onChange=${() => toggle(i.id)} /></td>
          <td>${what(i)}</td><td class="small">${KIND_NAMES[i.kind] || i.kind}</td><td class="small"><bdi>${i.language ? languageName(i.language) : 'all'}</bdi></td>
          <td class="small muted">${i.mic}</td><td class="small muted">${i.reason}${i.source === 'ai' ? ' · AI' : ''}</td>
          <td class="end"><button type="button" class="btn ghost icon" aria-label=${`Delete ${i.term}`} onClick=${() => remove([i.id])}><${Icon} name="x" /></button></td></tr>`)}
        </tbody></table></div>`}
    </section>`;
}

export function VocabularyScreen({ route }) {
  const mode = useSetting('ui.mode');
  const tab = (route || '').split('/')[1] === 'learned' ? 'learned' : 'mine';
  return html`<div class=${'page' + (mode === 'advanced' ? '' : ' narrow')}>
    <div class="page-head"><div><h1 class="page-title">${mode === 'advanced' ? 'Vocabulary' : 'My words'}</h1>
      <p class="page-sub">Teach it your names and terms. Every change applies to the next dictation.</p></div>
      <button type="button" class="btn" onClick=${() => navigate('practice')}><${Icon} name="mic" />Practice</button></div>
    <div class="seg" role="tablist" aria-label="Whose words" style="align-self:flex-start">
      ${[['mine', 'Mine'], ['learned', 'Learned']].map(([id, label]) => html`<button type="button" role="tab" aria-selected=${tab === id}
        class=${tab === id ? 'on' : ''} onClick=${() => navigate(id === 'mine' ? 'vocabulary' : 'vocabulary/learned')}>${label}</button>`)}</div>
    ${tab === 'learned' ? html`<${Learned} />` : html`
      <${Words} />
      <${Corrections} />
      ${mode === 'advanced' && html`<${Replacements} />`}
      <${Snippets} />`}
  </div>`;
}
