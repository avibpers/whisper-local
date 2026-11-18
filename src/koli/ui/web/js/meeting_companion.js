// meeting_companion.js
// Koli in the meeting, as the meeting panel shows her: a mode picker (Just
// listen, Insights, Active), whether she sits in the Google Meet call with
// her own tile, the topic now, cards (a question someone asked with an
// answer you could give, an answer you asked for, a catch-up), the tasks and
// decisions, and the buttons. No text box: the panel never takes the
// keyboard, so "Ask…" opens the meeting in the app window.
// Design: docs/design/meet-participant.md.

import { html, useState } from '../vendor/preact-htm.module.js';
import { call, on } from './api.js';
import { createStore } from './store.js';
import { Icon, dir } from './ui.js';

const MODES = [['off', 'Just listen'], ['insights', 'Insights'], ['active', 'Active']];
const MODE_HELP = {
  off: 'Koli only writes the transcript.',
  insights: 'Koli keeps notes: the topic now, decisions, tasks, open questions.',
  active: 'Insights, and an answer you could give when someone asks a question.',
};
// The guest's states in which she is on her way in or already there.
const ON_HER_WAY = ['installing', 'starting', 'joining', 'asking', 'refused'];

const koli = createStore({
  state: null,     // what services/companion.py published last
  error: '',       // a call that failed, shown as a line with ×
  lists: false,    // the tasks and decisions are opened
  menu: false,     // the mode list is open
});

// Runs a bridge call; a failure becomes a line in the section, never a dialog.
function act(name, ...args) {
  koli.set({ error: '' });
  return call(name, ...args).catch((e) => koli.set({ error: e.message }));
}

export function wireCompanion() {
  on('meeting_companion', (state) => koli.set({ state }));
  call('companion_state').then((state) => koli.set({ state })).catch(() => {});
}

// ── Pieces ────────────────────────────────────────────────────────────────

// The mode, as buttons (a native list may not open in a window without focus).
function ModePick({ mode }) {
  const open = koli.use((s) => s.menu);
  const label = (MODES.find(([value]) => value === mode) || MODES[0])[1];
  const pick = (value) => { koli.set({ menu: false }); act('settings_set', 'meeting.companion.mode', value); };
  return html`<div class="tpl-pick">
    <button type="button" class=${'btn' + (mode === 'off' ? '' : ' pri')} title=${MODE_HELP[mode]} aria-expanded=${open}
      onClick=${() => koli.set({ menu: !open })}>Koli · ${label}<${Icon} name=${open ? 'up' : 'down'} /></button>
    ${open && html`<div class="menu down" role="menu" aria-label="What Koli does in this meeting">
      ${MODES.map(([value, name]) => html`<button type="button" class="mi" role="menuitemradio" aria-checked=${value === mode}
        title=${MODE_HELP[value]} onClick=${() => pick(value)}>${name}${value === mode && html`<span class="grow"></span><${Icon} name="check" />`}</button>`)}
    </div>`}
  </div>`;
}

// Koli as a participant in the call: bring her in, see her on her way, take her out.
function Guest({ guest }) {
  const name = guest.name || 'Koli';
  if (guest.state === 'in_call') {
    return html`<span class="sub" title=${`${name} sits in the call with her own tile and shows her notes there`}>${name} is in the call</span>
      <button type="button" class="btn" onClick=${() => act('companion_leave')}>Take out</button>`;
  }
  if (ON_HER_WAY.includes(guest.state)) {
    const percent = guest.progress != null ? ` ${Math.round(guest.progress * 100)}%` : '';
    return html`<span class="dot busy"></span><span class="sub koli-wrap" role="status">${guest.detail}${percent}</span>
      <button type="button" class="btn" onClick=${() => act('companion_leave')}>Cancel</button>`;
  }
  if (guest.state === 'failed' || guest.state === 'unsupported') {
    return html`<span class="sub koli-wrap" role="status">${guest.detail}</span>
      ${guest.state === 'failed' && guest.can_join && html`<button type="button" class="btn" onClick=${() => act('companion_join')}>Try again</button>`}`;
  }
  if (guest.state === 'leaving') return html`<span class="sub koli-wrap" role="status">${guest.detail}</span>`;
  if (!guest.can_join) return null;      // not a Google Meet call: she has nowhere to sit
  return html`${guest.detail && html`<span class="sub koli-wrap" role="status">${guest.detail}</span>`}<button type="button" class="btn" title=${`${name} joins this Google Meet call as her own participant; everyone sees her tile`}
    onClick=${() => act('companion_join')}><${Icon} name="people" />Bring ${name} in</button>`;
}

function Card({ card, inCall }) {
  const mark = { ask: '❓ ', said: '💬 ', error: '' }[card.kind] || '';
  return html`<div class=${'card ' + card.kind}>
    <div class="card-title" dir=${dir(card.title)}>${mark}${card.title}</div>
    ${card.text && html`<p dir=${dir(card.text)}>${card.text}</p>`}
    ${card.points.length > 0 && html`<ul dir=${dir(card.points[0])}>${card.points.map((p) => html`<li>${p}</li>`)}</ul>`}
    <div class="card-row">
      ${card.kind !== 'error' && html`<button type="button" class="btn" onClick=${() => act('companion_copy', card.text)}><${Icon} name="copy" />Copy</button>`}
      ${inCall && ['ask', 'answer'].includes(card.kind) && html`<button type="button" class="btn"
        title="Koli writes this in the call's chat, for everyone" onClick=${() => act('companion_say', card.id)}>Send to chat</button>`}
      <span class="grow"></span>
      <button type="button" class="btn icon" aria-label="Dismiss" onClick=${() => act('companion_dismiss', card.id)}><${Icon} name="x" /></button>
    </div>
  </div>`;
}

