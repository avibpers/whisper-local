// screens/practice.js
// Practice: teach Koli your voice. Three views: the intake (your languages,
// accent, work and words, and what will be sent to the AI model), the round
// (read each sentence, then a paragraph, and see what Whisper wrote, word by
// word), and the results (findings to keep as Learned vocabulary). The work
// happens in services/practice.py; recording a reading blocks until you pause.

import { html, useEffect, useState } from '../../vendor/preact-htm.module.js';
import { call } from '../api.js';
import { app, navigate, toast } from '../store.js';
import { ChoiceTags, Icon, Select, TagInput } from '../ui.js';
import { KindBadge } from './shared.js';

const STATUS_STYLE = {
  right: '',
  unsure: 'text-decoration:underline dotted var(--warn);text-underline-offset:4px',
  wrong: 'color:var(--rec);text-decoration:line-through',
  missing: 'color:var(--mute);text-decoration:line-through',
};
const KIND_LABEL = { favored: 'Favored words', correction: 'Corrections', prompt: 'Profile prompt terms' };

const languageChoices = () => {
  const schema = app.get().schema;
  const s = schema && schema.settings.find((x) => x.path === 'whisper.languages');
  return (s && s.choices) || [['en', 'English'], ['he', 'Hebrew · עברית']];
};
const languageName = (code) => (languageChoices().find(([c]) => c === code) || [code, code])[1];
const pct = (x) => `${Math.round((x || 0) * 100)}%`;

// ── Intake ──────────────────────────────────────────────────────────────────

