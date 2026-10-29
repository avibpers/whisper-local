// screens/usage.js
// Usage: what the online models cost. This session's spending (live, as each
// dictation, meeting chunk or AI answer comes back), the totals per model and
// per day so models can be compared, the calls of this session one by one,
// and the price list (usage/prices.yaml with your own prices on top), where a
// price can be changed. Design: docs/design/online-models.md.

import { html, useEffect, useState } from '../../vendor/preact-htm.module.js';
import { call } from '../api.js';
import { app, closeModal, openModal, toast, useSetting } from '../store.js';
import { Icon, Segmented } from '../ui.js';
import { money, providerName, SettingRow, usePriceList } from './shared.js';

// What each feature name in the ledger is called on screen.
const FEATURES = { dictation: 'Dictation', meeting: 'Meeting transcription', summary: 'Meeting summary', chat: 'Meeting chat',
  meetings: 'Meetings (AI)', polish: 'Polish', rewrite: 'Rewrite', practice: 'Practice', commands: 'AI actions' };
const featureName = (f) => FEATURES[f] || f;
const minutes = (s) => (s >= 60 ? `${(s / 60).toFixed(s >= 600 ? 0 : 1)} min` : `${Math.round(s)} s`);
const tokens = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n || 0));

// {feature: {model: row}} from many days, summed per model.
function byModel(days) {
  const out = {};
  for (const d of days) {
    for (const [feature, models] of Object.entries(d.by_feature)) {
      for (const [spec, row] of Object.entries(models)) {
        const m = out[spec] || (out[spec] = { spec, features: new Set(), calls: 0, seconds: 0, tokens: 0, cost: 0, unpriced: false });
        m.features.add(feature);
        m.calls += row.calls || 0;
        m.seconds += row.seconds || 0;
        m.tokens += (row.tokens_in || 0) + (row.tokens_out || 0) + (row.audio_tokens || 0);
        m.cost += row.cost || 0;
        m.unpriced = m.unpriced || !!row.unpriced;
      }
    }
  }
  return Object.values(out).sort((a, b) => b.cost - a.cost || b.calls - a.calls);
}

// ── The month's budget ──────────────────────────────────────────────────────
// What this month has cost against the limit (usage/budget.py), with the
// limit and what happens when it is used up, edited right here.

function Budget({ b }) {
  const schema = app.use((s) => s.schema);
  const rows = schema ? schema.settings.filter((s) => s.path.startsWith('usage.')) : [];
  const share = b && b.limit ? Math.min(1, b.spent / b.limit) : 0;
  return html`<section class="card" aria-labelledby="h-use-budget">
    <div class="card-head"><div><h2 id="h-use-budget">This month</h2>
      <p>${b && b.limit ? html`<b>${money(b.spent)}</b> of ${money(b.limit)} this month${b.over ? ' · used up' : ''}` : 'No limit set.'}</p></div></div>
    ${b && b.limit ? html`<div style="padding:0 18px 12px"><div class="bar-meter" role="meter" aria-label="Budget used" aria-valuemin="0" aria-valuemax="100" aria-valuenow=${Math.round(share * 100)}>
      <span style=${`transform:scaleX(${share});background:${share >= 1 ? 'var(--rec)' : share >= 0.8 ? 'var(--warn)' : 'var(--acc)'}`}></span></div></div>` : ''}
    ${rows.map((s) => html`<${SettingRow} s=${s} />`)}
  </section>`;
}

// ── Totals ──────────────────────────────────────────────────────────────────

function Totals({ summary, period }) {
  const session = app.use((s) => s.usage);
  const today = summary.days.find((d) => d.day === new Date().toLocaleDateString('sv'));
  const total = summary.days.reduce((n, d) => n + d.cost, 0);
  const sessionCost = Math.max(session.session_cost || 0, summary.session.cost || 0);
  return html`<section class="card pad" aria-label="Spending">
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:16px">
      ${[[money(sessionCost), 'this session'], [money(today ? today.cost : 0), 'today'], [money(total), `last ${period} days`]]
        .map(([v, l]) => html`<div><div class="stat">${v}</div><div class="small muted">${l}</div></div>`)}
    </div>
    <p class="small muted" style="margin:12px 0 0">Local Whisper, Ollama, LM Studio and the ChatGPT and SuperGrok subscriptions cost $0 here.
      Costs are worked out from the price list below and the tokens or minutes each answer reports.</p>
  </section>`;
}

