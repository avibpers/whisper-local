// screens/brief.js
// The meeting's Summary tab: the brief the summary step writes ("<id>.brief.json")
// laid out like the shared meeting brief. Outcomes first (the gist, decisions,
// action items, open questions), then who spoke, what was on screen, how the
// meeting went topic by topic, and the data an agent can read. Every time is a
// link into the transcript. printBrief() turns the same view into a PDF.

import { html, useEffect, useState } from '../../vendor/preact-htm.module.js';
import { call } from '../api.js';
import { app, closeModal, navigate, openModal, textDir, toast } from '../store.js';
import { Thumb } from '../shots.js';
import { Icon, Select } from '../ui.js';

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

// The visual brief's order, for briefs written before templates existed.
const STANDARD_BLOCKS = ['header', 'tldr', 'decisions', 'action_items', 'lists', 'speakers', 'screenshots', 'topics', 'notes'];
const LABELS = {
  tldr: 'In short', decisions: 'Decisions', action_items: 'Action items', open_questions: 'Open questions', parked: 'Parked',
  next_meeting: 'Points for the next meeting', speakers: 'Who spoke', screenshots: 'What was on screen',
  topics: 'How the meeting went', participants: 'Participants', agenda: 'Meeting Agenda',
  agenda_details: 'Key Points, Decisions, and Action Items', agenda_discussion: 'Discussion', agenda_decisions: 'Decisions',
  agenda_actions: 'Action items', notes: 'Transcription notes',
};
const RTL = ['he', 'ar', 'fa', 'ur', 'yi'];
export const SUMMARY_LANGUAGES = [['', 'As the template says'], ['he', 'Hebrew · עברית'], ['en', 'English'], ['ar', 'Arabic'],
  ['ru', 'Russian'], ['fr', 'French'], ['es', 'Spanish'], ['de', 'German']];

// An action items table; owners who spoke get their colour.
function Actions({ items, byName, onJump }) {
  return html`<div class="bf-tablewrap"><table class="bf-table"><thead><tr><th></th><th>Task</th><th>Owner</th><th>Status</th><th>Source</th></tr></thead><tbody>
    ${items.map((a) => { const sp = a.owner && byName(a.owner); return html`<tr>
      <td class="bf-id">${a.id}</td><td dir=${textDir(a.task)}>${a.task}${a.effort && html`<span class="bf-note">Effort: ${a.effort}</span>`}</td>
      <td>${a.owner ? (sp ? html`<${Who} sp=${sp} />` : html`<span class="bf-who"><i></i>${a.owner}</span>`) : html`<span class="muted">Not set</span>`}</td>
      <td><${Status} status=${a.status} />${a.due && html`<span class="bf-note">Due ${a.due}</span>`}</td>
      <td><${At} at=${a.at} onJump=${onJump} /></td></tr>`; })}
  </tbody></table></div>`;
}

function Items({ list, onJump, parked }) {
  return html`<ul class="bf-list">${list.map((x) => html`<li><span class="bf-id">${x.id}</span>
    <span dir=${textDir(x.text)}>${parked ? html`<${Status} status="parked" /> ` : ''}${x.text} <${At} at=${x.at} onJump=${onJump} /></span></li>`)}</ul>`;
}

