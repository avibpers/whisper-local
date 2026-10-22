// screens/chat.js
// A meeting's Chat tab: ask the AI about this meeting ("what did we decide
// about pricing?") and it answers from the transcript and the summary. Ask it
// to change the meeting ("Speaker 2 is Ilona", "Ilona was there too", "make
// the summary shorter") and it does, listing what changed under its answer.
// The conversation is kept with the meeting (meeting/chat.py).

import { html, useEffect, useRef, useState } from '../../vendor/preact-htm.module.js';
import { call } from '../api.js';
import { confirmModal, navigate, textDir, toast } from '../store.js';
import { Icon } from '../ui.js';

const EXAMPLES = ['What did we decide?', 'Who took which tasks?', 'Speaker 2 is Ilona', 'Ilona was in the meeting too'];

export function MeetingChat({ meetingId, onChanged }) {
  const [messages, setMessages] = useState(null);
  const [text, setText] = useState('');
  const [waiting, setWaiting] = useState(null);   // the question on its way
  const [ready, setReady] = useState(true);
  const box = useRef(null);
  useEffect(() => {
    setMessages(null);
    call('meeting_chat', meetingId).then(setMessages).catch(() => setMessages([]));
    call('meeting_summary_state', meetingId).then((s) => setReady(s.ai_ready)).catch(() => {});
  }, [meetingId]);
  useEffect(() => { if (box.current) box.current.scrollTop = box.current.scrollHeight; }, [messages, waiting]);

  const ask = (question) => {
    const q = String(question || '').trim();
    if (!q || waiting) return;
    setWaiting(q); setText('');
    call('meeting_chat_ask', meetingId, q)
      .then((r) => { setMessages(r.messages); if (r.changed && onChanged) onChanged(); })
      .catch((e) => { toast(e.message, { kind: 'err' }); setText(q); })
      .finally(() => setWaiting(null));
  };
  const clear = async () => {
    if (await confirmModal({ title: 'Clear this chat?', body: 'The questions and answers go; the meeting keeps any change they made.', confirm: 'Clear', danger: true })) {
      call('meeting_chat_clear', meetingId).then(() => setMessages([])).catch((e) => toast(e.message, { kind: 'err' }));
    }
  };
  if (messages == null) return html`<div class="empty">Loading…</div>`;
  if (!ready) {
    return html`<div class="empty">Choose an AI model first in <a href="#" onClick=${(e) => { e.preventDefault(); navigate('settings/ai'); }}>Settings › AI</a>.</div>`;
  }
  return html`<div class="mc">
    <div class="mc-log" ref=${box}>
      ${messages.length === 0 && !waiting && html`<div class="mc-empty">
        <p class="small muted" style="margin:0 0 8px">Ask about this meeting, or tell the AI what to change: who a speaker is, who else was there, the title, the summary.</p>
        <div class="tags">${EXAMPLES.map((x) => html`<button type="button" class="btn ghost sm" onClick=${() => ask(x)}>${x}</button>`)}</div></div>`}
      ${messages.map((msg) => html`<div class=${'mc-msg ' + msg.role}>
        <p dir=${textDir(msg.text)}>${msg.text}</p>
        ${(msg.changes || []).length > 0 && html`<ul class="mc-changes">${msg.changes.map((c) => html`<li><${Icon} name="check" size="sm" /> ${c}</li>`)}</ul>`}
      </div>`)}
      ${waiting && html`<div class="mc-msg user"><p dir=${textDir(waiting)}>${waiting}</p></div>
        <div class="mc-msg assistant mc-thinking" role="status"><span class="spin" aria-hidden="true"></span>Thinking…</div>`}
    </div>
    <div class="mc-ask">
      <textarea class="inp" rows="2" dir="auto" value=${text} placeholder="Ask about the meeting, or say what to change" aria-label="Your question"
        onInput=${(e) => setText(e.currentTarget.value)}
        onKeyDown=${(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(text); } }}></textarea>
      <div class="stack" style="gap:6px">
        <button type="button" class="btn pri" disabled=${!!waiting || !text.trim()} onClick=${() => ask(text)}>Send</button>
        ${messages.length > 0 && html`<button type="button" class="btn ghost sm" onClick=${clear}>Clear</button>`}
      </div>
    </div>
  </div>`;
}