function ModelTable({ days }) {
  const rows = byModel(days);
  return html`<section class="card" aria-labelledby="h-use-models">
    <div class="card-head"><div><h2 id="h-use-models">By model</h2><p>Compare what each model cost for the same kind of work.</p></div></div>
    ${rows.length ? html`<div class="tbl-wrap"><table class="tbl">
      <thead><tr><th>Model</th><th>Used for</th><th>Calls</th><th>Audio</th><th>Tokens</th><th>Cost</th></tr></thead>
      <tbody>${rows.map((r) => html`<tr>
        <td class="mono small">${r.spec}</td>
        <td class="small">${[...r.features].map(featureName).join(', ')}</td>
        <td>${r.calls}</td><td>${r.seconds ? minutes(r.seconds) : '—'}</td><td>${r.tokens ? tokens(r.tokens) : '—'}</td>
        <td><b>${money(r.cost)}</b>${r.unpriced ? html` <span class="chip sm warn" title="Some calls had no price">+ no price</span>` : ''}</td>
      </tr>`)}</tbody></table></div>`
      : html`<p class="muted" style="margin:0;padding:14px 18px">Nothing used yet in this period.</p>`}
  </section>`;
}

function DayTable({ days }) {
  if (!days.length) return null;
  return html`<section class="card" aria-labelledby="h-use-days">
    <div class="card-head"><div><h2 id="h-use-days">Day by day</h2></div></div>
    <div class="tbl-wrap"><table class="tbl">
      <thead><tr><th>Day</th><th>Models</th><th>Cost</th></tr></thead>
      <tbody>${days.map((d) => html`<tr><td>${new Date(d.day).toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })}</td>
        <td class="small mono">${byModel([d]).map((m) => m.spec).join(', ')}</td><td><b>${money(d.cost)}</b></td></tr>`)}</tbody>
    </table></div>
  </section>`;
}

// This session's calls as they happen.
function Recent() {
  const recent = app.use((s) => s.usage.recent);
  return html`<section class="card" aria-labelledby="h-use-recent">
    <div class="card-head"><div><h2 id="h-use-recent">This session, call by call</h2><p>Updates as each answer comes back.</p></div></div>
    ${recent.length ? html`<div class="tbl-wrap"><table class="tbl">
      <thead><tr><th>Time</th><th>For</th><th>Model</th><th>Audio</th><th>Tokens in → out</th><th>Cost</th></tr></thead>
      <tbody>${recent.map((r) => html`<tr>
        <td class="small">${new Date(r.time * 1000).toLocaleTimeString()}</td><td class="small">${featureName(r.feature)}</td>
        <td class="mono small">${r.model}</td><td>${r.seconds ? `${r.seconds} s` : '—'}</td>
        <td class="small">${r.audio_tokens || r.tokens_in || r.tokens_out ? `${tokens(r.audio_tokens + r.tokens_in)} → ${tokens(r.tokens_out)}` : '—'}</td>
        <td><b>${money(r.cost)}</b></td></tr>`)}</tbody></table></div>`
      : html`<p class="muted" style="margin:0;padding:14px 18px">No calls yet since Koli started.</p>`}
  </section>`;
}

// ── The price list ──────────────────────────────────────────────────────────

const KIND = { upload: 'Transcribes', live: 'Live, words as you speak', chat: 'Voice chat', '': 'Text' };
const perM = (v) => (v == null ? '—' : `$${v}`);

function editPrice(row, reload) {
  const values = { per_minute: row.per_minute ?? '', audio_in: row.audio_in ?? '', text_in: row.text_in ?? '', text_out: row.text_out ?? '' };
  const field = (key, label) => html`<label class="stack" style="gap:6px"><span class="eyebrow">${label}</span>
    <input class="inp mono" type="number" min="0" step="any" value=${values[key]} onInput=${(e) => { values[key] = e.currentTarget.value; }} /></label>`;
  const save = () => call('usage_price_save', row.spec, { ...values, transcribe: row.transcribe })
    .then(() => { closeModal(); toast(`Price saved for ${row.spec}`); reload(); }).catch((e) => toast(e.message, { kind: 'err' }));
  openModal(() => html`<div class="modal"><div class="modal-head"><h2>Price of ${row.spec}</h2></div>
    <div class="modal-body">
      <p class="small muted" style="margin:0">In US dollars. Leave a box empty when the provider doesn't charge that way.
        A call is priced by its tokens when the answer gives them, else by the minute.</p>
      <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px">
        ${field('per_minute', 'Per minute of audio')}${field('audio_in', 'Per 1M audio tokens in')}
        ${field('text_in', 'Per 1M text tokens in')}${field('text_out', 'Per 1M tokens out')}
      </div>
    </div>
    <div class="modal-foot"><button type="button" class="btn" onClick=${closeModal}>Cancel</button>
      <button type="button" class="btn pri" onClick=${save}>Save</button></div></div>`);
}