// The meeting's summary, laid out by the template it was written with:
// its blocks in its order, with its headings.
export function Brief({ m, brief, layout, onJump, onOpenShot }) {
  if (!brief) return null;
  const names = Object.fromEntries(m.speakers.map((s) => [s.id, s]));
  const renamed = brief.participants || {};
  const who = (id) => {
    const sp = names[id] || { id, name: id === '?' ? 'Speaker ?' : id, colour: 7 };
    return renamed[id] && /^(You|Speaker \d+)$/.test(sp.name) ? { ...sp, name: renamed[id] } : sp;
  };
  const byName = (name) => m.speakers.map((s) => who(s.id)).find((s) => s.name === name || s.name.split(' ')[0] === String(name).split(' ')[0]);
  const stats = talkStats(m);
  const order = stats.shown.map((s) => s.id);
  const shots = (m.screenshots || []).slice().sort((a, b) => a.at - b.at);
  const inTopic = (t) => shots.filter((s) => s.at >= t.start && s.at < t.end);
  const roles = brief.roles || {};
  const length = m.ended_at ? (new Date(m.ended_at) - new Date(m.started_at)) / 1000 : Math.max(0, ...m.turns.map((t) => t.end));
  const count = (list, word) => list && list.length > 0 && html`<span class="bf-count">${list.length}${word ? ` ${word}` : ''}</span>`;
  const lay = layout || {};
  const label = (k) => (brief.labels && brief.labels[k]) || (lay.labels && lay.labels[k]) || LABELS[k] || k;
  const agenda = brief.agenda || [];
  const decisions = (brief.decisions && brief.decisions.length) ? brief.decisions : agenda.flatMap((a) => a.decisions || []);
  const actions = (brief.action_items && brief.action_items.length) ? brief.action_items : agenda.flatMap((a) => a.action_items || []);
  const dateOnly = lay.date === 'date';
  // People who were there but are not in the charts: a named voice under
  // 1% of the talk, or someone who said nothing (from the call or added by
  // you). They still belong in the report.
  const isDefault = (name) => /^(You|Speaker \d+)$/.test(name);
  const quiet = (m.roster || []).filter((r) => !order.includes(r.speaker) && (r.speaker == null || !isDefault(who(r.speaker).name)))
    .map((r) => ({ ...r, name: r.speaker ? who(r.speaker).name : r.name }));
  const splitOff = stats.folded.filter((s) => isDefault(who(s.id).name));
  const attendees = (brief.attendees && brief.attendees.length) ? brief.attendees
    : [...stats.shown.map((s) => ({ name: who(s.id).name, role: roles[s.id] || '' })),
      ...quiet.map((r) => ({ name: r.name, role: r.spoke ? '' : 'did not speak' }))];

  const blocks = {
    header: () => html`<header class="bf-head">
      <div class="bf-eyebrow">${lay.name || 'Meeting brief'}</div>
      <h2 dir=${textDir(brief.title || m.title)}>${brief.title || m.title}</h2>
      <div class="bf-meta">
        ${dateOnly ? html`<span><b>${(brief.date || m.started_at || '').slice(0, 10)}</b></span>` : html`
          <span>${day(m.started_at)}, <b>${hhmm(m.started_at)}${m.ended_at ? `–${hhmm(m.ended_at)}` : ''}</b></span>
          <span>Length <b class="mono">${clock(length)}</b></span>
          <span>Speakers <b>${stats.shown.map((s) => who(s.id).name).join(', ')}</b></span>
          ${m.source && html`<span>Source <b>${m.source.app ? m.source.app.replace('.exe', '') : 'started by hand'}</b></span>`}`}
      </div></header>`,
    tldr: () => (brief.tldr || []).length > 0 && html`<div class="bf-tldr">${brief.tldr.map((p) => html`<p dir=${textDir(p)}>${p}</p>`)}</div>`,
    participants: () => attendees.length > 0 && html`<section><h3>${label('participants')}</h3>
      <div class="bf-people">${attendees.map((a) => html`<span class="bf-person" dir="auto">${a.name}${a.role ? html` <span class="muted">· ${a.role}</span>` : ''}</span>`)}</div></section>`,
    agenda: () => agenda.length > 0 && html`<section><h3>${label('agenda')}</h3>
      <ol class="bf-agenda">${agenda.map((a) => html`<li dir=${textDir(a.title)}>${a.title} ${a.start != null ? html`<${At} at=${a.start} onJump=${onJump} />` : ''}</li>`)}</ol></section>`,
    agenda_details: () => agenda.length > 0 && html`<section><h3>${label('agenda_details')}</h3>
      <div class="bf-items">${agenda.map((a, i) => html`<article class="bf-item">
        <h4><span class="bf-num">${i + 1}.</span><span dir=${textDir(a.title)}>${a.title}</span></h4>
        ${(a.discussion || []).length > 0 && html`<div class="bf-grp"><h5>${label('agenda_discussion')}</h5>
          <ul>${a.discussion.map((x) => html`<li dir=${textDir(x)}>${x}</li>`)}</ul></div>`}
        ${(a.decisions || []).length > 0 && html`<div class="bf-grp"><h5>${label('agenda_decisions')}</h5><${Items} list=${a.decisions} onJump=${onJump} /></div>`}
        ${(a.action_items || []).length > 0 && html`<div class="bf-grp"><h5>${label('agenda_actions')}</h5>
          <${Actions} items=${a.action_items} byName=${byName} onJump=${onJump} /></div>`}
      </article>`)}</div></section>`,
    decisions: () => decisions.length > 0 && html`<section><h3>${label('decisions')} ${count(decisions)}</h3><${Items} list=${decisions} onJump=${onJump} /></section>`,
    action_items: () => actions.length > 0 && html`<section><h3>${label('action_items')} ${count(actions)}</h3>
      <${Actions} items=${actions} byName=${byName} onJump=${onJump} />
      ${actions.every((a) => !a.due) && html`<p class="bf-foot">No deadlines were stated. "Do now" means it was asked to start right away; it is not a date.</p>`}</section>`,
    open_questions: () => (brief.open_questions || []).length > 0 && html`<section><h3>${label('open_questions')} ${count(brief.open_questions)}</h3>
      <${Items} list=${brief.open_questions} onJump=${onJump} /></section>`,
    parked: () => (brief.parked || []).length > 0 && html`<section><h3>${label('parked')} ${count(brief.parked)}</h3>
      <${Items} list=${brief.parked} onJump=${onJump} parked=${true} /></section>`,
    lists: () => ((brief.open_questions || []).length > 0 || (brief.parked || []).length > 0) && html`<section class="bf-two">
      ${(brief.open_questions || []).length > 0 && html`<div><h3>${label('open_questions')} ${count(brief.open_questions)}</h3><${Items} list=${brief.open_questions} onJump=${onJump} /></div>`}
      ${(brief.parked || []).length > 0 && html`<div><h3>${label('parked')} ${count(brief.parked)}</h3><${Items} list=${brief.parked} onJump=${onJump} parked=${true} /></div>`}
    </section>`,
    next_meeting: () => (brief.next_meeting || []).length > 0 && html`<section><h3>${label('next_meeting')} ${count(brief.next_meeting)}</h3>
      <${Items} list=${brief.next_meeting} onJump=${onJump} /></section>`,
    speakers: () => (m.turns.length > 0 || quiet.length > 0) && html`<section><h3>${label('speakers')}</h3>
      <div class="bf-charts"><${ShareBars} stats=${stats} who=${who} /><${Timeline} minutes=${perMinute(m)} order=${order} who=${who} /></div>
      <div class="bf-tablewrap"><table class="bf-table"><thead><tr><th>Speaker</th><th>Talk time</th><th>Share</th><th>Turns</th><th>Role in this meeting</th></tr></thead><tbody>
        ${stats.shown.map((s) => html`<tr><td><${Who} sp=${who(s.id)} /></td><td class="mono">${clock(s.seconds)}</td>
          <td class="mono">${Math.round(s.share * 100)}%</td><td class="mono">${s.turns}</td><td>${roles[s.id] || ''}</td></tr>`)}
        ${quiet.map((r) => html`<tr><td>${r.speaker ? html`<${Who} sp=${who(r.speaker)} />` : html`<span class="bf-who"><i></i>${r.name}</span>`}</td>
          <td class=${r.spoke ? 'mono' : 'muted'}>${r.spoke ? clock(r.seconds) : 'Did not speak'}</td><td></td>
          <td class="mono">${r.turns || ''}</td><td>${(r.speaker && roles[r.speaker]) || ''}</td></tr>`)}
      </tbody></table></div>
      ${splitOff.length > 0 && html`<p class="bf-foot">${splitOff.map((s) => who(s.id).name).join(', ')}: under 1% of the talk, likely a voice split by mistake; left out of the charts.</p>`}</section>`,
    screenshots: () => shots.length > 0 && html`<section><h3>${label('screenshots')} ${count(shots)}</h3>
      <p class="bf-foot">Screenshots taken during the meeting, in order. Each one also sits inside its topic and at its moment in the transcript.</p>
      <div class="bf-strip">${shots.map((s) => html`<div class="bf-card"><${Thumb} meetingId=${m.id} shot=${s} width=${180} onOpen=${onOpenShot} />
        <div class="bf-cap"><span class="mono">${clock(s.at)}</span> ${s.caption || 'Screenshot'}</div></div>`)}</div></section>`,
    topics: () => (brief.topics || []).length > 0 && html`<section><h3>${label('topics')} ${count(brief.topics, 'topics')}</h3>
      <div class="bf-topics">${brief.topics.map((t) => html`<${Topic} t=${t} shots=${inTopic(t)} meetingId=${m.id} onJump=${onJump} onOpenShot=${onOpenShot} />`)}</div></section>`,
    sections: () => (brief.sections || []).map((s) => html`<section><h3 dir=${textDir(s.title)}>${s.title}</h3>
      ${(s.text || []).map((p) => html`<p dir=${textDir(p)} style="margin:0 0 8px">${p}</p>`)}
      ${(s.items || []).length > 0 && html`<ul class="bf-bullets">${s.items.map((x) => html`<li dir=${textDir(x)}>${x}</li>`)}</ul>`}</section>`),
    notes: () => (brief.notes || []).length > 0 && html`<p class="bf-foot">${label('notes')}: ${brief.notes.join('; ')}.</p>`,
  };
  const want = (lay.blocks && lay.blocks.length ? lay.blocks : STANDARD_BLOCKS).filter((b) => blocks[b]);
  const dir = RTL.includes(String(brief.language || '').slice(0, 2)) ? 'rtl' : 'ltr';
  return html`<div class="brief" dir=${dir}>
    ${want.map((b) => blocks[b]())}
    ${brief.generator && html`<p class="bf-foot">Written by: ${brief.generator}</p>`}
  </div>`;
}

