// screens/training.js
// Training: Koli learns your voice from the dictations you already make, with
// nothing for you to do. This screen is the switch and the disk cap, what is
// kept and how much space it takes, what Koli noticed this week, the recent
// clips (play one, or Test how the night model hears it, without learning
// anything), and the delete buttons. The work happens in learning/ and services/learning.py.
// Design: docs/design/self-learning.md.

import { html, useEffect, useState } from '../../vendor/preact-htm.module.js';
import { call } from '../api.js';
import { app, confirmModal, fmtBytes, textDir, toast, useSetting } from '../store.js';
import { Icon, Segmented } from '../ui.js';
import { SettingRow, TaskProgress, settingsBySection, useRunningTasks } from './shared.js';

const size = (n) => (n ? fmtBytes(n) : '0 MB');
const fmtDate = (ts) => (ts ? new Date(ts).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }) : '—');
const LANGUAGE = { en: 'English', he: 'Hebrew', ar: 'Arabic', '?': 'Unknown' };

// ── The switch and the cap ──────────────────────────────────────────────────

function Settings() {
  const schema = app.use((s) => s.schema);
  const rows = settingsBySection(schema, 'learning');
  return html`<section class="card" aria-labelledby="h-learn-on">
    <div class="card-head"><div><h2 id="h-learn-on">Learn from my dictations</h2>
      <p>Koli keeps your clips on this computer and learns from them at night. You never have to train it.</p></div></div>
    ${rows.map((s) => html`<${SettingRow} s=${s} />`)}
  </section>`;
}

// ── Disk ────────────────────────────────────────────────────────────────────

function Storage({ st, onOpen }) {
  const share = st.cap_bytes ? Math.min(1, st.used_bytes / st.cap_bytes) : 0;
  return html`<section class="card pad stack" aria-labelledby="h-learn-disk">
    <div class="row-flex" style="justify-content:space-between"><span class="card-title" id="h-learn-disk">Disk</span>
      <button type="button" class="btn ghost sm" onClick=${onOpen}><${Icon} name="folder" size="sm" />Open folder</button></div>
    <div><span class="stat">${size(st.used_bytes)}</span> <span class="muted">of ${size(st.cap_bytes)}</span></div>
    <div class="bar-meter" role="meter" aria-label="Disk used" aria-valuemin="0" aria-valuemax="100" aria-valuenow=${Math.round(share * 100)}>
      <span style=${`transform:scaleX(${share});background:${share > 0.9 ? 'var(--warn)' : 'var(--acc)'}`}></span></div>
    <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px">
      ${[[st.clips.toLocaleString(), 'clips with audio'], [`${st.audio_hours} h`, 'of your speech'],
        [fmtDate(st.oldest), 'oldest clip'], [Object.entries(st.by_language).map(([l, n]) => `${LANGUAGE[l] || l} ${n}`).join(' · ') || '—', 'by language']]
        .map(([v, l]) => html`<div style="min-width:0"><div class="stat" style="font-size:17px;overflow-wrap:anywhere">${v}</div><div class="small muted">${l}</div></div>`)}
    </div>
    <p class="small muted" style="margin:0">When the space is full, the clips Koli needs least go first. What it learned from them stays.</p>
  </section>`;
}

// ── What Koli noticed this week ─────────────────────────────────────────────
// Free signals: nobody checks anything, they come from how you use Koli.

function Week({ w }) {
  return html`<section class="card pad stack" aria-labelledby="h-learn-week">
    <span class="card-title" id="h-learn-week">This week</span>
    <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px">
      ${[[w.typed, 'typed'], [w.dropped, 'dropped as noise'],
        [w.said_again, 'you said again'], [w.after_drop, 'said again after a drop']]
        .map(([v, l]) => html`<div><div class="stat">${v}</div><div class="small muted">${l}</div></div>`)}
    </div>
    <p class="small muted" style="margin:0">“Said again” means you repeated a dictation within half a minute in the same window: a hint that Koli got the first one wrong.</p>
  </section>`;
}

// ── Recent clips: play, and Test with the night model ───────────────────────

const fmtTime = (ts) => (ts ? new Date(ts).toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '');
const KIND = { hotkey: 'hotkey', auto: 'text field', continuous: 'continuous' };
const VERDICT = {
  same: ['ok', 'The night model heard the same'],
  different: ['warn', 'The night model heard it differently'],
  speech: ['warn', 'Real speech: Koli dropped it'],
  noise: ['ok', 'Noise: dropping it was right'],
};
const PAGE = 20;

