// screens/simple.js
// Simple Settings: plain questions instead of engine settings. Each answer
// sets several real settings at once (see settings/simple_mode.py). When the
// current settings match no answer — tuned in Advanced — the question shows
// "Custom" and is left alone until the user picks an answer.

import { html, useEffect, useState } from '../../vendor/preact-htm.module.js';
import { call } from '../api.js';
import { app, saveSetting, toast, undo } from '../store.js';
import { HotkeyCapture, Icon, Select, Toggle } from '../ui.js';
import { ExtraInstall, MicSelect, MicTest, TaskProgress, useRunningTasks } from './shared.js';

const LANGUAGES = [['auto', 'Detect automatically'], ['en', 'English'], ['he', 'Hebrew · עברית'], ['ar', 'Arabic'],
  ['es', 'Spanish'], ['fr', 'French'], ['de', 'German'], ['ru', 'Russian'], ['it', 'Italian'], ['pt', 'Portuguese'],
  ['nl', 'Dutch'], ['pl', 'Polish'], ['uk', 'Ukrainian'], ['tr', 'Turkish'], ['ja', 'Japanese'], ['zh', 'Chinese']];

function Options({ name, value, options, onPick }) {
  return html`<div role="radiogroup" aria-label=${name} style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px">
    ${options.map(([v, title, sub]) => html`<button type="button" role="radio" aria-checked=${value === v}
      class="card" onClick=${() => onPick(v)}
      style=${`text-align:start;padding:14px 16px;cursor:pointer;display:flex;flex-direction:column;gap:4px;${value === v ? 'border-color:var(--acc);box-shadow:0 0 0 1px var(--acc);background:var(--acc-soft)' : ''}`}>
      <b style="font-weight:600">${title}</b><span class="small muted">${sub}</span></button>`)}
  </div>
  ${value === 'custom' && html`<p class="small muted" style="margin:0">Set to something custom in Advanced. Pick an answer to replace it.</p>`}`;
}

function Q({ title, help, children, id }) {
  return html`<div style="padding:20px 22px;border-top:1px solid var(--line);display:flex;flex-direction:column;gap:12px" id=${id}>
    <h3 style="margin:0;font-size:16px;font-weight:600">${title}</h3>
    ${help && html`<p class="muted" style="margin:0;font-size:13.5px">${help}</p>`}${children}</div>`;
}

function Line({ title, help, children }) {
  return html`<div style="padding:16px 22px;border-top:1px solid var(--line)"><label class="row-flex" style="justify-content:space-between;flex-wrap:nowrap">
    <span><b style="font-weight:600">${title}</b>${help && html`<br /><span class="muted" style="font-size:13.5px">${help}</span>`}</span>${children}</label></div>`;
}