// Asking the AI for a summary: the template, the language and your own
// instructions, then Summarize. With no summary it fills the tab; with one,
// "Summarize again" sits above it and opens the same choices in a dialog.
// While a summary is written it shows the progress.
export function SummaryBar({ meetingId, brief, live, onStarted }) {
  const has = !!brief;
  const [st, setSt] = useState(null);
  const [templates, setTemplates] = useState([]);
  const tasks = app.use((s) => s.tasks);
  const job = Object.values(tasks || {}).find((t) => t.kind === 'meeting_summary' && t.ref === meetingId && t.status === 'running');
  useEffect(() => {
    call('meeting_summary_state', meetingId).then(setSt).catch(() => {});
    call('summary_templates').then(setTemplates).catch(() => {});
  }, [meetingId, brief]);
  if (live) return has ? null : html`<div class="empty">The summary is written when the meeting ends.</div>`;
  if (!st) return null;
  if (job || st.running) {
    return html`<div class="bf-ask running"><span class="spin" aria-hidden="true"></span>
      <div><b>Writing the summary…</b><div class="small muted">${(job && job.detail) || 'The AI is reading the transcript'}</div></div></div>`;
  }
  const started = () => { setSt({ ...st, running: true }); onStarted && onStarted(); };
  if (has) {
    const open = () => openModal(() => html`<${SummarizeDialog} meetingId=${meetingId} st=${st} templates=${templates} onStarted=${started} />`);
    return html`<div class="bf-again"><button type="button" class="btn ghost sm" onClick=${open}><${Icon} name="refresh" />Summarize again</button></div>`;
  }
  return html`<div class="bf-ask" style="flex-direction:column;align-items:stretch">
    <div><b>No summary yet</b>
      <div class="small muted">${st.ai_ready ? 'The AI writes it from the transcript, laid out by the template you pick.'
        : html`Choose an AI model first in <a href="#" onClick=${(e) => { e.preventDefault(); navigate('settings/ai'); }}>Settings › AI</a>.`}</div></div>
    <${SummarizeForm} meetingId=${meetingId} st=${st} templates=${templates} onStarted=${started} />
  </div>`;
}