// One clip player for the whole list: a second Play stops the first.
let player = null;
function usePlayer() {
  const [playing, setPlaying] = useState(null);
  const stop = () => { if (player) { player.pause(); player = null; } setPlaying(null); };
  const play = async (id) => {
    if (playing === id) { stop(); return; }
    stop();
    try {
      const { url } = await call('learning_audio', id);
      player = new Audio(url);
      player.onended = () => setPlaying(null);
      await player.play();
      setPlaying(id);
    } catch (e) { toast(e.message, { kind: 'err' }); setPlaying(null); }
  };
  useEffect(() => stop, []);
  return [playing, play];
}

function NightResult({ t }) {
  const [tone, label] = VERDICT[t.verdict] || ['', ''];
  return html`<div class="stack" style="gap:4px;padding:8px 10px;border-radius:10px;background:var(--sunk)">
    <div class="small"><span class="muted">Night:</span>${' '}
      <span dir=${textDir(t.text)}>${t.words.length ? t.words.map((w, i) => html`${i ? ' ' : ''}${w.changed
        ? html`<mark class="night-diff">${w.text}</mark>` : w.text}`)
        : html`<i class="muted">no words</i>`}</span></div>
    <div class="small" style=${`color:var(--${tone || 'mute'})`}>${label}
      <span class="muted"> · ${t.model} · ${t.seconds}s · word confidence ${Math.round(t.mean_prob * 100)}%</span></div>
  </div>`;
}

function Clip({ c, playing, onPlay, onTested }) {
  const [busy, setBusy] = useState(false);
  const [needs, setNeeds] = useState(null);
  const downloads = useRunningTasks('download').filter((t) => needs && needs.some((n) => n.key === t.ref));
  const dropped = c.outcome.startsWith('dropped');
  const test = () => {
    setBusy(true);
    call('learning_test', c.id).then((r) => { if (r.needs) setNeeds(r.needs); else { setNeeds(null); onTested(c.id, r); } })
      .catch((e) => toast(e.message, { kind: 'err' })).finally(() => setBusy(false));
  };
  const download = () => needs.forEach((n) => call('model_download', n.key).catch((e) => toast(e.message, { kind: 'err' })));
  const tags = [fmtTime(c.ts), c.app.replace(/\.exe$/i, ''), `${c.seconds}s`, KIND[c.kind] || c.kind,
    c.signals.repeats && 'said again', c.signals.after_drop && 'after a drop'].filter(Boolean);
  return html`<div class="stack" style="gap:6px;padding:12px 18px;border-top:1px solid var(--line)">
    <div class="row-flex" style="gap:10px;align-items:center">
      <button type="button" class="btn ghost sm" disabled=${!c.has_audio} aria-label=${playing ? 'Stop' : 'Play the clip'}
        title=${c.has_audio ? '' : 'The audio was deleted to save space'} onClick=${() => onPlay(c.id)}>
        <${Icon} name=${playing ? 'pause' : 'play'} size="sm" /></button>
      <span class="small muted grow" style="min-width:0">${tags.join(' · ')}</span>
      ${dropped && html`<span class="chip" style="color:var(--warn)" title=${c.why}>dropped</span>`}
      <button type="button" class="btn sm" disabled=${busy || !c.has_audio} onClick=${test}>${busy ? 'Listening…' : 'Test'}</button>
    </div>
    <div class="small"><span class="muted">${dropped ? 'Heard:' : 'Typed:'}</span>${' '}
      <span dir=${textDir(c.typed || c.heard)}>${c.typed || c.heard || html`<i class="muted">nothing</i>`}</span>
      ${dropped && c.why && html`<span class="muted"> · ${c.why}</span>`}</div>
    ${c.test && html`<${NightResult} t=${c.test} />`}
    ${needs && html`<div class="notice info" style="align-items:center"><span class="grow">The night model isn't on this computer yet:
      ${needs.map((n) => n.label).join(', ')}. It downloads once.</span>
      ${downloads.length ? html`<${TaskProgress} task=${downloads[0]} />` : html`<button type="button" class="btn sm" onClick=${download}>Download</button>`}</div>`}
  </div>`;
}

function RecentClips({ refresh }) {
  const [show, setShow] = useState('all');
  const [data, setData] = useState({ total: 0, items: [] });
  const [limit, setLimit] = useState(PAGE);
  const [playing, play] = usePlayer();
  const load = () => call('learning_recent', limit, 0, show).then(setData).catch(() => {});
  useEffect(() => { load(); }, [show, limit, refresh]);
  const tested = (id, test) => setData((d) => ({ ...d, items: d.items.map((c) => (c.id === id ? { ...c, test } : c)) }));
  return html`<section class="card" aria-labelledby="h-learn-recent">
    <div class="card-head"><div><h2 id="h-learn-recent">Recent clips</h2>
      <p>Play what you said, and press Test to hear it with the night model. Nothing is learned from a test.</p></div>
      <${Segmented} label="Show" value=${show} onChange=${(v) => { setShow(v); setLimit(PAGE); }}
        options=${[['all', 'All'], ['typed', 'Typed'], ['dropped', 'Dropped'], ['again', 'Said again']]} /></div>
    ${data.items.length === 0 ? html`<div class="empty">No clips here yet.</div>`
      : data.items.map((c) => html`<${Clip} key=${c.id} c=${c} playing=${playing === c.id} onPlay=${play} onTested=${tested} />`)}
    ${data.total > data.items.length && html`<div style="padding:10px 18px;border-top:1px solid var(--line)">
      <button type="button" class="btn ghost sm" onClick=${() => setLimit(limit + PAGE)}>Show more (${data.total - data.items.length} left)</button></div>`}
  </section>`;
}

