// screens/brief.js
// The meeting's Summary tab: the brief the summary step writes ("<id>.brief.json")
// laid out like the shared meeting brief. Outcomes first (the gist, decisions,
// action items, open questions), then who spoke, what was on screen, how the
// meeting went topic by topic, and the data an agent can read. Every time is a
// link into the transcript. printBrief() turns the same view into a PDF.

import { html, useState } from '../../vendor/preact-htm.module.js';
import { call } from '../api.js';
import { textDir, toast } from '../store.js';
import { Thumb } from '../shots.js';

export const COLOURS = ['#0A6E82', '#B45309', '#6D28D9', '#15803D', '#BE185D', '#0369A1', '#C2410C', '#4D7C0F'];
const STATUS = { do_now: ['ok', 'Do now'], plan_only: ['warn', 'Plan only'], waiting: ['warn', 'Waiting'], done: ['acc', 'Done'],
  parked: ['', 'Parked'] };
function clock(s) { s = Math.max(0, Math.floor(s || 0)); return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`; }
function day(iso) {
  try { return new Date(iso).toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' }); } catch (e) { return ''; }
}
function hhmm(iso) { return (iso || '').slice(11, 16); }

// ── Numbers from the turns ────────────────────────────────────────────────

// Seconds and turns per speaker; speakers under 1% are folded into a note.
function talkStats(m) {
  const by = {};
  for (const t of m.turns) {
    const s = by[t.speaker] || (by[t.speaker] = { id: t.speaker, seconds: 0, turns: 0 });
    s.seconds += Math.max(0, t.end - t.start);
    s.turns += 1;
  }
  const total = Object.values(by).reduce((a, s) => a + s.seconds, 0) || 1;
  const all = Object.values(by).sort((a, b) => b.seconds - a.seconds).map((s) => ({ ...s, share: s.seconds / total }));
  return { shown: all.filter((s) => s.share >= 0.01), folded: all.filter((s) => s.share < 0.01) };
}

// Seconds spoken in each minute, per speaker (overlapping speech adds up).
function perMinute(m) {
  const minutes = [];
  for (const t of m.turns) {
    for (let mn = Math.floor(t.start / 60); mn <= Math.floor(t.end / 60); mn++) {
      const v = Math.min(t.end, (mn + 1) * 60) - Math.max(t.start, mn * 60);
      if (v <= 0) continue;
      const row = minutes[mn] || (minutes[mn] = {});
      row[t.speaker] = (row[t.speaker] || 0) + v;
    }
  }
  for (let i = 0; i < minutes.length; i++) minutes[i] = minutes[i] || {};
  return minutes;
}

// ── Pieces ────────────────────────────────────────────────────────────────

function At({ at, onJump }) {
  if (at == null) return null;
  return html`<button type="button" class="bf-at" title="Open the transcript here" onClick=${() => onJump && onJump(at)}>${clock(at)}</button>`;
}

function Who({ sp }) {
  return html`<span class="bf-who"><i style=${`background:${COLOURS[sp.colour % 8]}`}></i>${sp.name}</span>`;
}

function Status({ status }) {
  const [tone, label] = STATUS[status] || ['', status || ''];
  return html`<span class=${'bf-status ' + tone}>${label}</span>`;
}

// A chart's hover note, placed at the pointer inside its card.
function useTip() {
  const [tip, setTip] = useState(null);
  const show = (e, text) => {
    const box = e.currentTarget.closest('.bf-fig').getBoundingClientRect();
    setTip({ text, x: e.clientX - box.left + 12, y: e.clientY - box.top + 12 });
  };
  const view = tip && html`<div class="bf-tip" style=${`left:${tip.x}px;top:${tip.y}px`}>${tip.text}</div>`;
  return [show, () => setTip(null), view];
}

function ShareBars({ stats, who }) {
  const [show, hide, tip] = useTip();
  const rows = stats.shown;
  const max = Math.max(...rows.map((s) => s.seconds), 1);
  const h = 12 + rows.length * 34;
  return html`<div class="bf-fig bf-share"><h4>Talk time per speaker</h4><div class="bf-sub">Seconds of speech, from the transcript's timestamps</div>
    <svg viewBox=${`0 0 360 ${h}`} class="bf-chart" role="img" aria-label="Talk time per speaker">
      ${rows.map((s, i) => {
        const sp = who(s.id); const y = 6 + i * 34; const w = 170 * s.seconds / max;
        return html`<g onMouseMove=${(e) => show(e, `${sp.name}: ${clock(s.seconds)}, ${s.turns} turns`)} onMouseLeave=${hide}>
          <text x="84" y=${y + 15} text-anchor="end" font-size="12" fill="var(--ink2)">${sp.name}</text>
          <rect x="92" y=${y} width=${w} height="20" rx="4" fill=${COLOURS[sp.colour % 8]} />
          <text x=${98 + w} y=${y + 15} font-size="11.5" fill="var(--ink2)" font-family="var(--mono)">${clock(s.seconds)} · ${Math.round(s.share * 100)}%</text></g>`;
      })}</svg>${tip}</div>`;
}

function Timeline({ minutes, order, who }) {
  const [show, hide, tip] = useTip();
  const W = 640, H = 200, L = 36, R = 8, T = 10, B = 26;
  const ph = H - T - B, pw = W - L - R, cw = pw / Math.max(minutes.length, 1);
  const top = Math.max(...minutes.map((m) => order.reduce((a, k) => a + (m[k] || 0), 0)), 20);
  const ymax = Math.ceil(top / 20) * 20;
  const grid = []; for (let v = 0; v <= ymax; v += 20) grid.push(v);
  const step = minutes.length > 40 ? 10 : 5;
  return html`<div class="bf-fig"><h4>Who held the floor, minute by minute</h4>
    <div class="bf-sub">Seconds spoken in each minute, stacked. A minute can pass 60 s when people talk over each other.</div>
    <svg viewBox=${`0 0 ${W} ${H}`} class="bf-chart" role="img" aria-label="Who held the floor, minute by minute">
      ${grid.map((v) => { const y = T + ph - ph * v / ymax; return html`<g><line x1=${L} x2=${W - R} y1=${y} y2=${y} stroke="var(--line)" />
        <text x=${L - 6} y=${y + 4} text-anchor="end" font-size="10" fill="var(--mute)" font-family="var(--mono)">${v}s</text></g>`; })}
      ${minutes.map((m, i) => {
        let y = T + ph;
        const label = `Minute ${clock(i * 60)}: ` + (order.filter((k) => m[k]).map((k) => `${who(k).name} ${Math.round(m[k])}s`).join(', ') || 'quiet');
        return html`<g onMouseMove=${(e) => show(e, label)} onMouseLeave=${hide}>
          <rect x=${L + i * cw} y=${T} width=${cw} height=${ph} fill="transparent" />
          ${order.map((k) => { const v = m[k] || 0; if (v <= 0) return null; const h = ph * v / ymax; y -= h;
            return html`<rect x=${L + i * cw + 1} y=${y} width=${Math.max(cw - 2, 1)} height=${Math.max(h - 2, 0.5)} fill=${COLOURS[who(k).colour % 8]} />`; })}
          ${i % step === 0 ? html`<text x=${L + i * cw + cw / 2} y=${H - 8} text-anchor="middle" font-size="10" fill="var(--mute)" font-family="var(--mono)">${clock(i * 60)}</text>` : null}</g>`;
      })}</svg>
    <div class="bf-legend">${order.map((k) => html`<${Who} sp=${who(k)} />`)}</div>${tip}</div>`;
}

function Topic({ t, shots, meetingId, onJump, onOpenShot }) {
  return html`<details class="bf-topic">
    <summary><span class="bf-range">${clock(t.start)}–${clock(t.end)}</span>
      <span class="bf-ttl">${t.title}<span class="bf-lead">${t.lead || ''}</span></span></summary>
    <div class="bf-body">
      ${t.points && t.points.length > 0 && html`<ul>${t.points.map((p) => html`<li>${p}</li>`)}</ul>`}
      ${(t.quotes || []).map((q) => html`<blockquote class="bf-quote"><p dir=${textDir(q.text)}>${q.text}</p>
        <cite>${q.who} · <${At} at=${q.at} onJump=${onJump} /></cite></blockquote>`)}
      ${shots.map((s) => html`<figure class="bf-shotfig"><${Thumb} meetingId=${meetingId} shot=${s} width=${200} onOpen=${onOpenShot} />
        <figcaption><b>${clock(s.at)} · ${s.caption || 'Screenshot'}</b><span class="bf-sub">Taken during this part of the meeting</span></figcaption></figure>`)}
    </div></details>`;
}

// The Data tab: the brief as JSON with ids and times, for YouTrack or an agent.
export function BriefData({ brief }) {
  if (!brief) return html`<div class="empty">No summary yet, so there is no data to hand to an agent.</div>`;
  const text = JSON.stringify(brief, null, 2);
  const copy = (e) => {
    const pre = e.currentTarget.closest('.bf-data').querySelector('pre');
    navigator.clipboard.writeText(text).then(() => toast('JSON copied'), () => {
      const r = document.createRange(); r.selectNodeContents(pre); const s = getSelection(); s.removeAllRanges(); s.addRange(r);
      toast('Selected; press Ctrl+C');
    });
  };
  return html`<div class="bf-data">
    <p class="bf-foot" style="margin:0 0 10px">The same decisions, action items, questions and topics as JSON, with ids and times in seconds from the start,
      for YouTrack import or further AI work. Owners and dates are empty when nobody said them.</p>
    <div style="margin-bottom:10px"><button type="button" class="btn sm" onClick=${copy}>Copy JSON</button></div>
    <pre class="bf-json" style="max-height:none">${text}</pre></div>`;
}

// ── The tab ───────────────────────────────────────────────────────────────

export function Brief({ m, brief, onJump, onOpenShot }) {
  if (!brief) {
    return html`<div class="empty">No summary yet. Once the summary step runs, this tab shows the decisions, the action items and what was said about each topic.</div>`;
  }
  const names = Object.fromEntries(m.speakers.map((s) => [s.id, s]));
  const who = (id) => names[id] || { id, name: id === '?' ? 'Speaker ?' : id, colour: 7 };
  const byName = (name) => m.speakers.find((s) => s.name === name || s.name.split(' ')[0] === name);
  const stats = talkStats(m);
  const order = stats.shown.map((s) => s.id);
  const shots = (m.screenshots || []).slice().sort((a, b) => a.at - b.at);
  const inTopic = (t) => shots.filter((s) => s.at >= t.start && s.at < t.end);
  const roles = brief.roles || {};
  const length = m.ended_at ? (new Date(m.ended_at) - new Date(m.started_at)) / 1000 : Math.max(0, ...m.turns.map((t) => t.end));
  const count = (list, word) => list && list.length > 0 && html`<span class="bf-count">${list.length}${word ? ` ${word}` : ''}</span>`;
  return html`<div class="brief" dir="ltr">
    <header class="bf-head">
      <div class="bf-eyebrow">Meeting brief</div>
      <h2>${brief.title || m.title}</h2>
      <div class="bf-meta">
        <span>${day(m.started_at)}, <b>${hhmm(m.started_at)}${m.ended_at ? `–${hhmm(m.ended_at)}` : ''}</b></span>
        <span>Length <b class="mono">${clock(length)}</b></span>
        <span>Speakers <b>${stats.shown.map((s) => who(s.id).name).join(', ')}</b></span>
        ${m.source && html`<span>Source <b>${m.source.app ? m.source.app.replace('.exe', '') : 'started by hand'}</b></span>`}
      </div>
    </header>

    <div class="bf-tldr">${(brief.tldr || []).map((p) => html`<p dir=${textDir(p)}>${p}</p>`)}</div>

    ${brief.decisions && brief.decisions.length > 0 && html`<section><h3>Decisions ${count(brief.decisions)}</h3>
      <ul class="bf-list">${brief.decisions.map((d) => html`<li><span class="bf-id">${d.id}</span><span>${d.text} <${At} at=${d.at} onJump=${onJump} /></span></li>`)}</ul></section>`}

    ${brief.action_items && brief.action_items.length > 0 && html`<section><h3>Action items ${count(brief.action_items)}</h3>
      <div class="bf-tablewrap"><table class="bf-table"><thead><tr><th></th><th>Task</th><th>Owner</th><th>Status</th><th>Source</th></tr></thead><tbody>
        ${brief.action_items.map((a) => { const sp = a.owner && byName(a.owner); return html`<tr>
          <td class="bf-id">${a.id}</td><td>${a.task}${a.effort && html`<span class="bf-note">Effort: ${a.effort}</span>`}</td>
          <td>${a.owner ? (sp ? html`<${Who} sp=${sp} />` : html`<span class="bf-who"><i></i>${a.owner}</span>`) : html`<span class="muted">Not set</span>`}</td>
          <td><${Status} status=${a.status} />${a.due && html`<span class="bf-note">Due ${a.due}</span>`}</td>
          <td><${At} at=${a.at} onJump=${onJump} /></td></tr>`; })}
      </tbody></table></div>
      ${brief.action_items.every((a) => !a.due) && html`<p class="bf-foot">No deadlines were stated. "Do now" means it was asked to start right away; it is not a date.</p>`}</section>`}

    ${((brief.open_questions || []).length > 0 || (brief.parked || []).length > 0) && html`<section class="bf-two">
      ${(brief.open_questions || []).length > 0 && html`<div><h3>Open questions ${count(brief.open_questions)}</h3>
        <ul class="bf-list">${brief.open_questions.map((q) => html`<li><span class="bf-id">${q.id}</span><span>${q.text} <${At} at=${q.at} onJump=${onJump} /></span></li>`)}</ul></div>`}
      ${(brief.parked || []).length > 0 && html`<div><h3>Parked ${count(brief.parked)}</h3>
        <ul class="bf-list">${brief.parked.map((p) => html`<li><span class="bf-id">${p.id}</span><span><${Status} status="parked" /> ${p.text}</span></li>`)}</ul></div>`}
    </section>`}

    ${m.turns.length > 0 && html`<section><h3>Who spoke</h3>
      <div class="bf-charts"><${ShareBars} stats=${stats} who=${who} /><${Timeline} minutes=${perMinute(m)} order=${order} who=${who} /></div>
      <div class="bf-tablewrap"><table class="bf-table"><thead><tr><th>Speaker</th><th>Talk time</th><th>Share</th><th>Turns</th><th>Role in this meeting</th></tr></thead><tbody>
        ${stats.shown.map((s) => html`<tr><td><${Who} sp=${who(s.id)} /></td><td class="mono">${clock(s.seconds)}</td>
          <td class="mono">${Math.round(s.share * 100)}%</td><td class="mono">${s.turns}</td><td>${roles[s.id] || ''}</td></tr>`)}
      </tbody></table></div>
      ${stats.folded.length > 0 && html`<p class="bf-foot">${stats.folded.map((s) => who(s.id).name).join(', ')}: under 1% of the talk, likely a voice split by mistake; left out of the charts.</p>`}</section>`}

    ${shots.length > 0 && html`<section><h3>What was on screen ${count(shots)}</h3>
      <p class="bf-foot">Screenshots taken during the meeting, in order. Each one also sits inside its topic below and at its moment in the transcript.</p>
      <div class="bf-strip">${shots.map((s) => html`<div class="bf-card"><${Thumb} meetingId=${m.id} shot=${s} width=${180} onOpen=${onOpenShot} />
        <div class="bf-cap"><span class="mono">${clock(s.at)}</span> ${s.caption || 'Screenshot'}</div></div>`)}</div></section>`}

    ${brief.topics && brief.topics.length > 0 && html`<section><h3>How the meeting went ${count(brief.topics, 'topics')}</h3>
      <div class="bf-topics">${brief.topics.map((t) => html`<${Topic} t=${t} shots=${inTopic(t)} meetingId=${m.id} onJump=${onJump} onOpenShot=${onOpenShot} />`)}</div></section>`}

    ${(brief.notes || []).length > 0 && html`<p class="bf-foot">Transcription notes: ${brief.notes.join('; ')}.</p>`}
    ${brief.generator && html`<p class="bf-foot">Written by: ${brief.generator}</p>`}
  </div>`;
}

// The Summary tab as a PDF: a copy of it with every topic open goes into a
// print-only container, WebView2 prints the page, and the copy is removed.
export async function printBrief(node, meetingId, title) {
  if (!node) return;
  const root = document.createElement('div');
  root.id = 'print-root';
  const copy = node.cloneNode(true);
  copy.querySelectorAll('details').forEach((d) => { d.open = true; });
  root.append(copy);
  document.body.appendChild(root);
  document.body.classList.add('print-brief');
  try {
    const path = await call('meeting_export_pdf', meetingId, title);
    if (path) toast(`Saved the PDF to ${path}`);
  } catch (e) {
    toast(e.message, { kind: 'err' });
  } finally {
    document.body.classList.remove('print-brief');
    root.remove();
  }
}