// The choices for a summary: template, language, instructions, Summarize.
function SummarizeForm({ meetingId, st, templates, onStarted, onCancel, again = false }) {
  const [tpl, setTpl] = useState(st.template);
  const [lang, setLang] = useState('');
  const [text, setText] = useState(again ? (st.instructions || '') : '');
  const go = () => call('meeting_summarize', meetingId, tpl, lang, text.trim())
    .then(() => { onStarted && onStarted(); })
    .catch((e) => toast(e.message, { kind: 'err' }));
  return html`<div class="stack" style="gap:10px">
    <div class="row-flex">
      <${Select} label="Template" value=${tpl} options=${templates.map((t) => [t.id, t.name + (t.default ? ' (default)' : '')])} onChange=${setTpl} />
      <${Select} label="Language" value=${lang} options=${SUMMARY_LANGUAGES} onChange=${setLang} />
    </div>
    <label class="stack" style="gap:6px"><span class="eyebrow">Instructions for the AI (optional)</span>
      <textarea class="inp" rows="4" dir="auto" value=${text} onInput=${(e) => setText(e.currentTarget.value)}
        placeholder="Focus on the budget. Speaker 2 is Dana. Ilona was there and didn't speak."></textarea></label>
    <div class="row-flex" style="justify-content:flex-end">
      ${onCancel && html`<button type="button" class="btn" onClick=${onCancel}>Cancel</button>`}
      <button type="button" class="btn pri" disabled=${!st.ai_ready} onClick=${go}>${again ? 'Summarize again' : 'Summarize'}</button>
    </div></div>`;
}

