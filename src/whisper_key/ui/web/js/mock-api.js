// mock-api.js
// A pretend backend for building screens in a plain browser (open index.html
// or bar.html with ?mock=1). Settings and the schema come from mock-data.json,
// generated from config.defaults.yaml; everything else is made-up sample data.
// Settings changes are kept in memory, and a fake dictation runs every 12 s.

const ok = (data = null) => Promise.resolve({ ok: true, data });
const fail = (error) => Promise.resolve({ ok: false, error });
const delay = (ms) => new Promise((r) => setTimeout(r, ms));
const secs = () => Date.now() / 1000;

function getPath(obj, path) {
  return path.split('.').reduce((n, k) => (n && typeof n === 'object' ? n[k] : undefined), obj);
}
function setPath(obj, path, value) {
  const keys = path.split('.');
  let n = obj;
  keys.slice(0, -1).forEach((k) => { if (typeof n[k] !== 'object' || n[k] === null) n[k] = {}; n = n[k]; });
  n[keys[keys.length - 1]] = value;
}

const RECENT = ['Thanks, I will send the report by Thursday.', 'שלום, אני אגיע מחר בבוקר.', 'Let us move the meeting to 3 pm.'];
const APPS = ['slack.exe', 'chrome.exe', 'code.exe', 'outlook.exe', 'WINWORD.EXE'];

function history(n) {
  const out = [];
  for (let i = 0; i < n; i++) {
    const t = new Date(Date.now() - i * 3.7 * 3600e3);
    const text = RECENT[i % RECENT.length];
    out.push({ ts: t.toISOString(), text, app: APPS[i % APPS.length], language: /[\u0590-\u05FF]/.test(text) ? 'he' : 'en',
      chars: text.length, duration_s: 2 + (i % 5), model: 'small' });
  }
  return out;
}
const HISTORY = history(64);

