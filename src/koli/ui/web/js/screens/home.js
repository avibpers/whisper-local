// screens/home.js
// Simple Home: the round talk button (press, talk, press again; the words land
// in the "Try it here" box), the record keys for every other app, the last
// thing you said with Copy / Paste again / fix a word, three tips, and today's
// count. No engine internals.

import { html, useEffect, useRef, useState } from '../../vendor/preact-htm.module.js';
import { call } from '../api.js';
import { app, live, prettyKeys, textDir, toast, useSetting } from '../store.js';
import { Icon } from '../ui.js';
import { fixAWord } from './live.js';

// The talk button. Its rings grow with your voice while it listens.
function TalkButton({ practice }) {
  const st = app.use((s) => s.state);
  const level = live.use((s) => s.level);
  const listening = st.state === 'recording';
  const ring = listening ? Math.min(1, 0.8 + level * 3) : 0.78;
  const press = () => {
    if (listening) return call('dictation_stop', false).catch((e) => toast(e.message, { kind: 'err' }));
    // The words are typed where the cursor is: put it in the practice box.
    if (practice.current) practice.current.focus();
    return call('dictation_start').catch((e) => toast(e.message, { kind: 'err' }));
  };
  return html`<div class="orb-wrap" style=${`--ring:${ring.toFixed(3)}`}>
    <span class="orb-ring"></span><span class="orb-ring two"></span>
    <button type="button" class=${'orb' + (listening ? ' live' : '')} onClick=${press} disabled=${st.state === 'processing'}
      aria-label=${listening ? 'Stop and write' : 'Press to talk'}>
      ${listening ? html`<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="2.5"></rect></svg>`
        : html`<${Icon} name="mic" />`}</button>
  </div>`;
}

export function HomeScreen() {
  const keys = prettyKeys(useSetting('hotkey.recording_hotkey'));
  const mode = useSetting('hotkey.recording_mode');
  const language = useSetting('whisper.language');
  const pipeline = app.use((s) => s.pipeline);
  const recent = app.use((s) => s.recent);
  const st = app.use((s) => s.state);
  const practice = useRef(null);
  const [today, setToday] = useState(null);
  const delivered = pipeline.find((p) => p.stage === 'delivered');
  const last = (delivered && delivered.text) || recent[0] || '';
  useEffect(() => { call('insights', 1).then((d) => setToday(d.today)).catch(() => {}); }, [!!delivered]);
  const status = st.state === 'recording' ? 'Listening… press again when you are done'
    : st.state === 'processing' ? 'Writing it down…' : st.paused ? 'Paused' : 'Ready';
  const langName = { auto: 'any language', he: 'Hebrew', en: 'English' }[language || 'auto'] || language;
  return html`<div class="page">
    <section class="card" style="padding:36px;display:flex;flex-wrap:wrap;align-items:center;gap:32px" aria-label="Talk">
      <${TalkButton} practice=${practice} />
      <div style="flex:1 1 340px;min-width:0;display:flex;flex-direction:column;gap:14px">
        <span class="row-flex small" style="color:var(--ink2)"><span class="dot" style=${`color:${st.state === 'recording' ? 'var(--brand)' : st.paused ? 'var(--warn)' : 'var(--ok)'}`}></span>${status} · ${langName}</span>
        <h1 style="margin:0;font-size:34px;font-weight:700;letter-spacing:-0.02em;line-height:1.15">Talk. Koli writes.</h1>
        <p style="margin:0;color:var(--ink2)">Press the circle and talk, or in any app ${mode === 'toggle' ? 'press' : 'hold'}${' '}
          ${keys.map((k, i) => html`${i ? ' ' : ''}<span class="kbd">${k}</span>`)}.
          ${mode === 'toggle' ? ' Press again' : ' Let go'} and your words appear where you were typing.</p>
        <label style="display:flex;flex-direction:column;gap:6px">
          <span class="small" style="font-weight:500;color:var(--ink2)">Try it here</span>
          <textarea ref=${practice} class="inp" rows="3" dir="auto" placeholder="Your words appear here…"
            style="font-size:15px;background:var(--sunk);border:0;box-shadow:inset 0 0 0 1.5px var(--line);border-radius:14px;padding:12px 14px"></textarea>
        </label>
      </div>
    </section>
    <section class="card pad" style="display:flex;gap:18px;align-items:flex-start" aria-label="Last thing you said">
      <span class="badge acc"><${Icon} name="copy" /></span>
      <div class="stack grow" style="gap:10px">
        <div class="row-flex" style="justify-content:space-between"><span class="card-title">Last thing you said</span>
          ${delivered && delivered.app && html`<span class="small muted">${delivered.app.replace(/\.exe$/i, '')}</span>`}</div>
        ${last ? html`<p dir=${textDir(last)} style="margin:0;font-size:16.5px;white-space:pre-wrap">${last}</p>
          <div class="row-flex">
            <button type="button" class="btn pri" onClick=${() => call('copy_text', last).then(() => toast('Copied'))}>Copy</button>
            <button type="button" class="btn" onClick=${() => call('paste_last').catch((e) => toast(e.message, { kind: 'err' }))}>Paste it again</button>
            <button type="button" class="btn ghost" onClick=${() => fixAWord(last)}>A word was wrong?</button></div>`
        : html`<p class="muted" style="margin:0">Nothing yet. Press the circle and say something.</p>`}
      </div>
    </section>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px">
      <div class="card pad" style="display:flex;gap:14px;align-items:flex-start"><span class="badge purple"><${Icon} name="pen" /></span>
        <div><b style="font-weight:600">Say the punctuation</b><div class="muted small">“comma”, “period”. In Hebrew: <bdi>“פסיק”</bdi>, <bdi>“נקודה”</bdi>.</div></div></div>
      <div class="card pad" style="display:flex;gap:14px;align-items:flex-start"><span class="badge warn"><${Icon} name="down" /></span>
        <div><b style="font-weight:600">Start a new line</b><div class="muted small">Say “new line”, or “new paragraph”.</div></div></div>
      <div class="card pad" style="display:flex;gap:14px;align-items:flex-start"><span class="badge acc"><${Icon} name="book" /></span>
        <div><b style="font-weight:600">Names it misses?</b><div class="muted small"><a href="#/vocabulary">Teach Koli your words</a> once.</div></div></div>
    </div>
    ${today && html`<div class="row-flex muted" style="gap:28px;font-size:13.5px">
      <span><b style="color:var(--ink);font-size:18px">${today.count}</b> times today</span>
      <span><b style="color:var(--ink);font-size:18px">${Math.round(today.minutes_saved)} min</b> of typing saved</span></div>`}
  </div>`;
}
