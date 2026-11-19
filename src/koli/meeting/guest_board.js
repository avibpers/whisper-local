// guest_board.js
// Runs in Koli's own browser before Google Meet's scripts (meeting/guest.py
// adds it). It gives Meet a camera and a microphone that are Koli's: the
// camera is a board drawn on a canvas (her notes at rest, a prompter when
// she says something), the microphone is silence, where her voice goes later. The real devices are
// never asked for: every path returns Koli's tracks or fails. It also keeps
// the page's call connections, to read which sizes Meet asks her to send.
// Design: docs/design/meet-participant.md.

(() => {
  if (window.__koli) return;
  const CONFIG = /*CONFIG*/{ width: 960, height: 720, tickMs: 1000, view: {} }/*END*/;
  const md = navigator.mediaDevices;
  if (!md) return;

  // ── The board ──────────────────────────────────────────────────────────────
  // Two faces. At rest: her notes (the topic now, decisions, tasks, open
  // questions). When she says something: a prompter. Her words come in at
  // reading pace and the text glides up as it grows, so nothing is cut however
  // long it is, and a frame around the tile pulses when she starts, to draw
  // the eye. Time moves in frame(): once a second on the page's own timer (a
  // hidden page gets no more), and ten times a second while something moves,
  // called by the app over the control channel (measured 2026-11-19: 10.8
  // frames a second from a hidden page that way, 1.5 with a page timer).

  const FONT = '"Segoe UI", Arial, sans-serif';
  const COLOURS = { back: '#10141c', bar: '#ff7a1a', title: '#ffffff', dim: '#9aa7bd', text: '#e8edf5', label: '#ff9d4d',
                    panel: '#1b2230', dotOff: '#5a3a20' };
  const LABELS = {
    ltr: { now: 'NOW', decisions: 'DECIDED', tasks: 'TASKS', questions: 'OPEN QUESTIONS', more: 'more',
           answer: 'to', chime: 'has something to add', thinking: 'is thinking' },
    rtl: { now: 'עכשיו', decisions: 'הוחלט', tasks: 'משימות', questions: 'שאלות פתוחות', more: 'נוספים',
           answer: 'אל', chime: 'רוצה להוסיף', thinking: 'חושבת' },
  };
  const THINKING = '…';           // what the app sends while she is working on an answer
  const REVEAL_CPS = 42;          // characters a second her words come in at: a comfortable reading pace
  const CATCH_UP_S = 1.5;         // or faster, to stay this close behind the words already written
  const PULSE_S = 3.0;            // how long the frame pulses when she starts
  const READ_LINE_S = 1.7;        // seconds a line, when a long answer glides by again
  const HOLD_S = 3.0;             // the rest at the top and at the bottom of that glide
  const RETURN_S = 0.6;           // the way back to the top

  const canvas = document.createElement('canvas');
  canvas.width = CONFIG.width;
  canvas.height = CONFIG.height;
  const ctx = canvas.getContext('2d');
  const state = { view: { title: 'Koli', subtitle: '', topic: '', decisions: [], tasks: [], questions: [], say: '', dense: false },
                  draws: 0, started: Date.now(), calls: [], moving: false };
  // What she is saying: the words, how many characters are out, where the
  // text has scrolled to, and her own clock (seconds), which only frame() moves.
  const say = { text: '', shown: 0, scroll: 0, clock: 0, startedAt: -1e9, doneAt: 0, last: performance.now() / 1000 };

  function isRtl(text) { return /[֐-׿؀-ۿ]/.test(text || ''); }

  // Breaks `text` into lines no wider than `width` in the current font. With
  // `maxLines` the last line allowed ends with … when words were left over;
  // without it every line is returned.
  function wrap(text, width, maxLines) {
    const words = String(text || '').split(/\s+/).filter(Boolean);
    const lines = [];
    let line = '';
    for (const word of words) {
      const next = line ? `${line} ${word}` : word;
      if (ctx.measureText(next).width <= width || !line) { line = next; continue; }
      lines.push(line);
      line = word;
      if (maxLines && lines.length === maxLines) break;
    }
    if ((!maxLines || lines.length < maxLines) && line) lines.push(line);
    if (!maxLines) return lines;
    const used = lines.join(' ').split(/\s+/).length;
    if (used < words.length && lines.length) {
      let last = lines[lines.length - 1];
      while (last && ctx.measureText(`${last}…`).width > width) last = last.slice(0, -1);
      lines[lines.length - 1] = `${last.trimEnd()}…`;
    }
    return lines;
  }

  // A new view from the app. New words that continue the ones on the tile
  // (an answer still being written) keep flowing; anything else starts again
  // from the top, with the pulse.
  function setView(view) {
    const next = String((view && view.say) || '').split(/\s+/).filter(Boolean).join(' ');
    const old = say.text;
    state.view = { ...state.view, ...view, say: next };
    if (next === old) return;
    const grows = old && old !== THINKING && next.startsWith(old);
    if (!grows) {
      say.shown = 0;
      say.scroll = 0;
      if (next && old !== THINKING) say.startedAt = say.clock;   // the dots already pulsed for this answer
    }
    say.text = next;
    say.doneAt = 0;
  }

  // Moves time on by `step` seconds (by the real time since the last frame
  // when not given) and draws. Returns whether anything is still moving, so
  // the app knows when it may stop calling.
  function frame(step) {
    const now = performance.now() / 1000;
    const dt = typeof step === 'number' ? step : Math.min(1.5, Math.max(0, now - say.last));
    say.last = now;
    say.clock += dt;
    const text = say.text === THINKING ? '' : say.text;
    let moving = say.text === THINKING || say.clock - say.startedAt < PULSE_S;
    if (say.shown < text.length) {
      const behind = text.length - say.shown;
      say.shown = Math.min(text.length, say.shown + Math.max(REVEAL_CPS, behind / CATCH_UP_S) * dt);
      if (say.shown >= text.length) say.doneAt = say.clock;
      moving = true;
    }
    if (draw(dt)) moving = true;
    state.moving = Boolean(say.text) && moving;
    return state.moving;
  }

  function draw(dt) {
    const W = canvas.width, H = canvas.height;
    const view = state.view;
    const u = Math.min(W, H) / 720;              // everything scales with the frame
    const pad = Math.round(Math.max(40, W * 0.07) * (W < 700 ? 0.8 : 1));
    const rtl = isRtl(view.say && view.say !== THINKING ? view.say : (view.topic || (view.decisions || [])[0] || (view.tasks || [])[0]));
    const box = { W, H, u, pad, width: W - pad * 2, rtl, labels: rtl ? LABELS.rtl : LABELS.ltr, edge: rtl ? W - pad : pad };
    ctx.direction = rtl ? 'rtl' : 'ltr';
    ctx.textAlign = rtl ? 'right' : 'left';
    ctx.textBaseline = 'top';
    ctx.fillStyle = COLOURS.back;
    ctx.fillRect(0, 0, W, H);
    state.draws++;
    return view.say ? drawSay(box, view, dt || 0) : drawNotes(box, view);
  }

  // The dot beside her name changes every second, so a frame always differs
  // from the last: a still canvas sends nothing (measured).
  function drawDot(x, y, radius) {
    ctx.fillStyle = Math.floor(say.clock) % 2 === 0 ? COLOURS.bar : COLOURS.dotOff;
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, Math.PI * 2);
    ctx.fill();
  }

  // ── At rest: her notes ─────────────────────────────────────────────────────

  function drawNotes(box, view) {
    const { W, H, u, width, rtl, labels, edge } = box;
    ctx.fillStyle = COLOURS.bar;
    ctx.fillRect(0, 0, W, Math.round(10 * u));
    let y = Math.round(40 * u);
    ctx.fillStyle = COLOURS.title;
    ctx.font = `600 ${Math.round(60 * u)}px ${FONT}`;
    ctx.fillText(view.title || 'Koli', edge, y);
    const titleWidth = ctx.measureText(view.title || 'Koli').width;
    drawDot(rtl ? edge - titleWidth - 26 * u : edge + titleWidth + 26 * u, y + 38 * u, 10 * u);
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
    return false;
  }

  // ── Saying something: the prompter ─────────────────────────────────────────

  // Returns whether the text is still on the move (gliding by again).
  function drawSay(box, view, dt) {
    const { W, H, u, pad, width, rtl, labels, edge } = box;
    const sign = rtl ? -1 : 1;
    const age = say.clock - say.startedAt;
    const thinking = say.text === THINKING;

    // The pulse: the whole tile warms up and a thick frame beats, then a
    // thin frame stays for as long as she has the word.
    const pulse = age < PULSE_S ? 1 - age / PULSE_S : 0;
    if (pulse > 0) {
      ctx.fillStyle = `rgba(255, 122, 26, ${(0.30 * pulse).toFixed(3)})`;
      ctx.fillRect(0, 0, W, H);
    }
    const beat = pulse > 0 ? Math.abs(Math.sin(age * Math.PI * 1.6)) : 0;
    const border = Math.round((6 + 22 * beat * pulse) * u);
    ctx.strokeStyle = COLOURS.bar;
    ctx.lineWidth = border;
    ctx.strokeRect(border / 2, border / 2, W - border, H - border);

    // The head: her name, the dot, and whom she is talking to.
    let y = Math.round(34 * u);
    ctx.fillStyle = COLOURS.title;
    ctx.font = `600 ${Math.round(46 * u)}px ${FONT}`;
    const name = view.title || 'Koli';
    ctx.fillText(name, edge, y);
    const nameWidth = ctx.measureText(name).width;
    drawDot(edge + sign * (nameWidth + 22 * u), y + 30 * u, 9 * u);
    const about = thinking ? labels.thinking
      : view.say_kind === 'chime' ? labels.chime
      : view.say_to ? `${labels.answer} ${view.say_to}` : '';
    if (about) {
      ctx.fillStyle = COLOURS.label;
      ctx.font = `500 ${Math.round(28 * u)}px ${FONT}`;
      const room = width - nameWidth - 60 * u;
      ctx.fillText(wrap(about, Math.max(room, 80 * u), 1)[0] || '', edge + sign * (nameWidth + 46 * u), y + 14 * u);
    }
    y += Math.round(78 * u);

    const top = y;
    const bottom = H - Math.round(34 * u);
    const area = bottom - top;
    const size = Math.round((view.dense ? 52 : 44) * u);
    const step = Math.round(size * 1.34);

    if (thinking) {
      // Three dots, one after the other, while she works on the answer.
      for (let i = 0; i < 3; i++) {
        const lift = Math.max(0, Math.sin(say.clock * 5 - i * 0.9));
        ctx.fillStyle = COLOURS.bar;
        ctx.beginPath();
        ctx.arc(edge + sign * (18 * u + i * 46 * u), top + 60 * u - lift * 16 * u, 13 * u, 0, Math.PI * 2);
        ctx.fill();
      }
      return true;
    }

    ctx.font = `500 ${size}px ${FONT}`;
    const all = wrap(say.text, width);           // every line: nothing is dropped
    // The words that are out so far, never half a word.
    let out = Math.floor(say.shown);
    if (out < say.text.length) {
      const space = say.text.lastIndexOf(' ', out);
      out = space > 0 ? space : 0;
    }
    const lines = [];
    for (const line of all) {
      if (out <= 0) break;
      lines.push(line.slice(0, out));
      out -= line.length + 1;
    }

    // Where the text stands. While words come in, the newest line is kept in
    // the lower part of the tile. Once all of a long answer is out, it rests,
    // goes back to the top and glides by again at reading pace, for whoever
    // looked up late.
    const done = say.shown >= say.text.length;
    const reach = Math.max(0, all.length * step - area * 0.94);     // how far a long answer scrolls in all
    let moving = false;
    if (!done) {
      const target = Math.max(0, lines.length * step - area * 0.80);
      say.scroll += (target - say.scroll) * Math.min(1, dt * 5);
      moving = true;
    } else if (reach > 0) {
      const glide = reach / (step / READ_LINE_S);
      const t = (say.clock - say.doneAt) % (HOLD_S + RETURN_S + HOLD_S + glide);
      if (t < HOLD_S) say.scroll += (reach - say.scroll) * Math.min(1, dt * 5);
      else if (t < HOLD_S + RETURN_S) say.scroll = reach * (1 - (t - HOLD_S) / RETURN_S);
      else if (t < HOLD_S + RETURN_S + HOLD_S) say.scroll = 0;
      else say.scroll = Math.min(reach, (t - HOLD_S - RETURN_S - HOLD_S) * (step / READ_LINE_S));
      moving = true;
    } else {
      say.scroll = 0;
    }

    ctx.save();
    ctx.beginPath();
    ctx.rect(0, top, W, area);
    ctx.clip();
    ctx.fillStyle = COLOURS.title;
    lines.forEach((line, i) => {
      const lineY = top + i * step - say.scroll;
      if (lineY > top - step && lineY < bottom) ctx.fillText(line, edge, lineY);
    });
    ctx.restore();

    // The text fades out where more of it is above or below.
    const fade = Math.round(46 * u);
    const shade = (from, to, y0) => {
      const gradient = ctx.createLinearGradient(0, y0, 0, y0 + fade);
      gradient.addColorStop(0, from);
      gradient.addColorStop(1, to);
      ctx.fillStyle = gradient;
      ctx.fillRect(pad / 2, y0, W - pad, fade);
    };
    if (say.scroll > 1) shade('rgba(16, 20, 28, 1)', 'rgba(16, 20, 28, 0)', top);
    if (lines.length * step - say.scroll > area + 1) shade('rgba(16, 20, 28, 0)', 'rgba(16, 20, 28, 1)', bottom - fade);
    return moving;
  }

  frame(0);
  if (CONFIG.tickMs > 0) setInterval(() => frame(), CONFIG.tickMs);

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
    board(view) { setView(view); frame(0); return state.draws; },
    // Moves the board's time on and draws; true while something is still moving. The app
    // calls it ten times a second while she talks (`step` seconds, for a look without a call).
    frame(step) { return frame(step); },
    saying() { return { shown: Math.floor(say.shown), of: say.text.length, scroll: Math.round(say.scroll), moving: state.moving }; },
    resize(width, height) { canvas.width = width; canvas.height = height; frame(0); return `${canvas.width}x${canvas.height}`; },
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
