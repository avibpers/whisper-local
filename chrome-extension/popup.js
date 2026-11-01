// popup.js
// Asks background.js whether the Koli app answers, and shows what Koli does
// with Meet and the meeting it is recording, in plain words.

const MODES = {
  captions: "Names and captions (Meet's captions are the transcript)",
  names: 'Names only (Koli transcribes the audio)',
  off: 'Nothing: turned off in Koli',
};
const STATES = { live: 'Recording', paused: 'Paused', offered: 'Waiting for you to start it', idle: 'None now' };

function show(id, text) {
  document.getElementById(id).textContent = text;
}

function render(answer) {
  const dot = document.getElementById('dot');
  const hint = document.getElementById('hint');
  if (!answer || !answer.koli) {
    dot.className = 'dot off';
    show('link', 'Koli is not running');
    hint.hidden = false;
    hint.textContent = 'Start the Koli app on this computer. Meet calls are passed on once it runs.';
    return;
  }
  const reply = answer.reply || {};
  const meeting = reply.meeting;
  dot.className = `dot ${reply.state === 'live' ? 'rec' : 'ok'}`;
  show('link', 'Connected to Koli');
  document.getElementById('details').hidden = false;
  show('mode', MODES[reply.mode] || reply.mode || '');
  show('meeting', meeting ? `${STATES[reply.state] || reply.state}: ${meeting.title} (${meeting.turns} turns)`
                          : STATES.idle);
  hint.hidden = reply.mode !== 'off';
  hint.textContent = 'Change this in Koli: Settings › Meetings › Google Meet extension.';
}

chrome.runtime.sendMessage({ kind: 'hello' }, render);
