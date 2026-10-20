// mock-api.js
// A pretend backend for building screens in a plain browser (open index.html
// or bar.html with ?mock=1). Settings and the schema come from mock-data.json,
// generated from config.defaults.yaml; everything else is made-up sample data.
// Settings changes are kept in memory, and a fake dictation runs every 12 s.


// ── Meeting summaries and templates (mock) ──────────────────────────────
const summaries = {};
let templateDefault = 'standard';
const TEMPLATES = [
  { id: 'standard', name: 'Visual meeting brief', description: 'Outcomes first, who talked most with charts, topics with quotes.',
    origin: 'built-in', language: 'meeting', blocks: ['header', 'tldr', 'decisions', 'action_items', 'lists', 'speakers', 'screenshots', 'topics', 'notes'],
    sections: ['In short', 'Decisions', 'Action items', 'Open questions and parked items', 'Who spoke, with charts', 'What was on screen', 'Topics with quotes'],
    editable: false, overrides_builtin: false },
  { id: 'board-meeting', name: 'Board / management meeting', description: 'Formal minutes: participants, the agenda, per item discussion, decisions and action items.',
    origin: 'built-in', language: 'meeting', blocks: ['header', 'participants', 'agenda', 'agenda_details', 'next_meeting'],
    sections: ['Participants', 'Agenda', 'Discussion, decisions and action items per agenda item', 'Points for the next meeting'],
    editable: false, overrides_builtin: false },
];
const LAYOUTS = {
  standard: { id: 'standard', name: 'Visual meeting brief', date: 'datetime', blocks: TEMPLATES[0].blocks },
  'board-meeting': { id: 'board-meeting', name: 'Board / management meeting', date: 'date', blocks: TEMPLATES[1].blocks,
    labels: { participants: 'Participants', agenda: 'Meeting Agenda', agenda_details: 'Key Points, Decisions, and Action Items',
      next_meeting: 'Points for Discussion at the Next Meeting' } },
};
function templateText(id) {
  const t = TEMPLATES.find((x) => x.id === id) || TEMPLATES[0];
  return `---\nid: ${t.id}\nname: ${t.name}\ndescription: ${t.description}\nlanguage: meeting\ndate: date\nblocks: [${t.blocks.join(', ')}]\n---\nYou write a summary from a meeting transcript.\n\n{{common_rules}}\n\n## What to return\n\n\`\`\`json\n{"version": 1}\n\`\`\`\n`;
}
const STANDARD_BRIEF = {
  version: 1, template: 'standard', generator: 'mock',
  tldr: ['The bridge failed again on Friday; Ilona fixed it but expects it to break. The team agreed to move the release to next week.'],
  decisions: [{ id: 'D1', text: 'Move the release to next week.', at: 18 }],
  action_items: [{ id: 'A1', task: 'Add monitoring to the bridge so a failure pages someone.', owner: 'Ilona', status: 'do_now', due: null, at: 9 },
    { id: 'A2', task: 'Write the post-mortem for Friday.', owner: null, status: 'plan_only', due: null, at: 18 }],
  open_questions: [{ id: 'Q1', text: 'Who owns the bridge after the release?', at: 27 }],
  parked: [{ id: 'P1', text: 'Replacing the bridge altogether.' }],
  topics: [{ id: 'T1', title: 'The bridge outage', start: 0, end: 20, lead: 'Fixed for now, likely to break again.',
    points: ['Ilona fixed it on Friday.', 'Nobody knows why it fails.'], quotes: [{ text: 'We fixed it, but if it breaks again, who knows.', who: 'Ilona', at: 0 }] },
    { id: 'T2', title: 'Next steps', start: 20, end: 40, lead: 'Release moves; monitoring first.', points: ['Release next week.'], quotes: [] }],
  roles: { s2: 'Fixed the bridge', you: 'Asked about the state of the bridge' },
};
const BOARD_BRIEF = {
  version: 1, template: 'board-meeting', generator: 'codex/gpt-test via Koli (Board / management meeting)', title: 'Management Meeting Summary',
  date: '2026-10-10', language: 'he',
  attendees: [{ name: 'אבי', role: 'מנכ"ל' }, { name: 'אילונה', role: 'QA' }],
  agenda: [
    { id: '1', title: 'תקלת הגשר', start: 0, discussion: ['הגשר נפל שוב ביום שישי.', 'אילונה תיקנה, אבל הסיבה עדיין לא ידועה.'],
      decisions: [{ id: 'D1', text: 'מוסיפים ניטור לגשר לפני השחרור.', at: 9 }],
      action_items: [{ id: 'A1', task: 'להוסיף ניטור שמתריע כשהגשר נופל.', owner: 'Ilona', status: 'do_now', due: 'יום ראשון', at: 9 }] },
    { id: '2', title: 'מועד השחרור', start: 20, discussion: ['אין ביטחון בגשר לשחרור השבוע.'],
      decisions: [{ id: 'D2', text: 'השחרור עובר לשבוע הבא.', at: 18 }], action_items: [] },
  ],
  next_meeting: [{ id: 'N1', text: 'מי אחראי על הגשר אחרי השחרור.', at: 27 }],
};

const ok = (data = null) => Promise.resolve({ ok: true, data });
const fail = (error) => Promise.resolve({ ok: false, error });
const delay = (ms) => new Promise((r) => setTimeout(r, ms));
const secs = () => Date.now() / 1000;
let mockWakeMore = false;     // the wake recorder is adding to a saved word

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
const APP_TITLES = ['general (Channel) - Acme - Slack', '(2) WhatsApp - Google Chrome', 'app_rules.py - Koli - Visual Studio Code',
  'Inbox - dana@example.com - Outlook', 'Notes.docx - Word'];

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

