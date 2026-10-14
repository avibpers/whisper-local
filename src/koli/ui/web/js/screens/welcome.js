// screens/welcome.js
// First run: four short steps instead of console prompts — your language,
// your microphone, the graphics card (only when one is found), and a first
// try, which also offers Practice (teach Koli your voice). The installer will
// ask the same questions later; until then this is where a new user lands.

import { html, useEffect, useState } from '../../vendor/preact-htm.module.js';
import { call } from '../api.js';
import { app, navigate, prettyKeys, saveSetting, setting, toast, useSetting } from '../store.js';
import { Icon, Select } from '../ui.js';
import { MicSelect, MicTest } from './shared.js';
import { KoliMark } from '../brand.js';

const LANGUAGES = [['auto', 'Detect automatically'], ['en', 'English'], ['he', 'Hebrew · עברית'], ['ar', 'Arabic'],
  ['es', 'Spanish'], ['fr', 'French'], ['de', 'German'], ['ru', 'Russian'], ['it', 'Italian'], ['pt', 'Portuguese'], ['pl', 'Polish']];

export function WelcomeScreen() {
  const [step, setStep] = useState(0);
  const [gpu, setGpu] = useState(null);
  const lang = useSetting('whisper.language');
  const keys = prettyKeys(useSetting('hotkey.recording_hotkey'));
  const tasks = app.use((s) => s.tasks);
  const pipeline = app.use((s) => s.pipeline);
  const typed = (pipeline.find((p) => p.stage === 'delivered') || {}).text;
  useEffect(() => { call('gpu_detect').then(setGpu).catch(() => setGpu({})); }, []);
  const showGpu = gpu && gpu.name && !gpu.ready && gpu.class === 'nvidia';
  const steps = ['language', 'microphone', ...(showGpu ? ['gpu'] : []), 'try'];
  const current = steps[Math.min(step, steps.length - 1)];
  const gpuTask = Object.values(tasks).find((t) => t.kind === 'gpu');
  // `then`: where to go after the welcome (Practice from the last step).
  const finish = (then) => call('app_welcome_done').then(() => {
    app.set((s) => ({ settings: { ...s.settings, ui: { ...s.settings.ui, welcome_done: true } } }));
    navigate(typeof then === 'string' ? then : setting('ui.mode') === 'advanced' ? 'live' : 'home');
  });
  const next = () => (step >= steps.length - 1 ? finish() : setStep(step + 1));

  return html`<div style="min-height:100vh;display:grid;place-items:center;padding:24px">
    <div class="card" style="width:min(640px,100%);padding:32px;display:flex;flex-direction:column;gap:20px">
      <div class="row-flex" style="justify-content:space-between">
        <div class="brand"><${KoliMark} size=${36} />Welcome to Koli</div>
        <span class="small muted">Step ${step + 1} of ${steps.length}</span></div>

      ${current === 'language' && html`<div class="stack">
        <h1 style="margin:0;font-size:24px">What language do you speak?</h1>
        <p class="muted" style="margin:0">Pick one if you always speak the same language. It is faster and more accurate. You can change this any time.</p>
        <${Select} label="Language" value=${lang} options=${LANGUAGES} onChange=${(v) => saveSetting('whisper.language', v, { quiet: true })} /></div>`}

      ${current === 'microphone' && html`<div class="stack">
        <h1 style="margin:0;font-size:24px">Check your microphone</h1>
        <p class="muted" style="margin:0">Press Test and say something. The bars should move.</p>
        <div class="row-flex"><${MicSelect} /></div><${MicTest} big=${true} /></div>`}

      ${current === 'gpu' && html`<div class="stack">
        <h1 style="margin:0;font-size:24px">Use your graphics card?</h1>
        <p class="muted" style="margin:0">${gpu.name} can make dictation several times faster. It needs about 1.2 GB of NVIDIA libraries, downloaded once in the background. You can keep going while it installs.</p>
        ${gpuTask ? html`<div class="notice warn"><${Icon} name="refresh" /><span>${gpuTask.status === 'running' ? (gpuTask.detail || 'Installing…') : gpuTask.status === 'done' ? 'Installed — dictation now uses the GPU.' : `Didn't work: ${gpuTask.error}`}</span></div>`
          : html`<div class="row-flex"><button type="button" class="btn pri" onClick=${() => call('gpu_install').catch((e) => toast(e.message, { kind: 'err' }))}>Yes, install it</button>
            <button type="button" class="btn" onClick=${next}>No, use the CPU</button></div>`}</div>`}

      ${current === 'try' && html`<div class="stack">
        <h1 style="margin:0;font-size:24px">Try it once</h1>
        <p class="muted" style="margin:0">Click in the box, then ${setting('hotkey.recording_mode') === 'toggle' ? 'press' : 'hold'}
          ${keys.map((k, i) => html`${i ? ' + ' : ' '}<span class="kbd">${k}</span>`)} and say something.</p>
        <textarea class="inp" rows="3" dir="auto" placeholder="Your words appear here…" style="font-size:15px"></textarea>
        ${typed && html`<div class="notice info"><${Icon} name="check" /><span>It works. That's all there is to it.</span></div>`}
        <p class="small muted" style="margin:0">The first dictation may take a little longer while the model loads.</p>
        <div class="notice info" style="align-items:center"><${Icon} name="mic" />
          <span class="grow">Speak with an accent, or mix languages? Read a few short texts and Koli learns your words and how you say them. About 3 minutes.</span>
          <button type="button" class="btn sm" onClick=${() => finish('practice')}>Teach Koli your voice</button></div></div>`}

      <div class="row-flex" style="justify-content:space-between">
        <button type="button" class="btn ghost" onClick=${finish}>Skip setup</button>
        <div class="row-flex">
          ${step > 0 && html`<button type="button" class="btn" onClick=${() => setStep(step - 1)}>Back</button>`}
          <button type="button" class="btn pri" onClick=${next}>${step >= steps.length - 1 ? 'Done' : 'Next'}</button></div>
      </div>
    </div>
  </div>`;
}
