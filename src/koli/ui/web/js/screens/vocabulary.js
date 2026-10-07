// screens/vocabulary.js
// Vocabulary ("My words" in Simple mode): favored words Whisper leans toward,
// with suggestions from history and a folder scan; plus, in Advanced mode,
// corrections (one spelling, many mishearings), replacements (find/replace
// with options) and snippets (spoken shortcuts). Each edit applies to the
// next dictation.

import { html, useEffect, useState } from '../../vendor/preact-htm.module.js';
import { call } from '../api.js';
import { app, closeModal, openModal, toast, useSetting } from '../store.js';
import { Icon, TagInput, Toggle } from '../ui.js';

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
  return html`<section class="card" aria-labelledby="h-corr">
    <div class="card-head"><div><h2 id="h-corr">Corrections</h2><p>One right spelling, every way it gets misheard. The longest match wins.</p></div></div>
    <div class="tbl-wrap"><table class="tbl"><thead><tr><th scope="col" style="width:30%">Write it as</th><th scope="col">When Whisper hears</th><th scope="col"><span class="sr-only">Delete</span></th></tr></thead>
      <tbody>${rows.map(([canonical, variants]) => html`<tr>
        <td><b style="font-weight:500" dir="auto">${canonical}</b></td>
        <td><${TagInput} values=${variants} label=${`Mishearings of ${canonical}`} onChange=${(v) => save({ ...data, [canonical]: v })} /></td>
        <td class="end"><button type="button" class="btn ghost icon" aria-label=${`Delete ${canonical}`} onClick=${() => { const n = { ...data }; delete n[canonical]; save(n); }}><${Icon} name="x" /></button></td></tr>`)}
        <tr><td colspan="3"><div class="row-flex"><input class="inp" placeholder="Correct spelling, e.g. CAPEX" aria-label="New correction" dir="auto" value=${draft}
          onInput=${(e) => setDraft(e.currentTarget.value)} onKeyDown=${(e) => { if (e.key === 'Enter' && draft.trim()) { save({ ...data, [draft.trim()]: [] }); setDraft(''); } }} />
          <button type="button" class="btn" disabled=${!draft.trim()} onClick=${() => { save({ ...data, [draft.trim()]: [] }); setDraft(''); }}><${Icon} name="plus" />Add</button>
          <span class="small muted">Then add how it gets misheard.</span></div></td></tr>
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
          <td><input class=${'inp' + (it.regex ? ' mono' : '')} dir="auto" value=${it.from} aria-label="Find" style="width:100%" onBlur=${(e) => e.currentTarget.value !== it.from && update(i, { from: e.currentTarget.value })} /></td>
          <td><input class=${'inp' + (it.regex ? ' mono' : '')} dir="auto" value=${it.to} aria-label="Replace with" style="width:100%" onBlur=${(e) => e.currentTarget.value !== it.to && update(i, { to: e.currentTarget.value })} /></td>
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
          <td><input class="inp" dir="auto" value=${it.trigger} aria-label="Trigger" onBlur=${(e) => e.currentTarget.value !== it.trigger && update(i, { trigger: e.currentTarget.value })} /></td>
          <td><textarea class="inp" rows="2" dir="auto" aria-label="Expansion" value=${it.expansion} onBlur=${(e) => e.currentTarget.value !== it.expansion && update(i, { expansion: e.currentTarget.value })}></textarea></td>
          <td><div class="seg" role="radiogroup" aria-label="When it works">${[['anywhere', 'Anywhere'], ['alone', 'Alone']].map(([v, t]) => html`<button type="button" role="radio"
            aria-checked=${(it.mode || 'anywhere') === v} class=${(it.mode || 'anywhere') === v ? 'on' : ''} onClick=${() => update(i, { mode: v })}>${t}</button>`)}</div></td>
          <td class="end"><button type="button" class="btn ghost icon" aria-label="Delete" onClick=${() => save(items.filter((_, n) => n !== i))}><${Icon} name="x" /></button></td></tr>`)}
      </tbody></table></div>
  </section>`;
}

export function VocabularyScreen() {
  const mode = useSetting('ui.mode');
  return html`<div class=${'page' + (mode === 'advanced' ? '' : ' narrow')}>
    <div><h1 class="page-title">${mode === 'advanced' ? 'Vocabulary' : 'My words'}</h1>
      <p class="page-sub">Teach it your names and terms. Every change applies to the next dictation.</p></div>
    <${Words} />
    ${mode === 'advanced' && html`<${Corrections} /><${Replacements} /><${Snippets} />`}
    ${mode !== 'advanced' && html`<${Snippets} />`}
  </div>`;
}