// Settings › AI: one of each provider state.
const MOCK_AI = [
  { id: 'ollama', label: 'Ollama', kind: 'local', auth: 'none', site: 'https://ollama.com/download', connected: false, detail: 'Not running', address: 'http://localhost:11434' },
  { id: 'lmstudio', label: 'LM Studio', kind: 'local', auth: 'none', site: 'https://lmstudio.ai', connected: true, detail: 'Running · 4 models, 1 loaded', address: 'http://127.0.0.1:1234', key_hint: '' },
  { id: 'codex', label: 'ChatGPT (Codex)', kind: 'cloud', auth: 'device', site: 'https://chatgpt.com', connected: true, reconnect: false, email: 'dana@example.com', detail: 'Connected as dana@example.com' },
  { id: 'grok', label: 'SuperGrok', kind: 'cloud', auth: 'device', site: 'https://grok.com', connected: false, reconnect: false, detail: 'Not connected' },
  { id: 'gemini', label: 'Gemini', kind: 'cloud', auth: 'key', site: 'https://aistudio.google.com/apikey', connected: true, detail: 'Key saved', key_hint: '…x9Qa' },
  { id: 'openrouter', label: 'OpenRouter', kind: 'cloud', auth: 'key', site: 'https://openrouter.ai/keys', connected: false, detail: 'No key', key_hint: '' },
  { id: 'openai', label: 'OpenAI API', kind: 'cloud', auth: 'key', site: 'https://platform.openai.com/api-keys', connected: false, detail: 'No key', key_hint: '' },
];
// Practice: a short round in English and Hebrew, and what Whisper "heard".
const MOCK_PRACTICE_TEXTS = [
  { language: 'en', text: 'Please refactor the authentication service before we merge the pull request.', targets: ['refactor'] },
  { language: 'he', text: 'צריך לבחור תמונת כריכה חדשה לאלבום המשפחתי.', targets: ['כריכה'] },
  { language: 'he', text: 'צריך לעשות refactor לשירות ההתחברות לפני ה-merge.', targets: ['refactor', 'merge'] },
  { language: 'en', text: 'The TypeScript compiler complains about a missing type on this function.', targets: ['TypeScript'] },
];
const MOCK_PARAGRAPH = { language: 'he', text: 'הבוקר ה-build נשבר אחרי ה-merge האחרון. הטסטים עוברים אצלי, אבל ה-pipeline נכשל בשלב של המיגרציה של מסד הנתונים, ובחרנו תמונת כריכה חדשה לדוח.', targets: ['כריכה'] };
function mockCompare(text) {
  const words = text.split(' ').map((w) => ({ text: w, norm: w.toLowerCase(), status: 'right', heard: w, prob: 0.9 }));
  const wrong = words.find((w) => w.text.startsWith('כריכה') || w.text === 'refactor');
  if (wrong) Object.assign(wrong, { status: 'wrong', heard: wrong.text.startsWith('כריכה') ? 'בריכה' : 're factor', prob: null });
  if (words[3]) Object.assign(words[3], { status: 'unsure', prob: 0.3 });
  const right = words.filter((w) => w.status !== 'wrong').length;
  return { words, extra: [], spans: [], accuracy: right / words.length };
}

// AI actions: what the Activity list shows.
const MOCK_AGENT_HISTORY = [
  { id: 'a3', at: Date.now() / 1000 - 300, sentence: 'move today’s photos from Downloads to Pictures', summary: '',
    lines: ['Move 12 items from ~\\Downloads to ~\\Pictures'], outcome: 'done', undone: false, saved_command: false, undoable: true },
  { id: 'a2', at: Date.now() / 1000 - 4000, sentence: 'תפתח את ה-PDF האחרון במסמכים', summary: '',
    lines: ['Open invoice-0923.pdf'], outcome: 'done', undone: false, saved_command: false, undoable: false },
  { id: 'a1', at: Date.now() / 1000 - 90000, sentence: 'delete the zip files in downloads', summary: '',
    lines: ['Delete 3 items from ~\\Downloads (to the Recycle Bin)'], outcome: 'done', undone: true, saved_command: false, undoable: true },
];

const MOCK_AI_MODELS = {
  lmstudio: ['qwen/qwen3-8b', 'google/gemma-3-12b', 'openai/gpt-oss-20b', 'mistralai/mistral-small-3.2'],
  codex: ['gpt-5.5', 'gpt-5.4-mini', 'gpt-5.3-codex'],
  gemini: ['gemini-2.5-pro', 'gemini-2.5-flash', 'gemini-2.5-flash-lite'],
};