// "Summarize again" as a dialog over the summary.
function SummarizeDialog({ meetingId, st, templates, onStarted }) {
  return html`<div class="modal" role="dialog" aria-modal="true" aria-labelledby="sumtitle" style="width:min(560px,92vw)">
    <div class="modal-head"><h2 id="sumtitle">Summarize again</h2></div>
    <div class="modal-body">
      <p class="small muted" style="margin:0 0 10px">The AI writes the whole summary again from the transcript. Tell it what to do differently.</p>
      <${SummarizeForm} meetingId=${meetingId} st=${st} templates=${templates} again=${true}
        onStarted=${() => { closeModal(); onStarted(); }} onCancel=${closeModal} /></div>
  </div>`;
}

// ── Google Drive ──────────────────────────────────────────────────────────

// Google Drive's mark, in its own colours.
function DriveMark() {
  return html`<svg class="drv-mark" viewBox="0 0 87.3 78" aria-hidden="true">
    <path d="M6.6 66.85l3.85 6.65c.8 1.4 1.95 2.5 3.3 3.3L27.5 53H0c0 1.55.4 3.1 1.2 4.5z" fill="#0066DA" />
    <path d="M43.65 25L29.9 1.2c-1.35.8-2.5 1.9-3.3 3.3L1.2 48.5A9.06 9.06 0 000 53h27.5z" fill="#00AC47" />
    <path d="M73.55 76.8c1.35-.8 2.5-1.9 3.3-3.3l1.6-2.75 7.65-13.25c.8-1.4 1.2-2.95 1.2-4.5H59.8l5.85 11.5z" fill="#EA4335" />
    <path d="M43.65 25L57.4 1.2C56.05.4 54.5 0 52.9 0H34.4c-1.6 0-3.15.45-4.5 1.2z" fill="#00832D" />
    <path d="M59.8 53H27.5L13.75 76.8c1.35.8 2.9 1.2 4.5 1.2h50.8c1.6 0 3.15-.45 4.5-1.2z" fill="#2684FC" />
    <path d="M73.4 26.5l-12.7-22c-.8-1.4-1.95-2.5-3.3-3.3L43.65 25 59.8 53h27.45c0-1.55-.4-3.1-1.2-4.5z" fill="#FFBA00" /></svg>`;
}

