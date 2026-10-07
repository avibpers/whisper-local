// screens/history.js
// History & insights: totals, words per day, where and in which languages you
// dictate, and the searchable list of past dictations with copy, fix-a-word,
// export and clear. Also suggests words to add from what you've said.

import { html, useEffect, useState } from '../../vendor/preact-htm.module.js';
import { call } from '../api.js';
import { app, confirmModal, openModal, closeModal, textDir, toast } from '../store.js';
import { Icon, Select } from '../ui.js';
import { fixAWord } from './live.js';

const LANG = { en: 'English', he: 'Hebrew', ar: 'Arabic', es: 'Spanish', fr: 'French', de: 'German', ru: 'Russian' };
const PAGE = 50;

function Stats({ data }) {
  const l = data.lifetime;
  const cards = [['Dictations', l.count.toLocaleString(), data.first ? `since ${data.first.slice(0, 10)}` : ''],
    ['Words', l.words.toLocaleString(), `avg. ${l.avg_words} per dictation`],
    ['Time saved', l.minutes_saved >= 60 ? `${(l.minutes_saved / 60).toFixed(1)} h` : `${Math.round(l.minutes_saved)} min`, 'vs. typing at 40 wpm'],
    ['Speaking pace', l.wpm ? `${l.wpm} wpm` : '—', l.wpm ? `${(l.wpm / 40).toFixed(1)}× typing` : ''],
    ['Streak', `${data.streak} day${data.streak === 1 ? '' : 's'}`, `longest ${data.longest_streak}`]];
  return html`<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px">
    ${cards.map(([t, v, s]) => html`<div class="card" style="padding:14px 16px"><div class="eyebrow">${t}</div>
      <div style="font-size:24px;font-weight:600;margin-top:4px">${v}</div><div class="small muted">${s}</div></div>`)}
  </div>`;
}

function Charts({ data }) {
  const max = Math.max(1, ...data.per_day.map((d) => d.words));
  return html`<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px">
    <section class="card pad" style="grid-column:span 2;min-width:0" aria-label="Words per day">
      <div class="row-flex" style="justify-content:space-between"><span class="card-title">Words per day</span><span class="small muted">Last ${data.per_day.length} days</span></div>
      <div style="height:140px;display:flex;align-items:flex-end;gap:8px;margin-top:14px;border-bottom:1px solid var(--line)">
        ${data.per_day.map((d) => html`<div style="flex:1;height:100%;display:flex;align-items:flex-end;justify-content:center">
          <span title=${`${d.date}: ${d.words} words`} style=${`width:100%;max-width:28px;border-radius:4px 4px 0 0;background:var(--acc);height:${Math.max(1, (d.words / max) * 100)}%`}></span></div>`)}
      </div>
      <div class="row-flex small muted" style="justify-content:space-between;margin-top:6px"><span>${data.per_day[0].date}</span><span>Today</span></div>
    </section>
    <section class="card pad stack" style="min-width:0" aria-label="Where you dictate">
      <span class="card-title">Where you dictate</span>
      ${data.apps.length === 0 && html`<span class="muted small">No data yet.</span>`}
      ${data.apps.map((a) => html`<div><div class="row-flex" style="justify-content:space-between"><span>${a.app}</span><span class="mono small muted">${Math.round(a.share * 100)}%</span></div>
        <div class="bar-meter" style="height:6px;margin-top:4px"><span style=${`transform:scaleX(${a.share});background:var(--acc)`}></span></div></div>`)}
      ${data.languages.length > 0 && html`<div class="row-flex">${data.languages.map((l) => html`<span class="chip">${LANG[l.code] || l.code} ${Math.round(l.share * 100)}%</span>`)}</div>`}
    </section>
  </div>`;
}

async function suggest() {
  const items = await call('history_suggest');
  if (!items.length) { toast('No suggestions yet — they come from names you say often.'); return; }
  const picked = new Set(items.map((i) => i.word));
  openModal(() => html`<div class="modal"><div class="modal-head"><h2>Words to add</h2></div>
    <div class="modal-body"><p class="muted" style="margin:0">Names and terms you say often. Whisper will favor the ones you keep.</p>
      ${items.map((i) => html`<label class="row-flex" style="justify-content:space-between"><span dir="auto">${i.word} <span class="muted small">· ${i.count}×</span></span>
        <input type="checkbox" class="sw" checked aria-label=${i.word} onChange=${(e) => (e.currentTarget.checked ? picked.add(i.word) : picked.delete(i.word))} /></label>`)}</div>
    <div class="modal-foot"><button type="button" class="btn" onClick=${closeModal}>Cancel</button>
      <button type="button" class="btn pri" onClick=${() => { closeModal(); call('vocab_add', [...picked]).then(() => toast(`Added ${picked.size} words`)); }}>Add selected</button></div></div>`);
}