export function createMock(emit) {
  let data = { settings: {}, schema: { sections: [], settings: [] } };
  const loaded = fetch(new URL('./mock-data.json', import.meta.url)).then((r) => r.json()).then((d) => {
    data = d;
    // ?mock=1&welcome shows first-run setup; otherwise pretend setup is done.
    if (!new URLSearchParams(location.search).has('welcome')) data.settings.ui.welcome_done = true;
    if (new URLSearchParams(location.search).has('advanced')) data.settings.ui.mode = 'advanced';
  });
  const undo = [];
  const tasks = {};
  let state = { state: 'idle', mode: 'dictate', paused: false, model: 'small', device: 'cpu' };
  let profile = 'dictation';
  let seq = 0;
  let extraInstalled = false;

  const log = (lv, msg, mod = 'whisper_key.state_manager') => {
    seq += 1;
    emit('log', { seq, t: new Date().toTimeString().slice(0, 8), ts: secs(), lv, cls: { WARNING: 'warn', ERROR: 'err', DEBUG: 'debug' }[lv] || 'info', mod: mod.replace('whisper_key.', ''), msg });
  };
  const setState = (patch) => { state = { ...state, ...patch }; emit('state', patch); };

  function runTask(label, kind, seconds = 3) {
    const id = `t${Date.now()}`;
    tasks[id] = { id, label, kind, status: 'running', progress: 0, detail: 'Starting…' };
    emit('task', tasks[id]);
    let p = 0;
    const tick = setInterval(() => {
      p += 0.1;
      if (p >= 1) {
        clearInterval(tick);
        tasks[id] = { ...tasks[id], status: 'done', progress: 1, detail: '', result: [] };
      } else tasks[id] = { ...tasks[id], progress: p, detail: `${Math.round(p * 100)}%` };
      emit('task', tasks[id]);
    }, seconds * 100);
    return id;
  }

  async function fakeDictation() {
    if (state.state !== 'idle' || state.paused) return;
    setState({ state: 'recording', mode: 'dictate' });
    log('INFO', 'Recording started');
    const text = RECENT[Math.floor(Math.random() * RECENT.length)];
    const words = text.split(' ');
    for (let i = 0; i < 30; i++) {
      await delay(100);
      if (state.state !== 'recording') return;
      const level = Math.abs(Math.sin(i / 3)) * 0.12 + Math.random() * 0.03;
      emit('level', { level, peak: level * 1.3 });
      const prob = Math.min(1, level * 8);
      emit('vad', { probability: prob, speaking: prob > 0.5, silence_s: prob > 0.35 ? 0 : 0.3, onset: 0.5, offset: 0.35, timeout_s: 2.5 });
      if (i % 6 === 5 && data.settings.streaming && data.settings.streaming.enabled) emit('streaming_text', { text: words.slice(0, i / 6 + 1).join(' ') });
    }
    const pipe = (stage, extra) => emit('pipeline', { stage, mode: 'dictate', t: secs(), ...extra });
    setState({ state: 'processing' });
    pipe('captured', { duration: 3.0 });
    pipe('transcribing', {});
    log('INFO', 'Transcribing 3.0 s of audio');
    await delay(900);
    const he = /[֐-׿]/.test(text);
    const raw = (he ? '' : 'um, ') + text.toLowerCase();
    pipe('transcribed', { raw, info: { language: he ? 'he' : 'en', language_probability: 0.97, seconds: 0.8, model: 'base', device: 'cpu',
      compute_type: 'int8', beam_size: 5, backend: 'faster_whisper' } });
    pipe('postprocessed', { raw, text, steps: he ? [] : ['filler_words', 'capitalize'], rule: null, app: APPS[0], title: 'Slack' });
    pipe('delivered', { outcome: 'pasted', raw, text, rule: null, app: APPS[0], method: 'paste', auto_enter: false, duration: 3.0 });
    log('INFO', `Pasted ${text.length} characters into ${APPS[0]}`);
    RECENT.unshift(text);
    setState({ state: 'idle' });
  }

  setInterval(() => emit('foreground', { exe: APPS[Math.floor(Date.now() / 5000) % APPS.length], title: 'Some window', rule: null }), 2500);
  setInterval(fakeDictation, 12000);
  setInterval(() => { if (state.state === 'idle') emit('level', { level: Math.random() * 0.004, peak: 0 }); }, 200);

  const api = {
    async app_bootstrap(which) {
      await loaded;
      log('INFO', 'Mock backend ready', 'whisper_key.ui.host');
      return ok({ version: '0.0.0-mock', platform: 'nt', settings: data.settings, state, tasks: Object.values(tasks),
        pipeline: [], recent: RECENT.slice(0, 5), foreground: { exe: 'slack.exe', title: 'Slack', rule: null }, profile, dev: true,
        ...(which === 'main' ? { schema: data.schema, logs: [], webview2: '154.0.0.0 (mock)', undo: false } : {}) });
    },
    app_state: () => ok(state),
    app_welcome_done: () => { setPath(data.settings, 'ui.welcome_done', true); return ok(true); },

    async settings_all() { await loaded; return ok(data.settings); },
    // Same result shapes as SettingsService.set / set_many / undo.
    async settings_set(path, value, record = true) {
      await loaded;
      const meta = data.schema.settings.find((x) => x.path === path) || { label: path, apply: 'live' };
      const old = getPath(data.settings, path);
      if (JSON.stringify(old) === JSON.stringify(value)) return ok({ ok: true, path, value, old, applied: 'unchanged', task_id: null, warnings: [] });
      if (record) undo.push({ label: meta.label, changes: [[path, old]] });
      setPath(data.settings, path, value);
      const applied = meta.apply;
      const task_id = applied === 'background' ? runTask(`Applying ${meta.label}`, 'settings', 2) : null;
      emit('settings', { path, old, new: value, source: 'ui', applied, task_id, label: meta.label, undoable: record });
      log('INFO', `Setting ${path} = ${JSON.stringify(value)}`, 'whisper_key.settings.service');
      return ok({ ok: true, path, value, old, warnings: [], label: meta.label, applied, task_id });
    },
    async settings_set_many(patch, label = '') {
      const results = [];
      const olds = [];
      for (const [p, v] of Object.entries(patch)) {
        const before = getPath(data.settings, p);
        const r = (await api.settings_set(p, v, false)).data;
        if (r.applied !== 'unchanged') olds.push([p, before]);
        results.push(r);
      }
      if (olds.length) undo.push({ label: label || 'Several settings', changes: olds });
      return ok({ ok: true, results, task_ids: results.filter((r) => r.task_id).map((r) => r.task_id) });
    },
    async settings_undo() {
      const entry = undo.pop();
      if (!entry) return ok(null);
      for (const [p, v] of entry.changes.slice().reverse()) await api.settings_set(p, v, false);
      return ok({ label: entry.label, paths: entry.changes.map(([p]) => p) });
    },
    settings_reset_section: () => ok([]),
    settings_reset_all: () => ok([]),
    simple_get: () => ok({ ...data.simple, language: getPath(data.settings, 'whisper.language') || 'auto' }),
    // The real mapping lives in simple_mode.py; the mock only remembers the answer.
    simple_set: (q, v) => { data.simple = { ...data.simple, [q]: v }; return ok({ ok: true, results: [], task_ids: [] }); },

    audio_devices: () => ok({ host: 'Windows WASAPI', hosts: ['MME', 'Windows WASAPI', 'Windows DirectSound'],
      devices: [{ id: 1, name: 'Microphone (Realtek Audio)', default: true }, { id: 4, name: 'Headset (Jabra Evolve2)', default: false }],
      current: { device_id: null, name: 'Microphone (Realtek Audio)', host: 'Windows WASAPI', native_rate: 48000, recording_rate: 16000,
        channels: 1, dtype: 'float32', preroll_s: 0.3, max_duration: 900, noise_suppression: false, whisper_mode: false } }),
    mic_test: (on) => ok(!!on),
    sound_preview: () => ok(),

    models_list: () => ok({
      models: [
        { key: 'tiny', label: 'Tiny', group: 'multilingual', size: '75 MB', cached: true, active: false, visible: true, origin: 'built-in' },
        { key: 'small', label: 'Small', group: 'multilingual', size: '466 MB', cached: true, active: true, visible: true, origin: 'built-in' },
        { key: 'large-v3-turbo', label: 'Large v3 turbo', group: 'multilingual', size: '1.6 GB', cached: false, active: false, visible: true, origin: 'built-in' },
        { key: 'ivrit-turbo', label: 'ivrit.ai turbo (Hebrew)', group: 'language', size: '1.6 GB', cached: false, active: false, visible: true, origin: 'built-in' },
        { key: 'distil-small.en', label: 'Distil small (English)', group: 'english', size: '332 MB', cached: false, active: false, visible: false, origin: 'built-in' },
      ],
      groups: [{ id: 'multilingual', title: 'Every language' }, { id: 'language', title: 'One language' }, { id: 'english', title: 'English only' }, { id: 'custom', title: 'Added by you' }],
      streaming: ['tiny', 'base'],
    }),
    model_download: (key) => ok(runTask(`Downloading ${key}`, 'download', 4)),
    extra_status: () => ok({ installed: extraInstalled }),
    extra_install: (name) => { const id = runTask(`Installing the ${name} package`, 'install', 3); setTimeout(() => { extraInstalled = true; }, 3100); return ok(id); },
    gpu_detect: () => ok({ name: 'NVIDIA GeForce RTX 3060', class: 'nvidia', ready: false, vram_gb: 12 }),
    gpu_install: () => ok(runTask('Installing GPU support', 'gpu', 6)),

    recent: () => ok(RECENT.slice(0, 5)),
    copy_text: () => ok(true),
    paste_last: () => ok(true),
    dictation_start: () => { fakeDictation(); return ok(); },
    dictation_stop: () => ok(),
    dictation_cancel: () => { setState({ state: 'idle' }); return ok(); },
    dictation_pause: (p) => { setState({ paused: !!p }); return ok(!!p); },
    log_tail: () => ok([]),
    log_clear: () => ok(),

    history_query(q = '', appName = '', period = 'all', offset = 0, limit = 50) {
      const items = HISTORY.filter((e) => (!q || e.text.toLowerCase().includes(q.toLowerCase())) && (!appName || e.app === appName));
      return ok({ items: items.slice(offset, offset + limit), total: items.length, all: HISTORY.length });
    },
    history_apps: () => ok(APPS),
    history_suggest: () => ok([{ word: 'Kubernetes', count: 4 }, { word: 'Jabra', count: 2 }]),
    insights(days = 14) {
      const per_day = [];
      for (let i = days - 1; i >= 0; i--) per_day.push({ date: new Date(Date.now() - i * 864e5).toISOString().slice(0, 10), words: Math.round(80 + Math.random() * 400), count: Math.round(3 + Math.random() * 20) });
      const sum = { count: 412, words: 18240, seconds: 6120, minutes_saved: 312.5, wpm: 142 };
      return ok({ lifetime: sum, today: { count: 9, words: 380, seconds: 140, minutes_saved: 6.4, wpm: 151 }, streak: 5, longest_streak: 12,
        active_days: 41, first: '2026-08-02T09:00:00', per_day,
        apps: APPS.map((a, i) => ({ app: a, count: 40 - i * 7, share: [0.34, 0.25, 0.18, 0.13, 0.1][i] })),
        languages: [{ code: 'en', count: 300, share: 0.73 }, { code: 'he', count: 112, share: 0.27 }] });
    },

    vocab_add: (words) => { const list = [...(getPath(data.settings, 'whisper.hotwords') || []), ...words]; setPath(data.settings, 'whisper.hotwords', list); return ok(list); },
    vocab_scan: () => ok([{ word: 'Kubernetes', count: 4 }, { word: 'Jabra', count: 2 }]),
    vocab_set_corrections: (v) => ok(v),
    vocab_set_replacements: (v) => ok(v),
    vocab_set_snippets: (v) => ok(v),

    commands_list: () => ok(data.commands),
    command_test: (phrase) => {
      const hit = data.commands.find((c) => c.id === String(phrase).trim().toLowerCase());
      return ok(hit ? { matched: true, trigger: hit.id, action: hit.action } : { matched: false });
    },
    transforms_list: () => ok(data.transforms),
    transform_apply: () => ok(false),
    rules_list: () => ok({ rules: data.rules, foreground: { exe: 'windowsterminal.exe', title: 'Terminal' }, matching: 'terminals' }),
    profiles_list: () => ok({ active: profile, profiles: data.profiles }),
    profile_activate: (id) => { profile = id; emit('profile', { active: id }); emit('notify', { message: `Profile: ${id}` }); return ok(id); },

    ai_probe: () => ok({ reachable: false, models: [] }),
    server_status: () => ok({ running: false, url: 'http://127.0.0.1:7777' }),
    diag_checks: () => ok([
      { section: 'System', name: 'Python', status: 'ok', detail: '3.13.1' },
      { section: 'System', name: 'WebView2', status: 'ok', detail: '154.0' },
      { section: 'Audio', name: 'Microphone', status: 'ok', detail: 'Microphone (Realtek Audio)' },
      { section: 'GPU', name: 'CUDA libraries', status: 'warn', detail: 'Not installed', action: { label: 'Install', action: 'gpu_install' } },
    ]),
    diag_selftest: () => ok(runTask('Self-test', 'selftest', 3)),
    diag_shortcuts: () => ok([{ action: 'Dictate', keys: 'Ctrl + Win' }, { action: 'Paste last', keys: 'Ctrl + Alt + Z' }]),
    diag_paths: () => ok({ data: 'C:\\Users\\you\\AppData\\Roaming\\whisperkey', model_cache: 'C:\\Users\\you\\.cache\\huggingface', crash_count: 0 }),
    system_autostart: () => ok({ supported: true, enabled: false }),
    system_set_autostart: (v) => ok(!!v),
    system_check_update: () => ok({ current: '0.0.0-mock', latest: '0.0.0-mock', newer: false }),
    tasks_list: () => ok(Object.values(tasks)),
    task_result: (id) => ok(tasks[id] || null),
    bar_resize: () => ok({ max_height: Math.max(20, window.innerHeight - 56) }),
  };

  return new Proxy(api, {
    get(target, name) {
      if (name in target) return target[name];
      if (typeof name !== 'string' || name === 'then') return undefined;
      return (...args) => { console.info(`[mock] ${name}`, args); return ok(name.endsWith('_list') ? [] : null); };
    },
  });
}
