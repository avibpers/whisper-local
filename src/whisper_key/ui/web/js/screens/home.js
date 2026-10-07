// screens/home.js
// Simple Home: how to talk (the record keys, big), a box to try it, the last
// thing you said with Copy / Paste again / fix a word, three tips, and today's
// count. No engine internals.

import { html, useEffect, useState } from '../../vendor/preact-htm.module.js';
import { call } from '../api.js';
import { app, prettyKeys, textDir, toast, useSetting } from '../store.js';
import { Icon } from '../ui.js';
import { fixAWord } from './live.js';

export function HomeScreen() {
  const keys = prettyKeys(useSetting('hotkey.recording_hotkey'));
  const mode = useSetting('hotkey.recording_mode');
  const pipeline = app.use((s) => s.pipeline);
  const recent = app.use((s) => s.recent);
  const st = app.use((s) => s.state);
  const [today, setToday] = useState(null);
  const delivered = pipeline.find((p) => p.stage === 'delivered');
  const last = (delivered && delivered.text) || recent[0] || '';
  useEffect(() => { call('insights', 1).then((d) => setToday(d.today)).catch(() => {}); }, [!!delivered]);
  return html`<div class="page narrow">
    <section class="card" style="padding:32px;display:flex;flex-direction:column;align-items:center;text-align:center;gap:18px" aria-label="How to talk">
      <h1 style="margin:0;font-size:28px;font-weight:600">${mode === 'toggle' ? 'Press these keys, talk, press again' : 'Hold these keys and talk'}</h1>
      <div class="row-flex" style="gap:12px">${keys.map((k, i) => html`${i > 0 && html`<span style="font-size:22px" class="muted">+</span>`}
        <span class="kbd" style="min-width:72px;height:56px;justify-content:center;font-size:18px;border-bottom-width:4px;border-radius:12px">${k}</span>`)}</div>
      <p class="muted" style="margin:0;max-width:520px">${mode === 'toggle' ? 'Your words appear wherever you were typing' : 'Let go, and your words appear wherever you were typing'}: email, chat, documents. It works in every app.</p>
      ${st.state === 'recording' && html`<span class="chip rec"><span class="dot"></span>Listening…</span>`}
      <label style="width:100%;max-width:560px;display:flex;flex-direction:column;gap:6px;text-align:start">
        <span class="small" style="font-weight:500;color:var(--ink2)">Try it here</span>
        <textarea class="inp" rows="3" dir="auto" placeholder="Click here, hold your keys, and say something…" style="font-size:15px;background:var(--bg)"></textarea>
      </label>
    </section>
    <section class="card pad stack" aria-label="Last thing you said">
      <div class="row-flex" style="justify-content:space-between"><span class="card-title">Last thing you said</span>
        ${delivered && delivered.app && html`<span class="small muted">${delivered.app}</span>`}</div>
      ${last ? html`<p dir=${textDir(last)} style="margin:0;font-size:16px;white-space:pre-wrap">${last}</p>
        <div class="row-flex">
          <button type="button" class="btn" onClick=${() => call('copy_text', last).then(() => toast('Copied'))}><${Icon} name="copy" />Copy</button>
          <button type="button" class="btn" onClick=${() => call('paste_last').catch((e) => toast(e.message, { kind: 'err' }))}>Paste it again</button>
          <button type="button" class="btn ghost" onClick=${() => fixAWord(last)}>A word was wrong?</button></div>`
      : html`<p class="muted" style="margin:0">Nothing yet. Try the box above.</p>`}
    </section>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px">
      <div class="card pad stack" style="gap:6px"><b>Say the punctuation</b><span class="muted small">“comma”, “period”, “new line”.</span></div>
      <div class="card pad stack" style="gap:6px"><b>Start a new line</b><span class="muted small">Say “new line”, or “new paragraph” for a blank line between.</span></div>
      <div class="card pad stack" style="gap:6px"><b>Names it misses?</b><span class="muted small"><a href="#/vocabulary">Teach it your words</a> once and it remembers.</span></div>
    </div>
    ${today && html`<div class="row-flex muted" style="gap:24px;font-size:13.5px">
      <span><b style="color:var(--ink)">${today.count}</b> times today</span>
      <span><b style="color:var(--ink)">${Math.round(today.minutes_saved)} minutes</b> of typing saved</span></div>`}
  </div>`;
}
