// camera-inject.js  (THROWAWAY SPIKE)
// Runs in every page before its own scripts. Replaces the camera and the
// microphone with ones Koli owns: the camera is a board drawn on a canvas,
// the microphone is silence (a voice later). The real devices are never
// asked for: every path returns Koli's tracks or fails.
// Also keeps the page's call connections, to read what Meet asks Koli to send.

(() => {
  if (window.__koli) return;
  const CONFIG = /*CONFIG*/{ width: 1280, height: 720, tickMs: 1000 }/*END*/;
  const md = navigator.mediaDevices;
  if (!md) return;

  // ── The board ──────────────────────────────────────────────────────────────
  const canvas = document.createElement('canvas');
  canvas.width = CONFIG.width;
  canvas.height = CONFIG.height;
  const ctx = canvas.getContext('2d');
  const state = { title: 'Koli', subtitle: 'Listening', lines: [], card: false, draws: 0, started: Date.now(), calls: [] };

  // The test card: a ruler from each edge, so a picture of the tile shows
  // exactly how much of the frame a viewer's layout cut away.
  function drawCard(W, H) {
    const unit = Math.min(W, H) / 20;
    ctx.lineWidth = Math.max(2, unit / 12);
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'center';
    ctx.font = `600 ${unit * 0.8}px "Segoe UI", Arial, sans-serif`;
    for (let step = 1; step <= 9; step++) {
      const x = (W * step) / 10, y = (H * step) / 10;
      ctx.strokeStyle = step === 5 ? '#ff7a1a' : '#3a4558';
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
      ctx.fillStyle = '#ffd08a';
      ctx.fillText(String(step * 10), x, unit * 0.9);
      ctx.fillText(String(step * 10), x, H - unit * 0.9);
      ctx.fillText(String(step * 10), unit * 0.9, y);
      ctx.fillText(String(step * 10), W - unit * 0.9, y);
    }
    ctx.fillStyle = '#ffffff';
    ctx.font = `600 ${unit * 1.6}px "Segoe UI", Arial, sans-serif`;
    ctx.fillText(`${W} x ${H}`, W / 2, H / 2 - unit * 1.4);
    ctx.font = `400 ${unit}px "Segoe UI", Arial, sans-serif`;
    ctx.fillText(state.subtitle, W / 2, H / 2 + unit * 0.6);
    ctx.textAlign = 'start';
  }

  function drawBoard(W, H) {
    const unit = Math.min(W, H) / 720;   // everything scales with the frame
    ctx.fillStyle = '#ff7a1a';
    ctx.fillRect(0, 0, W, 10 * unit);
    ctx.textBaseline = 'top';
    ctx.fillStyle = '#ffffff';
    ctx.font = `600 ${76 * unit}px "Segoe UI", Arial, sans-serif`;
    ctx.fillText(state.title, 64 * unit, 48 * unit);
    ctx.fillStyle = '#9aa7bd';
    ctx.font = `400 ${36 * unit}px "Segoe UI", Arial, sans-serif`;
    ctx.fillText(state.subtitle, 64 * unit, 140 * unit);
    ctx.fillStyle = '#e8edf5';
    ctx.font = `400 ${44 * unit}px "Segoe UI", Arial, sans-serif`;
    state.lines.slice(0, 8).forEach((line, i) => ctx.fillText(line, 64 * unit, (220 + i * 58) * unit));
  }

  function draw() {
    const W = canvas.width, H = canvas.height;
    ctx.fillStyle = '#10141c';
    ctx.fillRect(0, 0, W, H);
    if (state.card) drawCard(W, H); else drawBoard(W, H);
    // A dot that changes on every tick, so a frame always differs from the last.
    const unit = Math.min(W, H) / 720;
    ctx.fillStyle = state.draws % 2 === 0 ? '#ff7a1a' : '#5a3a20';
    ctx.beginPath();
    ctx.arc(W / 2, H / 2 + 150 * unit, 14 * unit, 0, Math.PI * 2);
    ctx.fill();
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
  md.getUserMedia = async (constraints) => {
    const wants = constraints || {};
    state.calls.push({ at: Date.now() - state.started, video: !!wants.video, audio: !!wants.audio });
    const tracks = [];
    if (wants.video) tracks.push(videoTrack());
    if (wants.audio) tracks.push(audioTrack());
    if (!tracks.length) throw new TypeError('getUserMedia needs audio or video');
    return new MediaStream(tracks);
  };

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
    board(patch) { Object.assign(state, patch); draw(); return state.draws; },
    resize(width, height) { canvas.width = width; canvas.height = height; draw(); return `${canvas.width}x${canvas.height}`; },
    info() { return { draws: state.draws, calls: state.calls, hidden: document.hidden, visibility: document.visibilityState, webdriver: navigator.webdriver, ua: navigator.userAgent }; },
    // What Meet has Koli send right now: one row per video layer.
    async sending() {
      const rows = [];
      for (const connection of connections) {
        if (connection.connectionState === 'closed') continue;
        for (const s of (await connection.getStats()).values()) {
          if (s.type === 'outbound-rtp' && s.kind === 'video') {
            rows.push({ rid: s.rid || '', active: s.active, width: s.frameWidth || 0, height: s.frameHeight || 0, fps: s.framesPerSecond || 0, sent: s.framesSent || 0, kb: Math.round((s.bytesSent || 0) / 1024), limit: s.qualityLimitationReason || '', mode: s.scalabilityMode || '', codec: s.codecId || '' });
          }
        }
      }
      return rows;
    },
  };
})();
