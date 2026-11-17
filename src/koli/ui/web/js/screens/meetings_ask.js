// screens/meetings_ask.js
// "Ask all meetings": one conversation with the AI about every saved meeting
// (docs/design/meeting-search.md), kept between visits. Its answers mark facts
// with [n]; each marker that points at a real meeting becomes a small button
// that opens that meeting at that second. Modeled on the per-meeting Chat tab.

import { html, useEffect, useRef, useState } from '../../vendor/preact-htm.module.js';
import { call } from '../api.js';
import { confirmModal, textDir, toast } from '../store.js';

const EXAMPLES = ['Where did we talk about pricing?', 'What did we decide about the Enterprise page?', 'Summarize my last call with Shahar'];

function clock(s) { s = Math.max(0, Math.floor(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; }

// The reply with each [n] that has a reference turned into a button; a marker
// without one stays plain text.
function withRefs(text, refs, onOpen) {
  const byNumber = new Map((refs || []).map((r) => [Number(r.n), r]));
  return String(text || '').split(/(\[\d+\])/).map((part) => {
    const marker = /^\[(\d+)\]$/.exec(part);
    const ref = marker && byNumber.get(Number(marker[1]));
    if (!ref) return part;
    return html`<button type="button" class="chip sm acc" style="margin:0 2px;vertical-align:baseline" title=${`${ref.title || ''} · ${clock(ref.at)}`}
      onClick=${() => onOpen(ref.id, ref.at)}>${marker[1]}</button>`;
  });
}

export function AskAll({ initial = '', onOpen }) {
  const [messages, setMessages] = useState(null);
  const [text, setText] = useState('');
  const [waiting, setWaiting] = useState(null);   // the question on its way
  const box = useRef(null);
  const alive = useRef(true);
  useEffect(() => { if (box.current) box.current.scrollTop = box.current.scrollHeight; }, [messages, waiting]);

  const ask = (question) => {
    const q = String(question || '').trim();
    if (!q || waiting) return;
    setWaiting(q); setText('');
    call('meetings_ask', q)
      .then((r) => { if (alive.current) setMessages(r.messages); })
      .catch((e) => { toast(e.message, { kind: 'err' }); if (alive.current) setText(q); })
      .finally(() => { if (alive.current) setWaiting(null); });
  };
  // The saved conversation first; then the question the search box handed over, if any.
  useEffect(() => {
    alive.current = true;
    call('meetings_ask_history').catch(() => []).then((saved) => {
      if (!alive.current) return;
      setMessages(saved || []);
      if (initial && initial.trim()) ask(initial);
    });
    return () => { alive.current = false; };
  }, []);
  const clear = async () => {
    if (await confirmModal({ title: 'Clear this conversation?', body: 'The questions and answers about all your meetings are removed. The meetings stay.', confirm: 'Clear', danger: true })) {
      call('meetings_ask_clear').then(() => setMessages([])).catch((e) => toast(e.message, { kind: 'err' }));
    }
  };
  if (messages == null) return html`<div class="empty">Loading…</div>`;
  return html`<div class="mc">
    <div class="mc-log" ref=${box}>
      ${messages.length === 0 && !waiting && html`<div class="mc-empty">
        <p class="small muted" style="margin:0 0 8px">Ask about everything said in your meetings. The answer points to where it was said.</p>
        <div class="tags">${EXAMPLES.map((x) => html`<button type="button" class="btn ghost sm" onClick=${() => ask(x)}>${x}</button>`)}</div></div>`}
      ${messages.map((msg) => html`<div class=${'mc-msg ' + msg.role}>
        <p dir=${textDir(msg.text)}>${msg.role === 'assistant' ? withRefs(msg.text, msg.refs, onOpen) : msg.text}</p>
      </div>`)}
      ${waiting && html`<div class="mc-msg user"><p dir=${textDir(waiting)}>${waiting}</p></div>
        <div class="mc-msg assistant mc-thinking" role="status"><span class="spin" aria-hidden="true"></span>Thinking…</div>`}
    </div>
    <div class="mc-ask">
      <textarea class="inp" rows="2" dir="auto" value=${text} placeholder="Ask about all your meetings" aria-label="Your question"
        onInput=${(e) => setText(e.currentTarget.value)}
        onKeyDown=${(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(text); } }}></textarea>
      <div class="stack" style="gap:6px">
        <button type="button" class="btn pri" disabled=${!!waiting || !text.trim()} onClick=${() => ask(text)}>Send</button>
        ${messages.length > 0 && html`<button type="button" class="btn ghost sm" onClick=${clear}>Clear</button>`}
      </div>
    </div>
  </div>`;
}