function Intake({ state, onStart, busy }) {
  const [form, setForm] = useState(state.intake);
  const [showTerms, setShowTerms] = useState(false);
  const set = (k, v) => setForm({ ...form, [k]: v });
  const model = state.model;
  return html`<section class="card" aria-labelledby="h-intake">
    <div class="card-head"><div><h2 id="h-intake">About you</h2>
      <p>Koli picks what you read from this. It takes half a minute, once; you can change it any time.</p></div></div>
    <div class="srow top"><div class="slabel"><b>Languages you dictate in</b></div>
      <div class="sctl wide"><${ChoiceTags} values=${form.languages} options=${languageChoices()} label="Languages you dictate in"
        onChange=${(v) => set('languages', v)} /></div></div>
    <div class="srow"><div class="slabel"><b>Your native language or accent</b>
      <small>For example “Hebrew; English with an Israeli accent”. Koli practises the sounds that get mixed up.</small></div>
      <div class="sctl"><input class="inp" dir="auto" style="width:min(360px,100%)" value=${form.accent} aria-label="Native language or accent"
        onInput=${(e) => set('accent', e.currentTarget.value)} /></div></div>
    <div class="srow"><div class="slabel"><b>What you do</b><small>The texts use the words of your work.</small></div>
      <div class="sctl" style="flex-wrap:wrap;justify-content:flex-end">
        <${Select} label="What you do" value=${form.work} options=${state.domains} onChange=${(v) => set('work', v)} />
        ${form.work === 'other' && html`<input class="inp" dir="auto" placeholder="Your field" aria-label="Your field" value=${form.work_other}
          onInput=${(e) => set('work_other', e.currentTarget.value)} />`}</div></div>
    <div class="srow top"><div class="slabel"><b>Words Koli gets wrong</b><small>Optional. They go into the texts.</small></div>
      <div class="sctl wide"><${TagInput} values=${form.words} label="Words Koli gets wrong" placeholder="Add a word, press Enter"
        onChange=${(v) => set('words', v)} /></div></div>
    <div class="srow top"><div class="slabel"><b>Who writes the texts</b>
      <small>${model ? 'The AI model writes texts for you and analyses how Whisper heard you.'
        : 'No AI model is chosen, so Koli uses its built-in texts (English and Hebrew) and its own word comparison.'}</small></div>
      <div class="sctl" style="flex-direction:column;align-items:flex-end;gap:6px">
        ${model ? html`<span class="row-flex"><span class="chip mono">${model}</span><${KindBadge} kind=${state.model_kind} /></span>`
          : html`<span class="chip">Built-in texts</span>`}
        <a class="small" href="#/settings/ai">Change in Settings › AI</a></div></div>
    ${state.terms.length > 0 && html`<div class="srow top"><div class="slabel"><b>Your words from Koli</b>
      <small>${model ? `These ${state.terms.length} terms are sent to the AI model with your answers. Never your dictations.`
        : `${state.terms.length} terms from your history and corrections go into the texts.`}</small></div>
      <div class="sctl wide">${showTerms ? html`<div class="tags">${state.terms.map((t) => html`<span class="chip sm" dir="auto" title=${t.from}>${t.term}</span>`)}</div>`
        : html`<button type="button" class="btn ghost sm" onClick=${() => setShowTerms(true)}>Show them</button>`}</div></div>`}
    ${!state.word_confidence && html`<div class="notice warn" style="margin:0 18px 12px"><${Icon} name="alert" />
      <span>The whisper.cpp engine gives no confidence per word, so “unsure” words can't be found. Everything else works.</span></div>`}
    <div class="row-flex" style="justify-content:flex-end;padding:0 18px 18px">
      <button type="button" class="btn pri" disabled=${busy || !form.languages.length} onClick=${() => onStart(form)}>
        <${Icon} name="mic" />${busy ? (model ? 'Writing your texts…' : 'Getting ready…') : 'Start practising'}</button></div>
  </section>`;
}

// ── One reading ─────────────────────────────────────────────────────────────

function HeardWords({ comparison }) {
  return html`<p dir="auto" style="font-size:18px;line-height:1.8;margin:0">
    ${comparison.words.map((w) => html`<span style=${STATUS_STYLE[w.status]} title=${w.status === 'wrong' ? `Heard: ${w.heard}` : w.status}>${w.text}</span>${
      w.status === 'wrong' && w.heard ? html` <span class="chip sm rec" dir="auto">${w.heard}</span>` : ''} `)}
    ${comparison.extra.length > 0 && html`<span class="small muted"> (also heard: <span dir="auto">${comparison.extra.join(' ')}</span>)</span>`}</p>`;
}

function Reading({ round, index, onRecorded, onNext, busy }) {
  const item = round.items[index];
  const [listening, setListening] = useState(false);
  const stageItems = round.items.filter((i) => i.stage === item.stage && i.kind === item.kind);
  const position = stageItems.indexOf(item) + 1 + (item.kind === 'sentence' && item.stage === 'second'
    ? round.items.filter((i) => i.stage === 'first').length : 0);
  const sentenceTotal = round.planned || round.items.filter((i) => i.kind === 'sentence').length;
  const record = () => {
    if (listening || busy) return;
    setListening(true);
    call('practice_record', index).then(onRecorded).catch((e) => toast(e.message, { kind: 'err' })).finally(() => setListening(false));
  };
  useEffect(() => {
    const onKey = (e) => { if (e.code === 'Space' && !e.repeat && e.target === document.body) { e.preventDefault(); record(); } };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });
  // A misspelled word in the text would teach Koli the misspelling: this
  // reading then counts for nothing.
  const flag = () => call('practice_flag', index).then(() => { toast('Thanks. Nothing is learned from this text.'); onNext(); })
    .catch((e) => toast(e.message, { kind: 'err' }));
  const r = item.result;
  return html`<section class="card" aria-labelledby="h-read" style="padding:22px;display:flex;flex-direction:column;gap:16px">
    <div class="row-flex" style="justify-content:space-between">
      <span class="eyebrow" id="h-read">${item.kind === 'paragraph' ? 'Now in context: read the paragraph' : `Sentence ${position} of ${sentenceTotal}`}</span>
      <span class="chip sm"><bdi>${languageName(item.language)}</bdi></span></div>
    <p dir="auto" style="font-size:22px;line-height:1.6;margin:0;font-weight:500">${item.text}</p>
    ${listening ? html`<div class="row-flex"><span class="chip rec"><span class="dot"></span>Listening… read it now</span>
        <button type="button" class="btn" onClick=${() => call('practice_stop')}>Stop</button>
        <span class="small muted">It stops by itself when you pause.</span></div>`
      : !r && html`<div class="row-flex"><button type="button" class="btn pri" onClick=${record} disabled=${busy}><${Icon} name="mic" />Record</button>
        <span class="small muted">or press Space</span>
        <button type="button" class="btn ghost" onClick=${() => call('practice_skip', index).then(onNext)}>Skip</button>
        <button type="button" class="btn ghost" onClick=${flag}>The text has a mistake</button></div>`}
    ${r && !listening && html`<div class="stack" style="gap:10px;border-top:1px solid var(--line);padding-top:14px">
      <div class="row-flex" style="justify-content:space-between"><span class="eyebrow">What Whisper wrote</span>
        <span class=${'chip sm ' + (r.comparison.accuracy >= 0.95 ? 'ok' : r.comparison.accuracy >= 0.8 ? 'warn' : 'rec')}>${pct(r.comparison.accuracy)} right</span></div>
      <${HeardWords} comparison=${r.comparison} />
      ${r.heard_bare && r.heard_bare !== r.heard && html`<p class="small muted" style="margin:0" dir="auto">Without your vocabulary: ${r.heard_bare}</p>`}
      <div class="row-flex"><button type="button" class="btn" onClick=${record} disabled=${busy}><${Icon} name="refresh" />Again</button>
        <button type="button" class="btn pri" onClick=${onNext} disabled=${busy}>${busy ? 'One moment…' : 'Next'}</button>
        <button type="button" class="btn ghost" onClick=${flag} disabled=${busy}>The text has a mistake</button></div></div>`}
  </section>`;
}

// ── Results ─────────────────────────────────────────────────────────────────

function Results({ results, onApply, busy }) {
  const [picked, setPicked] = useState(() => new Set(results.findings.map((f) => f.id)));
  const toggle = (id) => { const n = new Set(picked); if (n.has(id)) n.delete(id); else n.add(id); setPicked(n); };
  const groups = ['correction', 'favored', 'prompt'].map((k) => [k, results.findings.filter((f) => f.kind === k)]).filter(([, l]) => l.length);
  const describe = (f) => (f.kind === 'correction' ? html`<span><bdi class="mono">${f.heard.join(', ')}</bdi> → <span style="font-weight:600"><bdi>${f.term}</bdi></span></span>`
    : html`<b><bdi>${f.term}</bdi></b>${f.profile ? html` <span class="chip sm">${f.profile}</span>` : ''}`);
  return html`<section class="card" aria-labelledby="h-res">
    <div class="card-head"><div><h2 id="h-res">What Koli learned</h2>
      <p>Everything is checked; uncheck what you don't want. Apply adds the rest to My words › Learned.</p></div></div>
    <div class="row-flex" style="padding:0 18px 12px;gap:8px;flex-wrap:wrap">
      ${Object.entries(results.accuracy).map(([l, a]) => html`<span class="chip"><bdi>${languageName(l)}</bdi>: ${pct(a)} heard right</span>`)}</div>
    ${results.context.length > 0 && html`<div style="padding:0 18px 12px"><span class="eyebrow">In context</span>
      <ul class="small" style="margin:6px 0 0;padding-inline-start:18px">${results.context.map((c) => html`<li>
        <b><bdi>${c.word}</bdi></b>: ${c.effect === 'fixed' ? 'wrong alone, right in the paragraph' : 'right alone, changed in the paragraph'}</li>`)}</ul></div>`}
    ${Object.entries(results.notes || {}).map(([l, note]) => html`<div class="notice info" style="margin:0 18px 12px"><${Icon} name="mic" />
      <span><b>How you speak (<bdi>${languageName(l)}</bdi>)</b><br /><span dir="auto">${note}</span></span></div>`)}
    ${groups.length === 0 ? html`<div class="empty">Nothing new to add: Whisper heard you well, or you already have these words.</div>`
      : groups.map(([kind, list]) => html`<div class="subhead"><div class="eyebrow">${KIND_LABEL[kind]}</div></div>
        ${list.map((f) => html`<label class="srow" style="cursor:pointer"><div class="slabel">${describe(f)}
          <small><bdi>${languageName(f.language)}</bdi> · ${f.reason}${f.source === 'ai' ? ' · AI' : ''}</small></div>
          <div class="sctl"><input type="checkbox" class="sw" checked=${picked.has(f.id)} aria-label=${f.term} onChange=${() => toggle(f.id)} /></div></label>`)}`)}
    <div class="row-flex" style="justify-content:flex-end;padding:14px 18px 18px">
      <button type="button" class="btn pri" disabled=${busy} onClick=${() => onApply([...picked])}>
        <${Icon} name="check" />${picked.size ? `Apply ${picked.size}` : 'Finish'}</button></div>
  </section>`;
}

// ── The screen ──────────────────────────────────────────────────────────────

export function PracticeScreen() {
  const [state, setState] = useState(null);
  const [round, setRound] = useState(null);
  const [index, setIndex] = useState(0);
  const [results, setResults] = useState(null);
  const [busy, setBusy] = useState(false);
  const fail = (e) => toast(e.message, { kind: 'err' });
  useEffect(() => {
    call('practice_state').then((s) => { setState(s); if (s.round) setRound(s.round); }).catch(fail);
    // Leaving the screen any way at all ends the round, so hotkeys never stay
    // paused behind your back (closing twice is harmless).
    return () => { call('practice_close').catch(() => {}); };
  }, []);

  const firstOpen = (r) => r.items.findIndex((i) => i.stage === r.stage && !i.result && !i.skipped && !i.flagged);
  const show = (r) => { setRound(r); const i = firstOpen(r); if (i >= 0) setIndex(i); };
  const start = (form) => {
    setBusy(true);
    call('practice_save_intake', form).then(() => call('practice_start')).then(show).catch(fail).finally(() => setBusy(false));
  };
  // After a reading: the next one in this stage, else the next stage (the
  // model may be writing it), else the results.
  const next = async () => {
    setBusy(true);
    try {
      let r = (await call('practice_state')).round;
      if (firstOpen(r) < 0) r = await call('practice_next');
      if (r.stage === 'results') { const res = await call('practice_results'); setRound(res.round); setResults(res); }
      else show(r);
    } catch (e) { fail(e); } finally { setBusy(false); }
  };
  const recorded = (item) => setRound((r) => ({ ...r, items: r.items.map((i) => (i.index === item.index ? item : i)) }));
  const apply = (ids) => {
    setBusy(true);
    call('practice_apply', ids).then((n) => { toast(n ? `Added ${n} to My words › Learned` : 'Practice saved'); navigate('vocabulary/learned'); })
      .catch(fail).finally(() => setBusy(false));
  };
  const close = () => call('practice_close').finally(() => navigate('vocabulary'));

  return html`<div class="page narrow">
    <div class="page-head"><div><h1 class="page-title">Practice</h1>
      <p class="page-sub">Read a few short texts aloud. Koli compares what it heard with what you read and learns your words and your accent. About 3 minutes.</p></div>
      ${round && html`<button type="button" class="btn ghost" onClick=${close}><${Icon} name="x" />Close</button>`}</div>
    ${round && round.notes.length > 0 && html`<div class="notice warn"><${Icon} name="alert" /><span>${round.notes[round.notes.length - 1]}</span></div>`}
    ${!state ? html`<div class="empty">Loading…</div>`
      : results ? html`<${Results} results=${results} onApply=${apply} busy=${busy} />`
      : round && round.items[index] ? html`<${Reading} key=${index} round=${round} index=${index} onRecorded=${recorded} onNext=${next} busy=${busy} />`
      : html`<${Intake} state=${state} onStart=${start} busy=${busy} />`}
  </div>`;
}
