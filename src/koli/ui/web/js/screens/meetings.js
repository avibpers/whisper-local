// screens/meetings.js
// Meetings: the one running now (with the panel's controls), the saved ones,
// and a transcript view with speaker colours, rename, export and delete.

import { html, useEffect, useRef, useState } from '../../vendor/preact-htm.module.js';
import { call, on } from '../api.js';
import { app, confirmModal, promptModal, textDir, toast } from '../store.js';
import { Icon, Select } from '../ui.js';
import { ExtraInstall } from './shared.js';

const COLOURS = ['#0A6E82', '#B45309', '#6D28D9', '#15803D', '#BE185D', '#0369A1', '#C2410C', '#4D7C0F'];
const RUNNING = new Set(['live', 'paused', 'offered']);
function clock(s) { s = Math.max(0, Math.floor(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; }
function when(iso) { return (iso || '').replace('T', ' ').slice(0, 16); }

// The meeting running now, with the same controls as the panel.
function LiveCard({ m }) {
  if (!m || !RUNNING.has(m.state)) return null;
  const paused = m.state === 'paused';
  return html`<section class="card pad stack" aria-label="Meeting now">
    <div class="row-flex" style="justify-content:space-between">
      <span class="card-title"><span class="chip rec">${m.state === 'offered' ? 'Call detected' : paused ? 'Paused' : 'Live'}</span> ${m.title || 'Meeting'}</span>
      <span class="small muted">${m.turns} turns${m.behind_s > 5 ? ` · ${Math.round(m.behind_s)} s behind` : ''}</span></div>
    ${m.state === 'offered' ? html`<div class="row-flex"><button type="button" class="btn pri" onClick=${() => call('meeting_offer', true)}>Start transcribing</button>
        <button type="button" class="btn" onClick=${() => call('meeting_offer', false)}>Not now</button></div>`
      : html`<div class="row-flex">
        <button type="button" class=${'btn' + (m.excluded ? ' pri' : '')} onClick=${() => call('meeting_exclude', !m.excluded)}>${m.excluded ? 'Include me' : 'Exclude me'}</button>
        <button type="button" class="btn" onClick=${() => call(paused ? 'meeting_resume' : 'meeting_pause')}>${paused ? 'Resume' : 'Pause'}</button>
        <button type="button" class="btn" onClick=${() => call('meeting_new')}>New meeting</button>
        ${m.can_continue && html`<button type="button" class="btn" onClick=${() => call('meeting_continue_previous')}>Continue previous instead</button>`}
        ${m.can_split && html`<button type="button" class="btn" onClick=${() => call('meeting_split')}>Start new instead</button>`}
        <span class="grow"></span>
        <button type="button" class="btn danger" onClick=${() => call('meeting_end')}>End meeting</button></div>`}
  </section>`;
}

function Transcript({ id, live, onDeleted }) {
  const [m, setM] = useState(null);
  const box = useRef(null);
  const load = () => call('meeting_get', id).then(setM).catch((e) => toast(e.message, { kind: 'err' }));
  useEffect(() => { setM(null); load(); }, [id]);
  useEffect(() => on('meeting_turn', (p) => {
    if (p.id !== id) return;
    if (p.reload) { load(); return; }
    if (p.turn) setM((cur) => cur && { ...cur, turns: [...cur.turns, p.turn] });
  }), [id]);
  useEffect(() => on('meeting', (s) => {
    if (s.id === id && s.speakers) setM((cur) => cur && { ...cur, speakers: s.speakers.map((sp) => ({ ...sp })) });
  }), [id]);
  useEffect(() => { if (live && box.current) box.current.scrollTop = box.current.scrollHeight; });
  if (!m) return html`<div class="empty">Loading…</div>`;
  const names = Object.fromEntries(m.speakers.map((s) => [s.id, s]));
  const who = (sid) => names[sid] || { id: sid, name: sid === '?' ? 'Speaker ?' : sid, colour: 7 };
  const renameSpeaker = async (sp) => {
    const name = await promptModal({ title: 'Rename speaker', label: 'Name', value: sp.name, confirm: 'Rename' });
    if (name) call('meeting_rename', sp.id, name, id).then(load).catch((e) => toast(e.message, { kind: 'err' }));
  };
  const exportAs = (fmt) => call('meeting_export', id, fmt).then((p) => p && toast(`Exported to ${p}`)).catch((e) => toast(e.message, { kind: 'err' }));
  const last = m.turns.length ? m.turns[m.turns.length - 1].end : 0;
  return html`<section class="card" style="min-width:0;overflow:hidden;display:flex;flex-direction:column;max-height:calc(100vh - 200px)" aria-label="Transcript">
    <div class="row-flex" style="padding:12px 18px;border-bottom:1px solid var(--line)">
      <div class="grow"><div style="font-weight:600">${m.title}</div>
        <div class="small muted">${when(m.started_at)} · ${clock(last)} · ${m.source && m.source.app ? m.source.app.replace('.exe', '') : 'started by hand'}</div></div>
      <div class="row-flex">${m.speakers.map((sp) => html`<button type="button" class="chip" style=${`color:${COLOURS[sp.colour % 8]}`} title="Rename" onClick=${() => renameSpeaker(sp)}>${sp.name}</button>`)}</div>
      <${Select} label="Export" value="" options=${[['', 'Export…'], ['txt', 'As .txt'], ['md', 'As .md']]} onChange=${(v) => v && exportAs(v)} />
      ${!live && html`<button type="button" class="btn ghost icon danger" aria-label="Delete" onClick=${async () => {
        if (await confirmModal({ title: 'Delete this meeting?', body: 'The transcript is removed from this computer.', confirm: 'Delete', danger: true })) {
          call('meeting_delete', id).then(() => { toast('Meeting deleted'); onDeleted(); }).catch((e) => toast(e.message, { kind: 'err' }));
        }
      }}><${Icon} name="trash" /></button>`}
    </div>
    <div ref=${box} style="overflow-y:auto;padding:8px 18px 16px">
      ${m.turns.length === 0 && html`<div class="empty">${live ? 'Listening…' : 'Nothing was said.'}</div>`}
      ${m.turns.map((t) => { const sp = who(t.speaker); return html`<div style="display:grid;grid-template-columns:56px 130px minmax(0,1fr);gap:10px;padding:6px 0;align-items:start">
        <span class="mono small muted">${clock(t.start)}</span>
        <button type="button" class="btn ghost sm" style=${`justify-content:flex-start;color:${COLOURS[sp.colour % 8]};font-weight:600`} onClick=${() => renameSpeaker(sp)}>${sp.name}</button>
        <p dir=${textDir(t.text)} style="margin:0;white-space:pre-wrap">${t.text}</p></div>`; })}
    </div>
  </section>`;
}

// Every voice heard on a call is saved with its own id when the meeting ends,
// so later meetings recognise it, named or not. Renaming a speaker names its
// voice. Forgetting one deletes its voiceprint.
function KnownVoices() {
  const [voices, setVoices] = useState([]);
  const load = () => call('meeting_voices').then(setVoices).catch(() => {});
  useEffect(() => { load(); }, []);
  useEffect(() => on('meeting', load), []);
  const label = (v) => v.name || `Voice ${v.id.slice(2, 6)}`;
  const forget = async (v) => {
    if (await confirmModal({ title: `Forget ${label(v)}?`, body: 'Its voiceprint is deleted; later meetings no longer recognise it. Past meetings keep their labels.', confirm: 'Forget', danger: true })) {
      call('meeting_forget_voice', v.id).then(load).catch((e) => toast(e.message, { kind: 'err' }));
    }
  };
  return html`<section class="card pad stack" aria-label="Known voices">
    <span class="card-title">Known voices</span>
    ${voices.length === 0
      ? html`<span class="small muted">Voices from your calls are saved when a meeting ends, so later meetings recognise them.</span>`
      : html`<div class="tags">${voices.map((v) => html`<span class="tag" dir="auto"
          title=${`${v.id} · ${v.meetings.length} meeting${v.meetings.length === 1 ? '' : 's'} · last heard ${when(v.last_seen)}`}>${label(v)}
          <button type="button" aria-label=${`Forget ${label(v)}`} onClick=${() => forget(v)}>×</button></span>`)}</div>`}
  </section>`;
}

export function MeetingsScreen() {
  const live = app.use((s) => s.meeting);
  const [list, setList] = useState([]);
  const [selected, setSelected] = useState(null);
  const reload = () => call('meetings_list').then(setList).catch(() => {});
  useEffect(() => { reload(); }, [live.state]);
  const liveId = live.id && (live.state === 'live' || live.state === 'paused') ? live.id : null;
  const current = selected || liveId || (list[0] && list[0].id);
  return html`<div class="page">
    <div class="page-head">
      <div><h1 class="page-title">Meetings</h1><p class="page-sub">Calls transcribed as they happen, with who said what. Stored only on this computer.</p></div>
      ${!RUNNING.has(live.state) && html`<button type="button" class="btn pri" onClick=${() => call('meeting_start').catch((e) => toast(e.message, { kind: 'err' }))}>Start a meeting now</button>`}
    </div>
    <${ExtraInstall} name="loopback" />
    <${LiveCard} m=${live} />
    <div style="display:grid;grid-template-columns:minmax(260px,320px) minmax(0,1fr);gap:16px;align-items:start">
      <div class="stack">
        <section class="card" style="overflow:hidden" aria-label="Past meetings">
          ${list.length === 0 && html`<div class="empty">No meetings yet.</div>`}
          ${list.map((h) => html`<button type="button" class="menu-item" style=${`display:block;width:100%;text-align:start;padding:12px 16px;border-top:1px solid var(--line)${current === h.id ? ';background:var(--sunk)' : ''}`} onClick=${() => setSelected(h.id)}>
            <div style="font-weight:600">${h.title}${liveId === h.id ? html` <span class="chip rec">Live</span>` : ''}</div>
            <div class="small muted">${when(h.started_at)} · ${clock(h.duration_s)} · ${h.speakers.join(', ')}</div></button>`)}
        </section>
        <${KnownVoices} />
      </div>
      ${current ? html`<${Transcript} id=${current} live=${liveId === current} onDeleted=${() => { setSelected(null); reload(); }} />` : html`<div class="empty">Pick a meeting.</div>`}
    </div>
  </div>`;
}