// ── Deleting ────────────────────────────────────────────────────────────────

function Delete({ onDeleted }) {
  const [from, setFrom] = useState('');
  const [to, setTo] = useState('');
  const run = async (title, body, action) => {
    if (!(await confirmModal({ title, body, confirm: 'Delete', danger: true }))) return;
    try {
      const n = await action();
      toast(n ? `Deleted ${n} clip${n === 1 ? '' : 's'}` : 'Nothing to delete');
      onDeleted();
    } catch (e) { toast(e.message, { kind: 'err' }); }
  };
  const recent = (span, label) => run(`Delete the ${label}?`, `Every clip kept in the ${label} goes, audio and record.`,
    () => call('learning_delete_recent', span));
  return html`<section class="card pad stack" aria-labelledby="h-learn-del">
    <span class="card-title" id="h-learn-del">Delete clips</span>
    <div class="row-flex" style="flex-wrap:wrap;gap:8px">
      <button type="button" class="btn" onClick=${() => recent('hour', 'last hour')}>Last hour</button>
      <button type="button" class="btn" onClick=${() => recent('day', 'last day')}>Last day</button>
      <button type="button" class="btn" onClick=${() => recent('week', 'last week')}>Last week</button>
    </div>
    <div class="row-flex" style="flex-wrap:wrap;gap:8px;align-items:center">
      <input class="inp" type="date" id="learn-from" aria-label="From" value=${from} onInput=${(e) => setFrom(e.currentTarget.value)} />
      <span class="muted">to</span>
      <input class="inp" type="date" id="learn-to" aria-label="To" value=${to} onInput=${(e) => setTo(e.currentTarget.value)} />
      <button type="button" class="btn" disabled=${!from || !to}
        onClick=${() => run('Delete these days?', `Every clip from ${from} to ${to} goes.`, () => call('learning_delete_range', from, to))}>Delete range</button>
    </div>
    <div><button type="button" class="btn danger" onClick=${() => run('Delete every clip?',
      'All clips and their records go. Turn off “Learn from my dictations” too if you want Koli to stop keeping new ones.',
      () => call('learning_delete_all'))}><${Icon} name="trash" size="sm" />Delete everything</button></div>
  </section>`;
}

// ── The screen ──────────────────────────────────────────────────────────────

export function TrainingScreen() {
  const [st, setSt] = useState(null);
  const [error, setError] = useState('');
  const on = useSetting('learning.enabled');
  const delivered = app.use((s) => s.pipeline.filter((p) => p.stage === 'delivered' || p.stage === 'empty').length);
  const [version, setVersion] = useState(0);   // bumps after a delete, so the list reloads too
  const load = () => call('learning_state').then((s) => { setSt(s); setError(''); }).catch((e) => setError(e.message));
  const deleted = () => { load(); setVersion((v) => v + 1); };
  useEffect(() => { load(); }, [on, delivered]);
  const open = () => call('learning_open_folder').catch((e) => toast(e.message, { kind: 'err' }));

  return html`<div class="page narrow">
    <div class="page-head"><div><h1 class="page-title">Training</h1>
      <p class="page-sub">You talk. Koli learns at night. Nothing for you to check or train.</p></div></div>
    <${Settings} />
    ${error && html`<div class="notice warn"><${Icon} name="alert" /><span>${error}</span></div>`}
    ${!on && st && st.records === 0 && html`<div class="notice info"><${Icon} name="lock" />
      <span>Off. Nothing is kept until you turn it on. Voice commands, meetings and apps you exclude on the Apps screen are never kept.</span></div>`}
    ${st && html`<div class="grid-cards"><${Storage} st=${st} onOpen=${open} /><${Week} w=${st.week} /></div>
      <div class="notice info"><${Icon} name="refresh" /><span>Koli is collecting clips now. Re-listening at night and learning from them arrive in the next update; Test below shows how the night model hears a clip today.</span></div>
      <${RecentClips} refresh=${`${delivered}:${version}`} />
      <${Delete} onDeleted=${deleted} />`}
  </div>`;
}
