// agent.js
// The conversation with the agent (agent/chat.py): what you said and what Koli
// did, newest at the bottom, a status line while it works, and Approve or
// Cancel when it waits for you. It listens for you without the wake word and
// closes after you're quiet a while, or with ×. The whole window drags.

import { html, render, useEffect, useRef } from '../vendor/preact-htm.module.js';
import { call, on } from './api.js';
import { createStore } from './store.js';
import { Icon } from './ui.js';

const chat = createStore({ id: 0, open: false, state: 'closed', status: '', turns: [], closes_after: 20,
                           approve: null });   // approve: the step waiting for a click

function dir(text) { return /[֐-׿؀-ۿ]/.test(text || '') ? 'rtl' : 'ltr'; }

function wire() {
  on('chat', (p) => {
    const s = chat.get();
    if (p.event === 'open') chat.set({ ...p, open: true, approve: null });
    else if (p.id !== s.id) return;
    else if (p.event === 'turn') chat.set({ turns: [...s.turns, p.turn].slice(-60) });
    else if (p.event === 'status') chat.set({ state: p.state, status: p.status, closes_after: p.closes_after || s.closes_after });
    else if (p.event === 'close') chat.set({ open: false, state: 'closed', status: p.why === 'quiet' ? 'Closed after a quiet while' : 'Closed', approve: null });
  });
  // A step that waits for your click: Approve here, like the bubble's.
  on('agent', (a) => {
    if (!chat.get().open) return;
    if (a.phase === 'approve' && a.needs === 'click') chat.set({ approve: a });
    else if (!['approve'].includes(a.phase)) chat.set({ approve: null });
  });
}

function Turn({ t }) {
  return html`<div class=${`turn ${t.who} ${t.kind || ''}`} dir=${dir(t.text)}>${t.text}
    ${t.lines && t.lines.length > 0 && html`<ul class="lines">${t.lines.slice(0, 6).map((l) => html`<li dir=${dir(l)}>${l}</li>`)}</ul>`}
  </div>`;
}

function Chat() {
  const s = chat.use();
  const list = useRef(null);
  useEffect(() => { if (list.current) list.current.scrollTop = list.current.scrollHeight; }, [s.turns.length, s.status]);
  const line = s.state === 'working' ? (s.status || 'Working…')
    : s.open ? `Listening · closes after ${Math.round(s.closes_after)} s of quiet` : s.status;
  return html`<div class="chat">
    <div class="head">
      <span class=${`dot ${s.state}`}></span><span class="title">Koli</span>
      <button type="button" class="icon-btn" aria-label="Close the conversation" title="Close"
        onClick=${() => call('agent_chat_close')}><${Icon} name="x" /></button>
    </div>
    <div class="turns" ref=${list} role="log" aria-live="polite">
      ${s.turns.length ? s.turns.map((t, i) => html`<${Turn} key=${i} t=${t} />`)
        : html`<p class="empty">Say what you need. Koli runs your commands, and asks the AI for anything else.</p>`}
    </div>
    <div class="foot">
      <span class="status" role="status">${line}</span>
      ${s.approve && html`<button type="button" class="btn pri" onClick=${() => call('agent_approve')}>Approve</button>`}
      ${s.state === 'working' && html`<button type="button" class="btn" onClick=${() => call('agent_cancel')}>Cancel</button>`}
    </div>
  </div>`;
}

async function boot() {
  wire();
  render(html`<${Chat} />`, document.getElementById('root'));
  try {
    const now = await call('agent_chat_state');
    if (now && now.open) chat.set(now);
  } catch (e) { /* the conversation starts empty */ }
}
boot();
