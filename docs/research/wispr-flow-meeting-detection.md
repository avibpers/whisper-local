# How Wispr Flow detects meetings and speakers (Windows)

Read from the installed Wispr Flow 1.6.1174 (`%LOCALAPPDATA%\WisprFlow`) on
2026-10-20: the Electron main bundle (`resources\app.asar` →
`.webpack\main\index.js`) and the strings of its .NET helper
(`resources\Release\Wispr Flow Helper.exe`). The goal is to know which
signals to borrow for Koli's meeting mode, platform by platform.

Two kinds of claims below: **verified** means read in the code or binary;
**inferred** means it follows from names and needs testing.

## Shape

- `Wispr Flow Helper.exe` (.NET, about 20 MB) does all the Windows work: Core
  Audio, the registry, UI Automation. It talks JSON to the Electron main
  process (`MicrophoneHoldersChanged`, `MeetingSpeakerChange`,
  `StartSpeakerPolling`, ...).
- The Electron side holds every decision: which app, whether to show the card,
  when the call ended, speaker names.

## 1. Detecting a call before anyone speaks (verified)

The signal is **who holds the microphone**, not sound. Joining a Discord
voice channel opens Discord's capture stream at once, even in silence and
even when muted in the app, so the call is seen at the moment of joining.

The helper reads mic holders from four sources (`Source` enum):

| Source | What it is |
|---|---|
| `audio_session` | WASAPI capture sessions: `IAudioSessionManager2` + `IAudioSessionNotification` / `IAudioSessionEvents` on capture endpoints (`CoreAudioMicrophoneActivitySource`), event driven |
| `consent_store` | `HKCU\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone\NonPackaged\<exe path with # for \>`: `LastUsedTimeStart` set and `LastUsedTimeStop == 0` means in use now (`ConsentStoreMicrophoneActivitySource`, with a registry watcher that re-arms) |
| `process_api` | The OS process-level "is using the mic" answer (mainly macOS) |
| `running_apps_heuristic` | A guess from running apps, untrusted |

It also snapshots output sessions (`OutputHolders`,
`TrySnapshotOutputAudioSessions`), but those only back up the end-of-call
logic.

### From holder to platform

`index.js`, module 71847 + 20775:

1. Drop Wispr itself; prefer holders from `process_api` / `audio_session`.
2. Map each holder's exe to a platform:
   `discord.exe → discord`, `zoom.exe`/`cpthost.exe → zoom`,
   `ms-teams.exe`/`teams.exe → teams`, `slack.exe`, `webex.exe`,
   `ciscocollabhost.exe`, `whatsapp.exe`, `wemeetapp.exe`, `lark.exe`,
   `gather.exe`, `dialpad.exe`, `aircall.exe`, `tuple.exe`, `clickup.exe`,
   plus the browsers (`chrome.exe`, `msedge.exe`, `brave.exe`, `opera.exe`,
   `opera_gx.exe`, `vivaldi.exe`, `firefox.exe`, `arc.exe`, `zen.exe`,
   `comet.exe`, `dia.exe`).
3. The first trusted holder that maps to a platform wins; its
   `signal` is `trusted_holder`, otherwise `guessed_holder`.

### What happens for Discord

- Any mapped holder, Discord included, makes the "Meeting detected" card
  appear with the platform name as subtitle ("Discord"). The user clicks to
  record.
- **Auto-start** (recording with no click) is gated separately. A platform
  qualifies on its own only if it is a dedicated meeting app
  (`zoom, teams, webex, facetime, gather, lark, voov, dialpad_meetings`), a
  browser on a meeting tab (Meet / Zoom / Teams URL), or Slack in a huddle.
  Discord is `false` in that table, so **Discord auto-starts only when a
  calendar event matches**. Without one it prompts.
- Browser Discord counts only on `discord.com/channels/...`; Slack only on a
  `/huddle/` URL.

### Browsers

A browser holding the mic is not enough (`no_join_evidence`). Wispr scans
tabs through UI Automation (`MeetingTabScanner`) for the URL and title: the
Meet code `abc-defg-hij`, `zoom.us/j/<id>`, Teams `/l/meetup-join` and `/meet`.
Chrome's tab title suffixes ("Microphone recording", "Camera and microphone
recording") are stripped by the same regex Koli uses.

### End of a call (verified)

- Holder polling every 3 to 5 s (30 s when idle); a drop counts after
  **2 polls and 1.5 s** without the holder (`absenceConfirmMs`). Wake from
  sleep, a service restart and a default-mic change each get a 10 s holdoff.
- `ultraHolders: [slack, discord, facetime, whatsapp]`: for these, the app
  releasing the mic is trusted as the end of the call, with no extra proof.
  For Zoom, Teams and browsers they also look for proof: a click on "Leave
  call", the tab gone, or the UI no longer showing call controls.
- Transcript cues help too: silence on mic and system, and sign-off phrases
  ("thanks everyone", "talk to you later", "bye everyone", ...).

## 2. Recognising speakers (verified, except where marked)

Three layers:

1. **Two channels.** The mic is "you" and system audio is "them"
   (`speaker.source: mic | system`). Echo of the far end on the mic is
   tracked separately.
2. **Reading the call window with UI Automation** (`MeetingSpeakerPoller`,
   `WindowsMeetingSpeakerSnapshotSource`). The helper binds to the meeting
   window, walks its tiles and reports `speakingNames`, `mutedNames`,
   `selfName`, `participantCount` on every change. Each change is stamped
   against the transcript's timestamps. Classifiers exist for Zoom
   (`ZoomMeetingSpeakerClassifier`, "Active speaker"), Teams
   (`TeamsSpeakerClassifier`, compact view), Google Meet in a browser, and
   Slack huddles (UIA `ClassName` `p-huddle_peer_tile__overlay--active_speaker`).
   Chromium and Electron expose an element's HTML `class` as UIA
   `ClassName`; that is how the Slack and Meet tiles are recognised
   (`tileRootClasses`, `markerClasses` are pushed from remote config).
   Speaker polling starts only for Zoom, Teams, Slack and browser meetings
   (`pn()` in `index.js`). **There is no Discord reader.**