export function HistoryScreen() {
  const [data, setData] = useState(null);
  const [list, setList] = useState({ items: [], total: 0, all: 0 });
  const [apps, setApps] = useState([]);
  const [q, setQ] = useState('');
  const [appFilter, setApp] = useState('');
  const [period, setPeriod] = useState('all');
  const [page, setPage] = useState(0);
  const pipeline = app.use((s) => s.pipeline);
  const mode = app.use((s) => s.settings.ui.mode);
  const delivered = pipeline.some((p) => p.stage === 'delivered');
  useEffect(() => { call('insights', 14).then(setData).catch(() => {}); call('history_apps').then(setApps).catch(() => {}); }, [delivered]);
  useEffect(() => {
    const t = setTimeout(() => call('history_query', q, appFilter, period, page * PAGE, PAGE).then(setList).catch(() => {}), 150);
    return () => clearTimeout(t);
  }, [q, appFilter, period, page, delivered]);
  const exportAs = (fmt) => call('history_export', fmt).then((p) => p && toast(`Exported to ${p}`)).catch((e) => toast(e.message, { kind: 'err' }));
  return html`<div class="page">
    <div class="page-head">
      <div><h1 class="page-title">${mode === 'advanced' ? 'History & insights' : 'History'}</h1>
        <p class="page-sub">Your last 2,000 dictations, searchable. Stored only on this computer.</p></div>
      <div class="row-flex">
        <button type="button" class="btn" onClick=${() => suggest().catch((e) => toast(e.message, { kind: 'err' }))}>Suggest words to add</button>
        <${Select} label="Export" value="" options=${[['', 'Export…'], ['txt', 'As .txt'], ['md', 'As .md'], ['csv', 'As .csv']]}
          onChange=${(v) => v && exportAs(v)} />
      </div>
    </div>
    ${data && html`<${Stats} data=${data} />`}
    ${data && mode === 'advanced' && html`<${Charts} data=${data} />`}
    <section class="card" style="min-width:0;overflow:hidden" aria-label="Transcripts">
      <div class="row-flex" style="padding:12px 18px">
        <label class="row-flex grow" style="flex:1 1 240px;height:36px;padding:0 10px;border:1px solid var(--line2);border-radius:9px;color:var(--mute);flex-wrap:nowrap">
          <${Icon} name="search" /><input aria-label="Search what you said" placeholder="Search what you said" dir="auto"
            style="flex:1;min-width:0;border:0;background:transparent;outline:none;color:var(--ink)" value=${q}
            onInput=${(e) => { setQ(e.currentTarget.value); setPage(0); }} /></label>
        <${Select} label="App" value=${appFilter} options=${[['', 'All apps'], ...apps.map((a) => [a, a])]} onChange=${(v) => { setApp(v); setPage(0); }} />
        <${Select} label="Period" value=${period} options=${[['all', 'Any time'], ['today', 'Today'], ['week', 'Last 7 days'], ['month', 'Last 30 days']]} onChange=${(v) => { setPeriod(v); setPage(0); }} />
      </div>
      ${list.items.length === 0 && html`<div class="empty">${list.all ? 'Nothing matches.' : 'No dictations yet.'}</div>`}
      ${list.items.map((it) => html`<div style="display:grid;grid-template-columns:110px minmax(0,1fr) auto;gap:6px 16px;padding:14px 18px;border-top:1px solid var(--line);align-items:start">
        <div class="small muted"><div>${(it.timestamp || '').replace('T', ' ').slice(0, 16)}</div><div>${it.app || '—'}${it.duration_s ? ` · ${it.duration_s} s` : ''}</div></div>
        <p dir=${textDir(it.text)} style="margin:0;white-space:pre-wrap">${it.text}</p>
        <div class="row-flex" style="flex-wrap:nowrap">
          <button type="button" class="btn ghost icon" aria-label="Copy" onClick=${() => call('copy_text', it.text).then(() => toast('Copied'))}><${Icon} name="copy" /></button>
          <button type="button" class="btn ghost sm" onClick=${() => fixAWord(it.text)}>Fix a word…</button></div>
      </div>`)}
      <div class="row-flex small muted" style="justify-content:space-between;padding:12px 18px;border-top:1px solid var(--line)">
        <span>${list.total ? `Showing ${page * PAGE + 1}–${Math.min(list.total, (page + 1) * PAGE)} of ${list.total.toLocaleString()}` : ''}</span>
        <span class="row-flex">
          <button type="button" class="btn sm" disabled=${page === 0} onClick=${() => setPage(page - 1)}>Newer</button>
          <button type="button" class="btn sm" disabled=${(page + 1) * PAGE >= list.total} onClick=${() => setPage(page + 1)}>Older</button>
          ${list.all > 0 && html`<button type="button" class="btn ghost sm danger" onClick=${async () => {
            if (await confirmModal({ title: 'Clear all history?', body: 'Deletes every saved dictation from this computer.', confirm: 'Clear', danger: true })) {
              call('history_clear').then(() => { setList({ items: [], total: 0, all: 0 }); toast('History cleared'); });
            }
          }}>Clear history</button>`}</span>
      </div>
    </section>
  </div>`;
}