// Saving the meeting to Google Drive: Connect while signed out, Save once
// connected, then Open and Share (Google's own Share, in the doc) with copy
// link and save again beside them.
export function DriveButton({ m, brief, onSaved }) {
  const [st, setSt] = useState(null);
  const [busy, setBusy] = useState(false);
  const reload = () => call('drive_status').then(setSt).catch(() => setSt({ connected: false, configured: false }));
  useEffect(() => { reload(); }, []);
  if (!st || !brief) return null;
  const saved = m.drive && m.drive.doc_id;
  if (!st.connected) {
    const connect = () => {
      if (!st.configured) { toast('Set up Google Drive first: paste the Google client, then Connect'); navigate('settings/drive'); return; }
      import('./settings.js').then((mod) => mod.openDriveLogin(reload));
    };
    return html`<button type="button" class="btn drv" title="Sign Koli in to Google Drive to save meetings there" onClick=${connect}>
      <${DriveMark} />${st.reconnect ? 'Reconnect Google Drive' : 'Connect Google Drive'}</button>`;
  }
  const save = () => {
    setBusy(true);
    call('meeting_drive_export', m.id).then((info) => {
      onSaved(info);
      toast(saved ? 'Saved again to Google Drive' : 'Saved to Google Drive', { action: { label: 'Open', run: () => call('meeting_drive_open', m.id) } });
    }).catch((e) => toast(e.message, { kind: 'err' })).finally(() => setBusy(false));
  };
  if (busy) return html`<button type="button" class="btn drv" disabled><${DriveMark} />Saving to Drive…</button>`;
  if (!saved) {
    return html`<button type="button" class="btn drv" title=${`Saves a Google Doc with a Quick summary, the report and the transcript to Koli › Meetings in ${st.email}'s Drive`}
      onClick=${save}><${DriveMark} />Save to Google Drive</button>`;
  }
  const open = () => call('meeting_drive_open', m.id).catch((e) => toast(e.message, { kind: 'err' }));
  // Drive for desktop's own Share window when it has the doc; the doc in the
  // browser otherwise, where Google's Share button is top right.
  const share = () => call('meeting_drive_share', m.id).then((r) => {
    if (!r.window) toast('Opened in Google Docs: press Share at the top right to choose who can see it');
  }).catch((e) => toast(e.message, { kind: 'err' }));
  const copy = () => call('copy_text', m.drive.doc_url).then(() => toast('Link copied. Who can open it follows the doc\'s Share settings'));
  return html`<div class="drv-group" role="group" aria-label="Google Drive">
    <button type="button" class="btn drv" title="Open the Google Doc" onClick=${open}><${DriveMark} />Open</button>
    <button type="button" class="btn drv" title="Google's Share window: people, link access, copy link" onClick=${share}>Share</button>
    <button type="button" class="btn drv icon" aria-label="Copy link" title="Copy the doc's link" onClick=${copy}><${Icon} name="copy" /></button>
    <button type="button" class="btn drv icon" aria-label="Save again" title="Save again (replaces the doc in the same folder)" onClick=${save}><${Icon} name="refresh" /></button>
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
  document.documentElement.classList.add('print-brief');
  document.body.classList.add('print-brief');
  try {
    const path = await call('meeting_export_pdf', meetingId, title);
    if (path) toast(`Saved the PDF to ${path}`);
  } catch (e) {
    toast(e.message, { kind: 'err' });
  } finally {
    document.body.classList.remove('print-brief');
    document.documentElement.classList.remove('print-brief');
    root.remove();
  }
}