3. **Server refine.** The recording goes up as Opus; the server returns a
   refined transcript with `Speaker N` labels (states `diarized`,
   `attributed`). A `speakerMap` then names each label from the UIA
   observations and the calendar attendees. The user edits what is left.

So a Discord meeting in Wispr gets: you vs. the others from the two
channels, the others split by cloud diarization, and names typed by hand.
Koli's local `diarize.py` and `VoiceBook` (voices remembered across meetings)
already do better than that for Discord.

## 3. Tested on this PC with live Discord calls (2026-10-20)

Three DM calls with one other person, watched with probes for the capture
and render sessions, Discord's own log, and Discord's window through UI
Automation.

### The call itself: reliable

| Event | Capture session (`Discord.exe`) | Discord's `renderer_js.log` |
|---|---|---|
| Call 1 start | active 13:27:35 | connected 13:27:34.9 |
| Call 1 end | released | `VOICE_DISCONNECT` 13:30:54.7 |
| Call 2 start | active 13:33:44 | connected 13:33:44.0 |
| Call 2 end | released 13:35:26 | `VOICE_DISCONNECT` 13:35:26.1 |
| Call 3 start | active 13:37:25 | connected 13:37:25.4 |

- **The capture session goes active the second the call connects**, with
  nobody speaking, and is released within a second of hanging up.
- The render session is weaker: it also lights for join sounds and pings
  outside a call (13:27:31, 13:33:40).
- **The session follows Discord across microphones.** At 13:29:39 Discord
  moved from the Logitech headset mic to the BY-V20 mic mid-call; one
  session went inactive and another active in the same second. Detection
  must key on the app, not on one device.
- **The ConsentStore does not work for Discord.** The current version
  (`app-1.0.9263`) has no key at all, and no Discord key showed "in use"
  during any call. Older versions have keys (one per version folder, e.g.
  `...#Discord#app-1.0.9261#Discord.exe`). Drop it as a Discord source.
- Unrelated apps sit "in use" permanently (EMEET Studio, NVIDIA Broadcast,
  python), so "someone holds the mic" means nothing without the
  meeting-app list.

### Discord's own log: exact, but IDs only

`%APPDATA%\discord\logs\renderer_js.log` has, with millisecond times:

- `RTCConnection(<channel id>) ... CONNECTING => CONNECTED`: call start
- `Creating user: <user id> with audio SSRC: <n>`: someone joined (SSRC 0
  first, the real one when their audio starts)
- `[VOICE_DISCONNECT]` and `Destroy RTCConnection`: call end

It has user and channel IDs, never names, and no per-person speaking (only a
`userSpeakingChange: Reduced framerate` note).

### Discord's window: names yes, speaking no

Discord exposes its accessibility tree to UI Automation, with HTML classes as
`ClassName` (`avatar__183c2`, `participant_c8dbe9`).

- **Who is in the call:** the DM list shows `In a call (Becky)` and
  `Becky (direct message), Ongoing Call` even while another chat is open.
  With the call conversation open there is one tile per person
  (`participant_c8dbe9`) with a `focusTarget__54e4b` child named after the
  user (`avi151105`, `v069675`).
- **Mute state** appears in that name: `v069675, Muted`.
- **Speaking is not exposed.** The tiles were read 4 times a second while
  the far end spoke at a peak level of 0.73 (13:41:00.9 to 13:41:03.4):
  name, class, ARIA properties, item status, help text and description
  stayed identical. The green ring is drawing only.
- The tiles only exist while the call's conversation is on screen; a full
  window read takes 1 to 4.7 s. Too slow and too fragile for timing anyway.

**Conclusion:** for Discord, the window gives the meeting's name and the
list of people; who speaks when must come from the audio (Koli's voice
splitting and voiceprints).

## 4. What Koli could take

Koli today (`meeting/detector.py`) keys on **output** sessions: an app with
an active render session that matches a meeting rule. That already sees
Discord at join. A capture session is the sharper signal: only apps actually
using the mic hold one, while anything that plays sound holds a render
session.

1. **Capture sessions as the main signal.** Read `EDataFlow.eCapture`
   sessions in `platform/windows/audio_sessions.py` next to the render ones;
   a meeting-rule app holding the mic is a call. Keep render sessions as
   corroboration.
2. **ConsentStore: not for Discord.** The test showed no key for the current
   Discord version. It may still help Zoom or Teams; check each app before
   relying on it.
3. **Events instead of the 1 s poll.** `IAudioSessionNotification` for new
   sessions and `IAudioSessionEvents.OnStateChanged` per session, as Wispr
   does; keep a slow poll as a backstop.
4. **End of call per platform.** For Discord, Slack and WhatsApp, the mic
   release is the end (2 polls, 1.5 s). For Zoom, Teams and Meet, wait for
   corroboration (the window or tab gone, the "Leave" click), since those
   apps release the mic briefly when switching devices.
5. **Discord names from the window, speakers from the voice (tested).**
   Read the call's people from Discord's window (`In a call (...)`, the
   participant tiles) to name the meeting and offer the names; Discord
   doesn't expose who is speaking, so voiceprints stay the way to tell
   voices apart. In a 1:1 call the far-end voice gets the one other name
   directly.
6. **Discord's log as a free corroborator.** `renderer_js.log` gives the
   exact connect and disconnect times and each join; tailing it confirms a
   call start or end without any audio API.