export function createMock(emit) {
  let data = { settings: {}, schema: { sections: [], settings: [] } };
  const loaded = fetch(new URL('./mock-data.json', import.meta.url)).then((r) => r.json()).then((d) => {
    data = d;
    // ?mock=1&welcome shows first-run setup; otherwise pretend setup is done.
    if (!new URLSearchParams(location.search).has('welcome')) data.settings.ui.welcome_done = true;
    if (new URLSearchParams(location.search).has('advanced')) data.settings.ui.mode = 'advanced';
  });
  window.mockEmit = emit;   // so a check in the browser can push any event
  const undo = [];
  const tasks = {};
  let state = { state: 'idle', mode: 'dictate', paused: false, model: 'small', device: 'cpu' };
  let practiceIntake = { languages: ['en', 'he'], accent: '', work: 'software', work_other: '', words: [] };
  let practiceRound = null;
  let agentPassword = false;
  let learningClips = 3420;   // the Training screen's sample clips
  const learningRecent = [
    { id: 'c3', ts: '2026-10-16T10:15:40', app: 'discord.exe', kind: 'auto', seconds: 1.9, language: 'en', outcome: 'dropped: unsure: 1.9 s clip Whisper was not clearly sure of',
      why: 'unsure: 1.9 s clip Whisper was not clearly sure of', heard: 'You can hold it.', typed: '', signals: {}, has_audio: true, test: null },
    { id: 'c2', ts: '2026-10-16T10:15:31', app: 'discord.exe', kind: 'auto', seconds: 4.9, language: 'en', outcome: 'typed', why: '',
      heard: 'And I would say, if you can touch it.', typed: 'And I would say, if you can touch it.', signals: {}, has_audio: true, test: null },
    { id: 'c1', ts: '2026-10-16T10:12:05', app: 'discord.exe', kind: 'hotkey', seconds: 3.4, language: 'en', outcome: 'typed', why: '',
      heard: 'I see ster everything is okay', typed: 'I see ster everything is okay', signals: { repeats: 'c0' }, has_audio: true, test: null },
  ];
  let learnedItems = [];
  let profile = 'dictation';
  const frame = { custom: true, maximized: false };   // the page draws the window buttons
  let seq = 0;
  let extraInstalled = false;

  // A fake meeting: saved ones to list, and a live one (meeting_start, or
  // ?mock=1&meeting) that gains a turn every few seconds.
  const SPEAKERS = [{ id: 'you', name: 'You', colour: 0 }, { id: 's1', name: 'Speaker 1', colour: 1 }, { id: 's2', name: 'Ilona', colour: 2 }];
  // Voices saved from meetings: a named one and one not named yet.
  let voices = [{ id: 'v-3f9a2c1b7e04', name: 'Ilona', count: 41, first_seen: '2026-10-10T15:00:00', last_seen: '2026-10-10T15:42:00', meetings: ['2026-10-10-150000'] },
    { id: 'v-81d0e5a6c2f9', name: '', count: 12, first_seen: '2026-10-10T15:00:00', last_seen: '2026-10-10T15:42:00', meetings: ['2026-10-10-150000'] }];
  const LINES = [['s2', 'We fixed it, but if it breaks again, who knows.'], ['you', 'And the bridge is not working now?'],
    ['s1', 'On Friday they hit one or two bridges.'], ['you', 'שלום, אני אגיע מחר בבוקר.']];
  const emptyMeeting = () => ({ state: 'idle', id: null, title: '', source: {}, started_at: null, elapsed_s: 0, paused_by_user: false,
    excluded: false, behind_s: 0, speakers: [], notice: null, turns: 0, can_continue: false, can_split: false, offer: null,
    auto_paused: false, screenshots: 'key' });
  // A screenshot stands in for the PNGs the real app keeps beside a meeting.
  let mockPngUrl = null;
  const mockPng = () => {
    if (mockPngUrl) return mockPngUrl;
    const c = document.createElement('canvas'); c.width = 320; c.height = 200;
    const g = c.getContext('2d');
    g.fillStyle = '#1f2a33'; g.fillRect(0, 0, 320, 200);
    g.fillStyle = '#3b4a58'; g.fillRect(0, 0, 320, 24);
    g.fillStyle = '#9fb3c8'; for (let i = 0; i < 6; i++) g.fillRect(20, 44 + i * 24, 120 + (i * 37) % 160, 8);
    g.fillStyle = '#F5A623'; g.fillRect(200, 60, 90, 60);
    mockPngUrl = c.toDataURL('image/png');
    return mockPngUrl;
  };
  let liveShots = [];
  let meeting = emptyMeeting();
  const saved = { '2026-10-10-150000': { id: '2026-10-10-150000', title: 'Discord', source: { app: 'discord.exe', kind: 'native', title: '', code: '' },
    started_at: '2026-10-10T15:00:00+03:00', ended_at: '2026-10-10T15:42:00+03:00', language: 'auto', speakers: SPEAKERS,
    turns: LINES.map(([sp, text], i) => ({ start: i * 9, end: i * 9 + 6, stream: sp === 'you' ? 'mic' : 'loopback', speaker: sp, text, lang: /[\u0590-\u05FF]/.test(text) ? 'he' : 'en' })),
    screenshots: [{ at: 12, image: '0012.00.png', caption: 'The bridge dashboard', description: null }], gaps: [] } };
  let liveTurns = [];
  let liveTimer = null;
  const emitMeeting = () => emit('meeting', { ...meeting, turns: liveTurns.length, speakers: SPEAKERS });
  const startMeeting = () => {
    meeting = { ...emptyMeeting(), state: 'live', id: 'live-mock', title: 'Discord', source: { app: 'discord.exe', kind: 'native' },
      started_at: new Date().toISOString(), notice: 'new', can_continue: true };
    liveTurns = [];
    liveShots = [];
    emitMeeting();
    let i = 0;
    clearInterval(liveTimer);
    liveTimer = setInterval(() => {
      const [sp, text] = LINES[i % LINES.length];
      emit('meeting_turn', { id: 'live-mock', pending: { stream: sp === 'you' ? 'mic' : 'loopback' } });
      setTimeout(() => {
        const turn = { start: i * 9, end: i * 9 + 6, stream: sp === 'you' ? 'mic' : 'loopback', speaker: sp, text, lang: 'en' };
        liveTurns.push(turn);
        emit('meeting_turn', { id: 'live-mock', turn });
      }, 800);
      i += 1;
    }, 4000);
  };
  const endMeeting = () => {
    clearInterval(liveTimer);
    emit('meeting', { ...meeting, state: 'ended', duration_s: 125, speakers: SPEAKERS });
    meeting = emptyMeeting();
    emitMeeting();
  };
  // ?detect: a Meet call found in auto mode, counting down like the real panel.
  let countdown = null;
  const detectMeeting = () => {
    const source = { key: 'chrome.exe|Meet - abc-defg-hij', app: 'chrome.exe', kind: 'browser', title: 'Meet - abc-defg-hij' };
    meeting = { ...emptyMeeting(), state: 'offered', source,
      offer: { ...source, label: 'Google Meet', app_name: 'Chrome', starts_at: Date.now() / 1000 + 3 } };
    emitMeeting();
    countdown = setTimeout(() => { if (meeting.state === 'offered') startMeeting(); }, 3000);
  };
  loaded.then(() => {
    const params = new URLSearchParams(location.search);
    if (params.has('meeting')) startMeeting();
    else if (params.has('detect')) detectMeeting();
  });

  const log = (lv, msg, mod = 'koli.state_manager') => {
    seq += 1;
    emit('log', { seq, t: new Date().toTimeString().slice(0, 8), ts: secs(), lv, cls: { WARNING: 'warn', ERROR: 'err', DEBUG: 'debug' }[lv] || 'info', mod: mod.replace('koli.', ''), msg });
  };
  const setState = (patch) => { state = { ...state, ...patch }; emit('state', patch); };

  function runTask(label, kind, seconds = 3, ref = null) {
    const id = `t${Date.now()}`;
    tasks[id] = { id, label, kind, ref, status: 'running', progress: 0, detail: 'Starting…' };
    emit('task', tasks[id]);
    let p = 0;
    const tick = setInterval(() => {
      if (tasks[id].status !== 'running') { clearInterval(tick); return; }
      p += 0.1;
      if (p >= 1) {
        clearInterval(tick);
        tasks[id] = { ...tasks[id], status: 'done', progress: 1, detail: '', result: [] };
      } else tasks[id] = { ...tasks[id], progress: p, detail: kind === 'download' ? `Downloading ${Math.round(p * 466)} of 466 MB` : `${Math.round(p * 100)}%` };
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

  setInterval(() => {
    const i = Math.floor(Date.now() / 5000) % APPS.length;
    emit('foreground', { exe: APPS[i], title: APP_TITLES[i], rule: null });
  }, 2500);
  setInterval(fakeDictation, 12000);
  setInterval(() => { if (state.state === 'idle' && !micTesting) emit('level', { level: Math.random() * 0.004, peak: 0 }); }, 200);
  // The microphone test hears a room with a TV on: about -30 to -26 dB.
  let micTesting = false;
  setInterval(() => { if (micTesting) emit('level', { level: 0.02 + Math.random() * 0.03, peak: 0 }); }, 50);

  const api = {
    async app_bootstrap(which) {
      await loaded;
      log('INFO', 'Mock backend ready', 'koli.ui.host');
      return ok({ version: '0.0.0-mock', platform: 'nt', settings: data.settings, state, tasks: Object.values(tasks),
        pipeline: [], recent: RECENT.slice(0, 5), foreground: { exe: 'slack.exe', title: 'Slack', rule: null }, profile, dev: true,
        meeting: { ...meeting, speakers: SPEAKERS }, turns: liveTurns.slice(), screenshots: liveShots.slice(),
        ...(which === 'main' ? { schema: data.schema, logs: [], webview2: '154.0.0.0 (mock)', undo: false, frame: { ...frame } } : {}) });
    },
    // The window buttons have no window to act on here; maximize just swaps its icon.
    window_minimize: () => ok(null),
    window_close: () => ok(null),
    window_toggle_maximize: () => {
      frame.maximized = !frame.maximized;
      emit('window', { action: 'frame', ...frame });
      return ok(null);
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
      log('INFO', `Setting ${path} = ${JSON.stringify(value)}`, 'koli.settings.service');
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
    mic_test: (on) => { micTesting = !!on; return ok(!!on); },
    my_voice_level: () => ok({ mic: 'Microphone (EMEET Piko+)', mine_db: -21.0 }),
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
    model_download: (key) => ok(runTask(`Downloading ${key}`, 'download', 6, key)),
    gpu_list: () => ok([{ index: 0, name: 'NVIDIA GeForce RTX 5090', memory_gb: 31.8 }, { index: 1, name: 'NVIDIA GeForce RTX 4070 SUPER', memory_gb: 12 }]),
    task_cancel: (id) => { if (tasks[id]) { tasks[id] = { ...tasks[id], status: 'cancelled' }; emit('task', tasks[id]); } return ok(true); },
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
    voice_check: (seconds) => new Promise((resolve) => setTimeout(() => resolve(ok({
      verdict: 'leave_out', reason: "It doesn't sound like you on this microphone: auto-dictation leaves it out", speech: true,
      mic: 'Microphone (EMEET Piko+)',
      voiceprint: { status: 'no_match', score: 0.11, needs: 0.43, learned: 5, needed: 3, ready: true },
      loudness: { status: 'mine', level_db: -29.6, mine_db: -21.0, margin_db: 12 },
    })), (seconds || 5) * 1000)),
    hotkeys_reserved: () => ok([{ hotkey: 'alt+shift', label: 'switch the keyboard language' },
      { hotkey: 'alt+shift+1', label: 'switch to English (United States)' }]),
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
    vocab_record_correction: (canonical) => new Promise((resolve) => setTimeout(() => {
      const all = { ...(getPath(data.settings, 'postprocess.corrections') || {}) };
      all[canonical] = [...(all[canonical] || []), 'hey yeah'];
      setPath(data.settings, 'postprocess.corrections', all);
      resolve(ok({ heard: 'Hey, yeah!', variant: 'hey yeah', added: true, already_right: false, corrections: all }));
    }, 900)),
    vocab_set_replacements: (v) => ok(v),
    vocab_set_snippets: (v) => ok(v),

    commands_list: () => ok(data.commands),
    command_test: (phrase) => {
      const said = String(phrase).trim().toLowerCase();
      const hit = data.commands.find((c) => c.id === said || (c.entry.other_triggers || []).some((p) => p.toLowerCase() === said));
      return ok(hit ? { matched: true, trigger: hit.id, phrase: said, action: hit.action } : { matched: false });
    },
    wake_word_status: () => {
      const phrases = getPath(data.settings, 'wake_word.phrases') || [];
      const personal = !!getPath(data.settings, 'wake_word.only_my_voice') && phrases.length > 0;
      return ok({ personal, word: personal ? phrases[0] : 'Hey Koli', spellings: personal ? phrases : [], mic: 'Jabra Evolve2 65',
        recorded_here: true, samples: personal ? 3 : 0, recordings: [], needed: 3, can_record: true });
    },
    wake_word_prepare: (spelling, more) => { mockWakeMore = !!more; return ok({ ready: true, needed: more ? 1 : 3, most: more ? 10 : 3 }); },
    wake_word_record: () => new Promise((resolve) => setTimeout(() => resolve(ok(mockWakeMore
      ? { heard: 'Deus', count: 1, needed: 1, new_spelling: true, similarity: 0.41 }
      : { heard: 'Hey Avia', count: 1, needed: 3 })), 900)),
    wake_word_save: () => {
      if (mockWakeMore) {
        const phrases = [...(getPath(data.settings, 'wake_word.phrases') || []), 'deus'];
        setPath(data.settings, 'wake_word.phrases', phrases);
        emit('settings', { path: 'wake_word.phrases', new: phrases });
        return ok({ personal: true, word: phrases[0], spellings: phrases, mic: 'Jabra Evolve2 65', recorded_here: true, samples: 5,
          recordings: [], needed: 3, can_record: true, added_recordings: 2, added_spellings: ['deus'],
          skipped_spellings: [{ spelling: 'the use', why: "didn't sound enough like your other recordings (0.12)" }] });
      }
      setPath(data.settings, 'wake_word.phrases', ['קולי']);
      setPath(data.settings, 'wake_word.only_my_voice', true);
      emit('settings', { path: 'wake_word.phrases', new: ['קולי'] });
      return ok({ personal: true, word: 'קולי', spellings: ['קולי'], mic: 'Jabra Evolve2 65', recorded_here: true, recordings: [], needed: 3, can_record: true });
    },
    wake_word_test: () => new Promise((resolve) => setTimeout(() => resolve(ok({ heard: 'Hey Koli', word: true, woke: true })), 900)),
    wake_word_reset: () => {
      setPath(data.settings, 'wake_word.phrases', []);
      setPath(data.settings, 'wake_word.only_my_voice', false);
      emit('settings', { path: 'wake_word.phrases', new: [] });
      return ok({ personal: false, word: 'Hey Koli', spellings: [], mic: 'Jabra Evolve2 65', recorded_here: false, recordings: [], needed: 3, can_record: true });
    },
    command_open_apps: () => ok([{ exe: 'chrome.exe', title: 'Inbox (3) - Gmail - Google Chrome' },
      { exe: 'code.exe', title: 'koli - Visual Studio Code' }, { exe: 'obs64.exe', title: 'OBS 30.2 - Profile: Untitled' }]),
    transforms_list: () => ok(data.transforms),
    transform_apply: () => ok(false),
    rules_list: () => ok({ rules: data.rules, foreground: { exe: 'chrome.exe', title: 'Inbox (3) - Gmail - Google Chrome' }, matching: null }),
    profiles_list: () => ok({ active: profile, profiles: data.profiles }),
    profile_activate: (id) => { profile = id; emit('profile', { active: id }); emit('notify', { message: `Profile: ${id}` }); return ok(id); },

    ai_catalog: () => ok(MOCK_AI.map(({ id, label, kind, auth, site }) => ({ id, label, kind, auth, site }))),
    ai_providers: () => ok(MOCK_AI),
    ai_models: (provider) => ok((MOCK_AI_MODELS[provider] || []).map((id) => ({ id, label: id, loaded: provider === 'lmstudio' && id.includes('qwen') }))),
    ai_test: (model) => new Promise((resolve) => setTimeout(() => resolve(ok({ ok: true, text: 'pong', error: '', seconds: 1.4, model })), 900)),
    ai_set_key: () => ok(12),
    ai_remove: () => ok(null),
    ai_login_start: () => ok({ pending: true, user_code: 'KQ7F-2MXD', url: 'https://auth.openai.com/codex/device', error: '' }),
    ai_login_status: () => ok({ pending: true, user_code: 'KQ7F-2MXD', url: 'https://auth.openai.com/codex/device', error: '' }),
    ai_login_open: () => ok(null),
    drive_status: () => ok({ configured: true, connected: true, reconnect: false, email: 'studio@perspective-media.me',
      client_id: '1234567890-abc.apps.googleusercontent.com', client_hint: 'GOCSPX-••••1234',
      redirect_uri: 'http://127.0.0.1:52880/callback', error: '', last_refresh: Date.now() / 1000, expires: Date.now() / 1000 + 3600 }),
    drive_set_client: () => ok(null),
    drive_disconnect: () => ok(null),
    drive_login_start: () => ok({ pending: true, url: 'https://accounts.google.com/o/oauth2/v2/auth', error: '', connected: false, email: '' }),
    drive_login_status: () => ok({ pending: true, url: 'https://accounts.google.com/o/oauth2/v2/auth', error: '', connected: false, email: '' }),
    drive_login_open: () => ok(null),
    drive_login_cancel: () => ok(null),
    practice_state: () => ok({ intake: practiceIntake, has_intake: false,
      domains: [['software', 'Software development'], ['business', 'Business and email'], ['medicine', 'Medicine'], ['law', 'Law'], ['studies', 'Studies'], ['creative', 'Creative work'], ['other', 'Other']],
      terms: [{ term: 'Koli', from: 'history' }, { term: 'TypeScript', from: 'history' }, { term: 'כריכה', from: 'you' }],
      model: 'codex/gpt-5.6-luna', model_kind: 'cloud', packs: { en: true, he: true }, word_confidence: true,
      round: practiceRound, notes: {} }),
    practice_save_intake: (intake) => { practiceIntake = intake; return ok(intake); },
    practice_start: () => delay(700).then(() => {
      practiceRound = { id: 'r1', stage: 'first', model: 'codex/gpt-5.6-luna', mic: 'Jabra Evolve2 65', notes: [], accuracy: {}, planned: 4,
        items: MOCK_PRACTICE_TEXTS.slice(0, 2).map((t, index) => ({ ...t, index, kind: 'sentence', stage: 'first', result: null, skipped: false })) };
      return { ok: true, data: practiceRound };
    }),
    practice_record: (index) => delay(1500).then(() => {
      const item = practiceRound.items[index];
      item.result = { heard: item.text.replace('כריכה', 'בריכה'), heard_bare: item.text.replace('כריכה', 'בריכה').replace('refactor', 're factor'),
        comparison: mockCompare(item.text), confidence: true, seconds: 1.2 };
      return { ok: true, data: item };
    }),
    practice_stop: () => ok(null),
    practice_flag: (index) => { practiceRound.items[index].flagged = true; return ok(practiceRound); },
    practice_skip: (index) => { practiceRound.items[index].skipped = true; return ok(practiceRound); },
    practice_next: () => delay(600).then(() => {
      const r = practiceRound;
      if (r.stage === 'first') { r.stage = 'second'; MOCK_PRACTICE_TEXTS.slice(2).forEach((t) => r.items.push({ ...t, index: r.items.length, kind: 'sentence', stage: 'second', result: null, skipped: false })); }
      else if (r.stage === 'second') { r.stage = 'paragraph'; r.items.push({ ...MOCK_PARAGRAPH, index: r.items.length, kind: 'paragraph', stage: 'paragraph', result: null, skipped: false }); }
      else r.stage = 'results';
      return { ok: true, data: r };
    }),
    practice_results: () => delay(900).then(() => ok({ round: practiceRound, accuracy: { en: 0.93, he: 0.84 },
      context: [{ word: 'כריכה', alone: 'wrong', in_context: 'right', effect: 'fixed' }],
      notes: { he: 'The k sound of כ is often heard as ב, especially at the start of a word. English terms inside Hebrew come out right.' },
      findings: [
        { id: 'f1', kind: 'correction', language: 'he', term: 'תמונת כריכה', heard: ['תמונת בריכה'], profile: '', reason: 'Whisper wrote “⁨תמונת בריכה⁩” for “⁨תמונת כריכה⁩” (2×)', source: 'ai' },
        { id: 'f2', kind: 'favored', language: 'he', term: 'כריכה', heard: [], profile: '', reason: 'Misheard or dropped (2×)', source: 'rules' },
        { id: 'f3', kind: 'favored', language: 'en', term: 'refactor', heard: [], profile: '', reason: 'Misheard or dropped', source: 'rules' },
        { id: 'f4', kind: 'prompt', language: 'en', term: 'TypeScript', heard: [], profile: 'code', reason: 'A term from your work that Whisper missed', source: 'ai' }] })),
    practice_apply: (ids) => { learnedItems.push(...ids.map((id, n) => ({ id, kind: n ? 'favored' : 'correction', language: 'he', term: n ? 'כריכה' : 'תמונת כריכה', heard: n ? [] : ['תמונת בריכה'], profile: '', mic: 'Jabra Evolve2 65', session: 'r1', reason: 'From practice', source: 'ai' }))); practiceRound = null; return ok(ids.length); },
    practice_close: () => { practiceRound = null; return ok(null); },
    agent_password_status: () => ok({ set: agentPassword, voice: true, recorded: agentPassword }),
    agent_password_ready: () => ok(true),
    agent_password_begin: () => ok(true),
    agent_password_record: () => delay(900).then(() => ({ ok: true, data: { heard: 'Koli approve blue', count: 1, needed: 3 } })),
    agent_password_save: () => { agentPassword = true; return ok(true); },
    agent_password_clear: () => { agentPassword = false; return ok(null); },
    agent_password_test: () => delay(1500).then(() => ({ ok: true, data: { ok: true, why: '' } })),
    agent_history: () => ok(MOCK_AGENT_HISTORY),
    agent_undo: () => ok('ran'),
    agent_cancel: () => ok(null),
    agent_approve: () => ok(null),
    agent_save: (id, phrase) => ok(phrase),
    agent_decline: () => ok(null),
    agent_show: () => ok(null),
    learned_list: () => ok({ items: learnedItems, notes: learnedItems.length ? { he: 'The k sound of כ is often heard as ב.' } : {}, sessions: learnedItems.length ? [{ id: 'r1' }] : [] }),
    learned_delete: (ids) => { const before = learnedItems.length; learnedItems = learnedItems.filter((i) => !ids.includes(i.id)); return ok(before - learnedItems.length); },
    learned_delete_all: () => { learnedItems = []; return ok(null); },
    learning_state: () => ok({ enabled: !!getPath(data.settings, 'learning.enabled'), folder: 'C:\Users\you\AppData\Roaming\koli\learning',
      used_bytes: learningClips ? 412e6 : 0, cap_bytes: (getPath(data.settings, 'learning.disk_cap_gb') || 2) * 1e9,
      clips: learningClips, records: learningClips + 40, audio_hours: learningClips ? 6.3 : 0,
      oldest: learningClips ? '2026-09-02T09:14:00' : null, newest: learningClips ? '2026-10-16T11:20:00' : null,
      week: { kept: 96, typed: 81, dropped: 15, said_again: 6, after_drop: 4, hotkey: 70, auto: 26 },
      by_language: learningClips ? { he: 2130, en: 1290 } : {} }),
    learning_recent: (limit = 20, offset = 0, show = 'all') => {
      const rows = learningRecent.filter((c) => show === 'all' || (show === 'typed' && c.outcome === 'typed')
        || (show === 'dropped' && c.outcome.startsWith('dropped')) || (show === 'again' && (c.signals.repeats || c.signals.after_drop)));
      return ok({ total: rows.length, items: rows.slice(offset, offset + limit) });
    },
    learning_audio: () => Promise.resolve({ ok: false, error: 'Playing needs the app (the preview has no clips)' }),
    learning_test: (id) => {
      const c = learningRecent.find((x) => x.id === id);
      const test = c.outcome.startsWith('dropped')
        ? { text: 'You can hold it.', words: [{ text: 'You', changed: false }, { text: 'can', changed: false }, { text: 'hold', changed: false }, { text: 'it.', changed: false }],
          verdict: 'speech', model: 'large', language: 'en', seconds: 2.1, mean_prob: 0.82 }
        : { text: 'Hi sister, everything is okay', words: [{ text: 'Hi', changed: true }, { text: 'sister,', changed: true }, { text: 'everything', changed: false }, { text: 'is', changed: false }, { text: 'okay', changed: false }],
          verdict: 'different', model: 'large', language: 'en', seconds: 2.4, mean_prob: 0.91 };
      c.test = test;
      return ok(test);
    },
    learning_delete_recent: () => { const n = Math.min(learningClips, 12); learningClips -= n; return ok(n); },
    learning_delete_range: () => { const n = Math.min(learningClips, 140); learningClips -= n; return ok(n); },
    learning_delete_all: () => { const n = learningClips; learningClips = 0; return ok(n); },
    learning_open_folder: () => ok('C:\Users\you\AppData\Roaming\koli\learning'),
    ai_login_cancel: () => ok(null),
    server_status: () => ok({ running: false, url: 'http://127.0.0.1:7777' }),
    diag_checks: () => ok([
      { section: 'System', name: 'Python', status: 'ok', detail: '3.13.1' },
      { section: 'System', name: 'WebView2', status: 'ok', detail: '154.0' },
      { section: 'Audio', name: 'Microphone', status: 'ok', detail: 'Microphone (Realtek Audio)' },
      { section: 'GPU', name: 'CUDA libraries', status: 'warn', detail: 'Not installed', action: { label: 'Install', action: 'gpu_install' } },
    ]),
    diag_selftest: () => ok(runTask('Self-test', 'selftest', 3)),
    diag_shortcuts: () => ok([{ action: 'Dictate', keys: 'Ctrl + Win' }, { action: 'Paste last', keys: 'Ctrl + Alt + Z' }]),
    diag_paths: () => ok({ data: 'C:\\Users\\you\\AppData\\Roaming\\koli', model_cache: 'C:\\Users\\you\\.cache\\huggingface', crash_count: 0 }),
    system_autostart: () => ok({ supported: true, enabled: false }),
    system_set_autostart: (v) => ok(!!v),
    system_check_update: () => ok({ current: '0.0.0-mock', latest: '0.0.0-mock', newer: false }),
    tasks_list: () => ok(Object.values(tasks)),
    task_result: (id) => ok(tasks[id] || null),
    bar_resize: () => ok({ max_height: Math.max(20, window.innerHeight - 56) }),
    // Kept on the page so a drag can be checked in the browser.
    bar_drag: (...args) => { (window.mockDrags = window.mockDrags || []).push(args); return ok(); },
    resources: () => ok({ device: 'cuda', gpu_index: 1, models: ['large-v3-turbo', 'hebrew-large-v3-turbo'],
      gpu: { name: 'NVIDIA GeForce RTX 4070 SUPER', used_mb: 7350, total_mb: 12282 }, koli_gpu_mb: 4520,
      ram_mb: 1830, cpu_percent: 3.2 }),
    meeting_state: () => ok({ ...meeting, speakers: SPEAKERS }),
    meeting_start: () => { startMeeting(); return ok(); },
    meeting_end: () => { endMeeting(); return ok(); },
    meeting_new: () => { endMeeting(); startMeeting(); return ok(); },
    meeting_pause: () => { meeting = { ...meeting, state: 'paused', paused_by_user: true }; emitMeeting(); return ok(); },
    meeting_resume: () => { meeting = { ...meeting, state: 'live', paused_by_user: false, notice: 'resumed', can_split: true }; emitMeeting(); return ok(); },
    meeting_exclude: (on) => { meeting = { ...meeting, excluded: !!on }; emitMeeting(); return ok(); },
    meeting_offer: (accept) => {
      clearTimeout(countdown);
      if (accept) startMeeting(); else { meeting = { ...emptyMeeting(), auto_paused: true }; emitMeeting(); }
      return ok();
    },
    meeting_rename: (sid, name) => { const sp = SPEAKERS.find((s) => s.id === sid); if (sp) sp.name = name; emitMeeting(); return ok(); },
    meeting_voices: () => ok(voices.slice()),
    meeting_forget_voice: (id) => { const had = voices.some((v) => v.id === id); voices = voices.filter((v) => v.id !== id); return ok(had); },
    meeting_continue_previous: () => { meeting = { ...meeting, notice: 'resumed', can_continue: false }; emitMeeting(); return ok(); },
    meeting_split: () => { meeting = { ...meeting, notice: 'new', can_split: false }; emitMeeting(); return ok(); },
    meetings_list: () => ok(Object.values(saved).map((m) => ({ id: m.id, title: m.title, source: m.source, started_at: m.started_at,
      ended_at: m.ended_at, duration_s: 2520, speakers: m.speakers.map((s) => s.name), turns: m.turns.length }))),
    meeting_get: (id) => ok(id === 'live-mock' ? { id, title: meeting.title, source: meeting.source, started_at: meeting.started_at,
      speakers: SPEAKERS, turns: liveTurns.slice(), screenshots: liveShots.slice(), gaps: [] } : saved[id]),
    // The camera button: the "snip" lands 800 ms later, as if the overlay closed.
    meeting_screenshot: () => {
      if (meeting.state !== 'live' && meeting.state !== 'paused') return ok({ ok: false, reason: 'no meeting' });
      setTimeout(() => {
        const at = Math.round((Date.now() - new Date(meeting.started_at).getTime()) / 10) / 100;
        const shot = { at, image: `${String(Math.floor(at)).padStart(4, '0')}.${String(Math.round((at % 1) * 100)).padStart(2, '0')}.png`, caption: '', description: null };
        liveShots.push(shot);
        emit('meeting_shot', { id: 'live-mock', shot });
      }, 800);
      return ok({ ok: true });
    },
    // A summary for the saved meeting, in the shape the summary step writes;
    // Summarize writes one for the others (the board template's, or this one).
    meeting_brief: (id) => ok(summaries[id] || (id === '2026-10-10-150000' ? STANDARD_BRIEF : null)),
    meeting_summary_state: (id) => ok({ ai_ready: true, running: false, template: templateDefault }),
    meeting_summarize: (id, template) => {
      const task = { id: 900 + Object.keys(summaries).length, label: 'Summarizing the meeting', kind: 'meeting_summary', ref: id,
        status: 'running', progress: null, detail: `Writing it with the ${(TEMPLATES.find((x) => x.id === (template || templateDefault)) || {}).name} template` };
      emit('task', task);
      setTimeout(() => {
        summaries[id] = (template || templateDefault) === 'board-meeting' ? BOARD_BRIEF : STANDARD_BRIEF;
        emit('task', { ...task, status: 'done', progress: 1 });
        emit('meeting_brief', { id });
      }, 1500);
      return ok(task.id);
    },
    summary_templates: () => ok(TEMPLATES.map((x) => ({ ...x, default: x.id === templateDefault }))),
    summary_template: (id) => ok({ ...TEMPLATES.find((x) => x.id === id), text: templateText(id) }),
    summary_template_layout: (id) => ok(LAYOUTS[id] || LAYOUTS.standard),
    summary_template_default: (id) => { templateDefault = id; return ok(id); },
    summary_template_copy: (id, name) => ok({ text: templateText(id).replace(/^id: .*$/m, 'id: my-copy').replace(/^name: .*$/m, `name: ${name || 'My copy'}`) }),
    summary_template_draft: (kind, text, name) => new Promise((resolve) => setTimeout(() => resolve(ok({
      text: templateText('board-meeting').replace(/^id: .*$/m, 'id: weekly-sync').replace(/^name: .*$/m, `name: ${name || 'Weekly sync'}`),
      problems: [] })), 1200)),
    summary_template_save: (text) => {
      const id = (text.match(/^id: (.*)$/m) || [])[1] || 'mine';
      const name = (text.match(/^name: (.*)$/m) || [])[1] || id;
      const row = { id, name, description: 'Your own template.', origin: 'mine', language: 'meeting', blocks: ['header', 'tldr'],
        sections: ['In short'], editable: true, overrides_builtin: false };
      const i = TEMPLATES.findIndex((x) => x.id === id);
      if (i >= 0) TEMPLATES[i] = row; else TEMPLATES.push(row);
      return ok(row);
    },
    summary_template_delete: (id) => { const i = TEMPLATES.findIndex((x) => x.id === id); if (i >= 0) TEMPLATES.splice(i, 1); return ok(true); },
    summary_template_read_files: () => ok({ names: ['minutes-september.md'], text: '--- minutes-september.md ---\n# Board Meeting Summary\n## Participants\n...' }),
    meeting_export_pdf: () => ok('C:\\Users\\you\\Desktop\\Discord.pdf'),
    // Google Drive: a short wait, then the doc is "saved" on the meeting.
    meeting_drive_export: (id) => new Promise((resolve) => setTimeout(() => {
      const info = { doc_id: 'mock-doc', doc_url: 'https://docs.google.com/document/d/mock-doc/edit', folder_id: 'mock-folder',
        folder_url: 'https://drive.google.com/drive/folders/mock-folder', exported_at: new Date().toISOString() };
      if (saved[id]) saved[id].drive = info;
      resolve(ok(info));
    }, 1200)),
    meeting_drive_open: (id) => ok((saved[id] && saved[id].drive && saved[id].drive.doc_url) || ''),
    meeting_drive_share: () => ok({ window: false }),
    meeting_caption: (id, image, caption) => {
      const list = id === 'live-mock' ? liveShots : (saved[id] ? saved[id].screenshots : []);
      const shot = list.find((s) => s.image === image);
      if (!shot) return ok(false);
      shot.caption = caption;
      emit('meeting_shot', { id, shot: { ...shot }, caption: true });
      return ok(true);
    },
    meeting_image: () => ok({ url: mockPng() }),
    meeting_delete: (id) => { delete saved[id]; return ok(true); },
    meeting_export: () => ok('C:\\Users\\you\\Desktop\\meeting.txt'),
    meeting_resize: () => ok(),
    meeting_collapse: (c) => ok({ collapsed: !!c }),
    meeting_drag: () => ok(),
    meeting_show_in_sharing: (on) => ok({ shown: !!on }),
    problem_dismiss: () => ok(),
  };

  return new Proxy(api, {
    get(target, name) {
      if (name in target) return target[name];
      if (typeof name !== 'string' || name === 'then') return undefined;
      return (...args) => { console.info(`[mock] ${name}`, args); return ok(name.endsWith('_list') ? [] : null); };
    },
  });
}