// Your tasks (with × to remove), the tasks Koli noted (with + to take), the decisions.
function Lists({ state }) {
  const mine = state.tasks;
  const taken = new Set(mine.map((t) => t.task.toLowerCase()));
  const noted = state.notes.tasks.map((t, index) => ({ ...t, index })).filter((t) => !taken.has(t.task.toLowerCase()));
  const line = (t) => `${t.task}${t.owner ? ` (${t.owner})` : ''}`;
  return html`<div class="koli-list">
    ${mine.map((t) => html`<div class="koli-item"><span dir=${dir(t.task)}>${line(t)}</span>
      <button type="button" class="btn icon" aria-label="Remove task" onClick=${() => act('companion_task_remove', t.id)}><${Icon} name="x" /></button></div>`)}
    ${noted.map((t) => html`<div class="koli-item noted"><span dir=${dir(t.task)}>${line(t)}</span>
      <button type="button" class="btn icon" aria-label="Keep this task" title="Keep this task in the meeting"
        onClick=${() => act('companion_task_take', t.index)}><${Icon} name="plus" /></button></div>`)}
    ${state.notes.decisions.map((d) => html`<div class="koli-item"><span dir=${dir(d)}>✓ ${d}</span></div>`)}
    ${mine.length + noted.length + state.notes.decisions.length === 0 && html`<span class="sub">No tasks or decisions yet.</span>`}
  </div>`;
}

// ── The section ───────────────────────────────────────────────────────────

export function Companion({ m }) {
  const state = koli.use((s) => s.state);
  const error = koli.use((s) => s.error);
  const lists = koli.use((s) => s.lists);
  const [asked, setAsked] = useState(false);
  if (!state || !['live', 'paused'].includes(m.state)) return null;
  const mode = state.mode;
  const inCall = state.guest.state === 'in_call';
  const notes = state.notes;
  const tasks = state.tasks.length + notes.tasks.filter((t) => !state.tasks.some((mine) => mine.task.toLowerCase() === t.task.toLowerCase())).length;
  const press = (name, ...args) => { setAsked(true); act(name, ...args); };
  return html`<div class="koli">
    <div class="koli-head">
      <${ModePick} mode=${mode} />
      ${state.busy && html`<span class="dot busy" title="Koli is thinking"></span>`}
      <span class="grow"></span>
      <${Guest} guest=${state.guest} />
    </div>
    ${error && html`<div class="notice"><span class="grow koli-wrap" role="alert">${error}</span>
      <button type="button" class="btn icon" aria-label="Dismiss" onClick=${() => koli.set({ error: '' })}><${Icon} name="x" /></button></div>`}
    ${mode !== 'off' && html`<div class="koli-body">
      ${!state.ready && html`<div class="notice"><span class="grow koli-wrap">Koli needs an AI model for this.</span>
        <button type="button" class="btn" onClick=${() => call('window_open', 'settings/ai')}>Choose one</button></div>`}
      ${notes.topic
        ? html`<p class="koli-now" dir=${dir(notes.topic)}><span class="sub">Now</span> ${notes.topic}</p>`
        : state.ready && html`<p class="koli-now"><span class="sub">Koli is listening. Her notes start after a few sentences.</span></p>`}
      ${asked && notes.summary.length > 0 && html`<ul class="koli-sum" dir=${dir(notes.summary[0])}>${notes.summary.map((s) => html`<li>${s}</li>`)}</ul>`}
      ${state.cards.map((card) => html`<${Card} key=${card.id} card=${card} inCall=${inCall} />`)}
      <button type="button" class="btn koli-count" aria-expanded=${lists} onClick=${() => koli.set({ lists: !lists })}>
        Tasks ${tasks} · Decisions ${notes.decisions.length}<${Icon} name=${lists ? 'up' : 'down'} /></button>
      ${lists && html`<${Lists} state=${state} />`}
    </div>
    <div class="koli-acts">
      <button type="button" class="btn" title="Bring the notes up to this second" onClick=${() => press('companion_now')}>What now?</button>
      <button type="button" class="btn" title="What is being asked, and something you could say" onClick=${() => act('companion_answer')}>What should I answer?</button>
      <button type="button" class="btn" title="The last five minutes in three lines" onClick=${() => act('companion_catch_up', 5)}>Catch me up</button>
      <button type="button" class="btn" title="Keep the task being talked about right now" onClick=${() => act('companion_task')}><${Icon} name="plus" />Task</button>
      <button type="button" class="btn" title="Type a question about this meeting in the app window" onClick=${() => call('window_open', `meetings/${m.id}`)}>Ask…</button>
    </div>`}
  </div>`;
}