function PriceList() {
  const [version, setVersion] = useState(0);
  const rows = usePriceList(version);
  const [show, setShow] = useState('transcribe');
  const reload = () => setVersion((v) => v + 1);
  const reset = (spec) => call('usage_price_reset', spec).then(() => { toast(`${spec} is back to the shipped price`); reload(); })
    .catch((e) => toast(e.message, { kind: 'err' }));
  const shown = rows.filter((r) => (show === 'transcribe' ? r.transcribe : !r.transcribe));
  const providers = [...new Set(shown.map((r) => r.provider))];
  return html`<section class="card" aria-labelledby="h-use-prices">
    <div class="card-head"><div><h2 id="h-use-prices">Prices</h2>
      <p>Every model OpenAI and Gemini sell that Koli can use, from their pricing pages. Change a price when they do.</p></div>
      <${Segmented} value=${show} label="Which models" options=${[['transcribe', 'Hear audio'], ['text', 'Text only']]} onChange=${setShow} /></div>
    <div class="tbl-wrap"><table class="tbl">
      <thead><tr><th>Model</th><th>Kind</th>${show === 'transcribe' && html`<th>≈ an hour</th><th>Per minute</th><th>Audio in /1M</th>`}
        <th>Text in /1M</th><th>Out /1M</th><th></th></tr></thead>
      <tbody>${providers.map((p) => html`
        <tr><td colspan="8" class="eyebrow" style="background:var(--sunk)">${providerName(p)}
          ${!shown.find((r) => r.provider === p).has_key && html`<span class="chip sm warn" style="margin-inline-start:8px">No key yet · Settings › AI</span>`}</td></tr>
        ${shown.filter((r) => r.provider === p).map((r) => html`<tr>
          <td><span class="mono small">${r.model}</span>${r.note && html`<div class="small muted">${r.note}</div>`}
            ${r.changes.map((c) => html`<div class="small muted">From ${c.from}: ${Object.entries(c).filter(([k]) => k !== 'from').map(([k, v]) => `${k.replace('_', ' ')} $${v}`).join(', ')}</div>`)}</td>
          <td class="small">${KIND[r.transcribe || '']}${r.edited ? html` <span class="chip sm acc">Yours</span>` : ''}</td>
          ${show === 'transcribe' && html`<td><b>${r.per_hour != null ? money(r.per_hour) : '—'}</b></td><td>${perM(r.per_minute)}</td><td>${perM(r.audio_in)}</td>`}
          <td>${perM(r.text_in)}</td><td>${perM(r.text_out)}</td>
          <td style="white-space:nowrap"><button type="button" class="btn ghost sm" onClick=${() => editPrice(r, reload)}><${Icon} name="pen" size="sm" />Edit</button>
            ${r.edited && html`<button type="button" class="btn ghost sm" onClick=${() => reset(r.spec)}>Reset</button>`}</td>
        </tr>`)}`)}
      </tbody></table></div>
    <p class="small muted" style="margin:0;padding:10px 18px 14px">“≈ an hour” is an hour of speech: the per-minute price, or Gemini's 25 audio tokens a second plus about 175 tokens of text a minute.</p>
  </section>`;
}

// ── Screen ──────────────────────────────────────────────────────────────────

export function UsageScreen() {
  const [period, setPeriod] = useState(30);
  const [summary, setSummary] = useState(null);
  const lastCall = app.use((s) => s.usage.recent[0]);
  const limit = useSetting('usage.monthly_budget');   // a new limit shows at once
  useEffect(() => { call('usage_summary', period).then(setSummary).catch((e) => toast(e.message, { kind: 'err' })); }, [period, lastCall, limit]);
  return html`<div class="page">
    <div class="page-head"><div><h1 class="page-title">Usage & cost</h1>
      <p class="page-sub">What the online models cost you, model by model. Pick them in Settings › Model & engine (dictation) and Settings › Meetings.</p></div>
      <${Segmented} value=${period} label="Period" options=${[[7, '7 days'], [30, '30 days'], [90, '90 days']]} onChange=${setPeriod} /></div>
    ${summary ? html`<${Totals} summary=${summary} period=${period} /><${Budget} b=${summary.budget} /><${ModelTable} days=${summary.days} />` : html`<p class="muted">Loading…</p>`}
    <${Recent} />
    <${PriceList} />
    ${summary && html`<${DayTable} days=${summary.days} />`}
  </div>`;
}
