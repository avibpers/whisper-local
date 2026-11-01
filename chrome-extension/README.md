# Koli for Google Meet

A Chrome extension that tells the Koli app on this computer what happens in a
Google Meet:

- when you join and leave, so the meeting starts and ends with you;
- who is in the call;
- Meet's live captions, with each speaker's name.

Koli uses the names to label the transcript, and teaches each person's
voiceprint so it recognises them in Zoom, Discord and other calls. With
captions on, Meet's captions can be the transcript instead of Whisper.

Design: [docs/design/meet-extension.md](../docs/design/meet-extension.md).

## Install (unpacked)

1. Open `chrome://extensions` and turn on **Developer mode** (top right).
2. Click **Load unpacked** and choose this `chrome-extension` folder.
3. Pin "Koli for Google Meet" to see whether Koli is connected.

Koli must be running. It listens on `http://127.0.0.1:52881` while
Settings › Meetings › Google Meet extension is not Off.

## Files

| File | What it does |
|---|---|
| `manifest.json` | Manifest V3: the Meet content script, the worker, `127.0.0.1:52881` access |
| `meet.js` | Reads the Meet page: in the call or not, title, tiles, captions |
| `background.js` | Posts batches to Koli, notices a Meet tab closing |
| `popup.html` / `popup.js` / `popup.css` | Shows whether Koli answers and what it records |
| `icons/` | The Koli icon at 16, 32, 48 and 128 px |