export function SimpleSettingsScreen() {
  const [a, setA] = useState(null);
  const settings = app.use((s) => s.settings);
  const reload = () => call('simple_get').then(setA).catch(() => {});
  useEffect(() => { reload(); }, [settings]);
  if (!a) return html`<div class="empty">Loading…</div>`;
  const set = (q, v) => call('simple_set', q, v).then((res) => {
    toast('Saved', { action: { label: 'Undo', run: undo } });
    if (res && res.task_ids && res.task_ids.length) toast('Switching in the background — you can keep talking.');
    reload();
  }).catch((e) => toast(e.message, { kind: 'err' }));
  const presets = a.quality_presets || {};
  const switching = useRunningTasks('engine')[0];
  return html`<div class="page narrow">
    <div><h1 class="page-title">Settings</h1><p class="page-sub">Changes save by themselves.</p></div>

    <section class="card" aria-labelledby="sq-mic">
      <div style="padding:20px 22px;display:flex;flex-direction:column;gap:12px">
        <div class="row-flex" style="justify-content:space-between"><h3 id="sq-mic" class="row-flex" style="margin:0;font-size:16px;font-weight:600"><${Icon} name="mic" />Microphone & sound</h3></div>
        <div class="row-flex"><${MicSelect} /></div>
        <${MicTest} big=${true} />
        <p class="muted" style="margin:0;font-size:13.5px">Say something while testing. The bars should move. If they don't, pick another microphone above.</p>
      </div>
      <${Line} title="Reduce background noise" help="For fans, air conditioning or a noisy office."><${Toggle} checked=${a.noise} label="Reduce background noise" onChange=${(v) => set('noise', v)} /></${Line}>
      ${a.noise && html`<div style="padding:0 22px 14px"><${ExtraInstall} name="noise" /></div>`}
      <${Line} title="I speak quietly" help="Turns up soft voices, for whispering in an open office."><${Toggle} checked=${a.quiet} label="I speak quietly" onChange=${(v) => set('quiet', v)} /></${Line}>
      <${Line} title="Pause my music while I talk" help="Pauses Spotify, YouTube and others once you start talking, and plays them again when you're done."><${Toggle} checked=${a.music} label="Pause music" onChange=${(v) => set('music', v)} /></${Line}>
      <${Line} title="Turn the computer down while I talk" help="Lowers the volume once you start talking, then puts it back when you're done. Only if it is louder than this.">
        <${Select} label="Turn the computer down while I talk" value=${a.volume || 'off'} style="max-width:180px"
          options=${[['off', 'Off'], ['5', 'To 5%'], ['10', 'To 10%'], ['20', 'To 20%'], ['30', 'To 30%'], ['50', 'To 50%']]}
          onChange=${(v) => set('volume', v)} /></${Line}>
      <${Line} title="Stop listening when I go quiet" help="Ends the recording by itself if you forget.">
        <${Select} label="Stop after silence" value=${a.stop} onChange=${(v) => set('stop', v)}
          options=${[['never', 'Never'], ['10', 'After 10 seconds'], ['30', 'After 30 seconds'], ['60', 'After 1 minute'], ...(a.stop === 'custom' ? [['custom', 'Custom (Advanced)']] : [])]} /></${Line}>
      <${Line} title="Play sounds" help="A short beep when listening starts and stops."><${Toggle} checked=${a.sounds} label="Play sounds" onChange=${(v) => set('sounds', v)} /></${Line}>
    </section>

    <section class="card" aria-label="Talking">
      <${Q} title="What language do you speak?" help="Pick one if you always speak the same language. It gets more accurate.">
        <div><${Select} label="Language" value=${a.language} options=${LANGUAGES} onChange=${(v) => set('language', v)} /></div></${Q}>
      <${Q} title="How do you want to start talking?">
        <${Options} name="How to start" value=${a.start} onPick=${(v) => set('start', v)}
          options=${[['hold', 'Hold the keys while I talk', 'Let go and your text appears.'], ['press', 'Press once to start, again to stop', 'Better for long messages.']]} />
        <div class="row-flex"><span>Your keys:</span><${HotkeyCapture} value=${a.keys} label="Record shortcut"
          onCommit=${(v) => saveSetting('hotkey.recording_hotkey', v).then(reload).catch(() => {})} /></div></${Q}>
      <${Q} title="Where should the text go?">
        <${Options} name="Where text goes" value=${a.where} onPick=${(v) => set('where', v)}
          options=${[['type', "Type it where I'm writing", 'Like you typed it yourself.'], ['copy', 'Just copy it', 'You paste it with Ctrl + V.']]} /></${Q}>
    </section>

    <section class="card" aria-label="Quality">
      <${Q} title="Speed or accuracy?" help="Changes in the background. You can keep talking while it switches.">
        <${Options} name="Speed or accuracy" value=${a.quality} onPick=${(v) => set('quality', v)}
          options=${[['fast', 'Fastest', `Good for short messages. (${presets.fast})`], ['balanced', 'Balanced', `Recommended for this computer. (${presets.balanced})`],
            ['accurate', 'Most accurate', `Slower, bigger download. (${presets.accurate})`]]} />
        ${switching && html`<div style="margin-top:12px"><${TaskProgress} task=${switching} /></div>`}</${Q}>
    </section>

    <section class="card" aria-label="Your text">
      <${Q} title="How should your text look?">
        <${Options} name="Text style" value=${a.look} onPick=${(v) => set('look', v)}
          options=${[['standard', 'As written', 'Capitals and punctuation, nothing removed.'], ['exact', 'Exactly as I said it', 'No changes at all.'], ['clean', 'Clean', 'Capitals, periods, no “um” or repeats.'], ['relaxed', 'Relaxed', 'For chats. No period at the end.']]} /></${Q}>
      <${Line} title="Say punctuation out loud" help="Say “comma”, “period” or “new line” and the sign appears."><${Toggle} checked=${a.punctuation} label="Say punctuation out loud" onChange=${(v) => set('punctuation', v)} /></${Line}>
      <div style="padding:16px 22px;border-top:1px solid var(--line)" class="row-flex">
        <span class="grow"><b style="font-weight:600">Names and words it gets wrong</b><br /><span class="muted" style="font-size:13.5px">${(settings.whisper.hotwords || []).length} words added</span></span>
        <a class="btn" href="#/vocabulary">Teach it words</a></div>
    </section>

    <section class="card" aria-label="App">
      <${Line} title="Start when the computer starts"><${Toggle} checked=${a.startup} label="Start with the computer" onChange=${(v) => set('startup', v)} /></${Line}>
      <${Line} title="Show the Koli bubble" help="A small tab on the edge of the screen that shows when you are being heard."><${Toggle} checked=${a.bar} label="Show the Koli bubble" onChange=${(v) => set('bar', v)} /></${Line}>
      <${Q} title="Transcribe my calls?" help="Discord, Zoom, Teams and Google Meet. The panel shows who said what; nothing leaves this computer.">
        <${Options} name="Calls" value=${a.calls} onPick=${(v) => set('calls', v)}
          options=${[['auto', 'Yes, automatically', 'Starts when a call has sound.'], ['ask', 'Ask me first', 'A small prompt when a call starts.'], ['off', 'No', 'Never listens to calls.']]} /></${Q}>
      <${Line} title="Dark mode"><${Toggle} checked=${settings.ui.theme === 'dark'} label="Dark mode" onChange=${(v) => saveSetting('ui.theme', v ? 'dark' : 'light', { quiet: true })} /></${Line}>
    </section>

    <div class="row-flex" style="justify-content:space-between;padding:16px 20px;border-radius:14px;border:1px dashed var(--line2)">
      <span style="color:var(--ink2)">Want every option, per-app rules and voice commands?</span>
      <button type="button" class="btn" onClick=${() => { saveSetting('ui.mode', 'advanced', { quiet: true }); location.hash = '#/settings'; }}>Switch to Advanced</button>
    </div>
  </div>`;
}
