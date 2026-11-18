// guest_board.js
// Runs in Koli's own browser before Google Meet's scripts (meeting/guest.py
// adds it). It gives Meet a camera and a microphone that are Koli's: the
// camera is a board drawn on a canvas (what she noted in the meeting), the
// microphone is silence, where her voice goes later. The real devices are
// never asked for: every path returns Koli's tracks or fails. It also keeps
// the page's call connections, to read which sizes Meet asks her to send.
// Design: docs/design/meet-participant.md.

(() => {
  if (window.__koli) return;
  const CONFIG = /*CONFIG*/{ width: 960, height: 720, tickMs: 1000, view: {} }/*END*/;
  const md = navigator.mediaDevices;
  if (!md) return;

  // ── The board ──────────────────────────────────────────────────────────────

  const FONT = '"Segoe UI", Arial, sans-serif';
  const COLOURS = { back: '#10141c', bar: '#ff7a1a', title: '#ffffff', dim: '#9aa7bd', text: '#e8edf5', label: '#ff9d4d',
                    panel: '#1b2230', dotOff: '#5a3a20' };
  const LABELS = {
    ltr: { now: 'NOW', decisions: 'DECIDED', tasks: 'TASKS', questions: 'OPEN QUESTIONS', more: 'more' },
    rtl: { now: 'עכשיו', decisions: 'הוחלט', tasks: 'משימות', questions: 'שאלות פתוחות', more: 'נוספים' },
  };

  const canvas = document.createElement('canvas');
  canvas.width = CONFIG.width;
  canvas.height = CONFIG.height;
  const ctx = canvas.getContext('2d');
  const state = { view: { title: 'Koli', subtitle: '', topic: '', decisions: [], tasks: [], questions: [], say: '', dense: false },
                  draws: 0, started: Date.now(), calls: [] };
  Object.assign(state.view, CONFIG.view || {});

  function isRtl(text) { return /[֐-׿؀-ۿ]/.test(text || ''); }

  // Breaks `text` into lines no wider than `width` in the current font.
  function wrap(text, width, maxLines) {
    const words = String(text || '').split(/\s+/).filter(Boolean);
    const lines = [];
    let line = '';
    for (const word of words) {
      const next = line ? `${line} ${word}` : word;
      if (ctx.measureText(next).width <= width || !line) { line = next; continue; }
      lines.push(line);
      line = word;
      if (lines.length === maxLines) break;
    }
    if (lines.length < maxLines && line) lines.push(line);
    // The last line allowed ends with … when words were left over.
    const used = lines.join(' ').split(/\s+/).length;
    if (used < words.length && lines.length) {
      let last = lines[lines.length - 1];
      while (last && ctx.measureText(`${last}…`).width > width) last = last.slice(0, -1);
      lines[lines.length - 1] = `${last.trimEnd()}…`;
    }
    return lines;
  }

  function draw() {
    const W = canvas.width, H = canvas.height;
    const view = state.view;
    const u = Math.min(W, H) / 720;              // everything scales with the frame
    const pad = Math.round(Math.max(40, W * 0.07) * (W < 700 ? 0.8 : 1));
    const width = W - pad * 2;
    const rtl = isRtl(view.topic) || isRtl((view.decisions || [])[0]) || isRtl((view.tasks || [])[0]) || isRtl(view.say);
    const labels = rtl ? LABELS.rtl : LABELS.ltr;
    const edge = rtl ? W - pad : pad;            // where a line of text starts
    ctx.direction = rtl ? 'rtl' : 'ltr';
    ctx.textAlign = rtl ? 'right' : 'left';
    ctx.textBaseline = 'top';

    ctx.fillStyle = COLOURS.back;
    ctx.fillRect(0, 0, W, H);
    ctx.fillStyle = COLOURS.bar;
    ctx.fillRect(0, 0, W, Math.round(10 * u));

    // The header: her name, who she is with, and a dot that changes on
    // every tick so a frame always differs from the last (a still canvas
    // sends nothing, measured).
    let y = Math.round(40 * u);
    ctx.fillStyle = COLOURS.title;
    ctx.font = `600 ${Math.round(60 * u)}px ${FONT}`;
    ctx.fillText(view.title || 'Koli', edge, y);
    const titleWidth = ctx.measureText(view.title || 'Koli').width;
    ctx.fillStyle = state.draws % 2 === 0 ? COLOURS.bar : COLOURS.dotOff;
    ctx.beginPath();
    ctx.arc(rtl ? edge - titleWidth - 26 * u : edge + titleWidth + 26 * u, y + 38 * u, 10 * u, 0, Math.PI * 2);
    ctx.fill();
    y += Math.round(74 * u);
    if (view.subtitle) {
      ctx.fillStyle = COLOURS.dim;
      ctx.font = `400 ${Math.round(26 * u)}px ${FONT}`;
      ctx.fillText(wrap(view.subtitle, width, 1)[0] || '', edge, y);
      y += Math.round(44 * u);
    }
    y += Math.round(14 * u);

    const bottom = H - Math.round(36 * u);
    // One block of lines; returns false when it did not fit.
    const lines = (text, size, weight, colour, maxLines, gap) => {
      ctx.font = `${weight} ${Math.round(size * u)}px ${FONT}`;
      ctx.fillStyle = colour;
      const step = Math.round(size * u * 1.28);
      for (const line of wrap(text, width, maxLines)) {
        if (y + step > bottom) return false;
        ctx.fillText(line, edge, y);
        y += step;
      }
      y += Math.round(gap * u);
      return true;
    };
    const label = (text) => lines(text, 20, 700, COLOURS.label, 1, 2);

    // Something she is saying to everyone takes the whole board.
    if (view.say) {
      const top = y;
      ctx.fillStyle = COLOURS.panel;
      ctx.fillRect(pad - 18 * u, top, width + 36 * u, bottom - top);
      y += Math.round(22 * u);
      lines(view.say, view.dense ? 46 : 38, 500, COLOURS.title, 9, 0);
      state.draws++;
      return;
    }

    if (view.topic) {
      label(labels.now);
      lines(view.topic, view.dense ? 54 : 40, 600, COLOURS.title, view.dense ? 5 : 3, 20);
    }
    const counts = [];
    const section = (name, items, mark) => {
      const list = (items || []).filter(Boolean);
      if (!list.length) return;
      // A small tile shows the topic and how many there are, in big type.
      if (view.dense) { counts.push(`${list.length} ${labels[name].toLowerCase()}`); return; }
      if (y + 90 * u > bottom) return;
      label(labels[name]);
      let shown = 0;
      for (const item of list) {
        const before = y;
        if (!lines(`${mark} ${item}`, 30, 400, COLOURS.text, 2, 6)) { y = before; break; }
        shown++;
      }
      if (shown < list.length) lines(`+${list.length - shown} ${labels.more}`, 22, 400, COLOURS.dim, 1, 0);
      y += Math.round(12 * u);
    };
    section('decisions', view.decisions, '✓');
    section('tasks', view.tasks, '•');
    section('questions', view.questions, '?');
    if (view.dense && counts.length) lines(counts.join(' · '), 32, 400, COLOURS.dim, 2, 0);
    state.draws++;
  }
  draw();
  if (CONFIG.tickMs > 0) setInterval(draw, CONFIG.tickMs);

  // ── Koli's tracks ──────────────────────────────────────────────────────────

  let boardStream = null;
  let audioContext = null;
  let voiceDestination = null;

  function label(track, text, deviceId) {
    Object.defineProperty(track, 'label', { get: () => text });
    const settings = track.getSettings.bind(track);
    track.getSettings = () => ({ ...settings(), deviceId, groupId: 'koli' });
    return track;
  }

  function videoTrack() {
    if (!boardStream) boardStream = canvas.captureStream();
    const track = boardStream.getVideoTracks()[0].clone();
    try { track.contentHint = 'detail'; } catch (e) { /* older engines */ }
    return label(track, 'Koli Board', 'koli-board');
  }

  function audioTrack() {
    if (!audioContext) {
      audioContext = new AudioContext();
      voiceDestination = audioContext.createMediaStreamDestination();
    }
    return label(voiceDestination.stream.getAudioTracks()[0].clone(), 'Koli Voice', 'koli-voice');
  }

  // ── The replaced calls ─────────────────────────────────────────────────────

  const getUserMedia = async (constraints) => {
    const wants = constraints || {};
    state.calls.push({ at: Date.now() - state.started, video: !!wants.video, audio: !!wants.audio });
    const tracks = [];
    if (wants.video) tracks.push(videoTrack());
    if (wants.audio) tracks.push(audioTrack());
    if (!tracks.length) throw new TypeError('getUserMedia needs audio or video');
    return new MediaStream(tracks);
  };
  // On the object only, as it ran when Meet let her in (2026-11-18). A page
  // that reached the prototype's own function would get the browser's fake
  // device (a test pattern), never the real camera.
  md.getUserMedia = getUserMedia;

  const device = (kind, deviceId, text) => ({
    kind, deviceId, groupId: 'koli', label: text,
    toJSON() { return { kind, deviceId, groupId: 'koli', label: text }; },
  });
  md.enumerateDevices = async () => [
    device('videoinput', 'koli-board', 'Koli Board'),
    device('audioinput', 'koli-voice', 'Koli Voice'),
    device('audiooutput', 'default', 'Default'),
  ];

  // Screen sharing is not Koli's to start.
  md.getDisplayMedia = async () => { throw new DOMException('Not allowed', 'NotAllowedError'); };

  // ── The call's connections, kept to read what is being sent ────────────────

  const connections = [];
  const NativeConnection = window.RTCPeerConnection;
  if (NativeConnection) {
    window.RTCPeerConnection = new Proxy(NativeConnection, {
      construct(target, args) {
        const connection = new target(...args);
        connections.push(connection);
        return connection;
      },
    });
  }

  window.__koli = {
    state,
    // The new public view; returns how many times the board was drawn.
    board(view) { state.view = { ...state.view, ...view }; draw(); return state.draws; },
    resize(width, height) { canvas.width = width; canvas.height = height; draw(); return `${canvas.width}x${canvas.height}`; },
    info() { return { draws: state.draws, calls: state.calls, hidden: document.hidden, size: `${canvas.width}x${canvas.height}` }; },
    // The board as a picture, for a look at it without a call.
    picture() { return canvas.toDataURL('image/png'); },
    // What Meet has Koli send right now: one row per video layer.
    async sending() {
      const rows = [];
      for (const connection of connections) {
        if (connection.connectionState === 'closed') continue;
        for (const s of (await connection.getStats()).values()) {
          if (s.type === 'outbound-rtp' && s.kind === 'video') {
            rows.push({ active: s.active !== false, width: s.frameWidth || 0, height: s.frameHeight || 0, sent: s.framesSent || 0 });
          }
        }
      }
      return rows;
    },
  };
})();
