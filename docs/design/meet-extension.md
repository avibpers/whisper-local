# Google Meet extension

The Koli Chrome extension (`chrome-extension/`) reads a Google Meet page and
tells the Koli app on the same computer what it sees. The app needs no voice
recognition to know:

1. who is in the call, and who is talking (from Meet's own tiles and captions);
2. what was said, while Meet's live captions are on (no Whisper needed);
3. when you join and leave the call (the meeting starts and ends with you);
4. what each named person sounds like: the call audio Meet attributes to one
   person teaches that person's voiceprint, saved under their real name, so
   Koli recognises them later in Zoom, Discord or any call no extension can read.

Setting: `meeting.google_meet` (Settings › Meetings › Google Meet extension):

| Value | Names, start/end, voiceprints | Transcript |
|---|---|---|
| `captions` (default) | from Meet | Meet's captions; Whisper skipped while captions are on |
| `names` | from Meet | Koli transcribes the audio as before |
| `off` | the extension is ignored | Koli as before |

## Flow

```
meet.google.com tab                      Koli app
───────────────────                      ────────
meet.js (content script)                 meeting/meet_link.py   HTTP 127.0.0.1:52881
  status / caption / left   ─► background.js ─POST /meet/events─►  MeetLink
                                                                  │
                                         services/meetings.py ◄───┘ _meet_events
                                           MeetFeed (meeting/meet_feed.py)
                                           ├─ _with_meet: joined calls join the detector's sources every tick
                                           ├─ session.call_left: leaving ends the meeting
                                           ├─ _meet_people: people → meeting['call'], Meet's title
                                           ├─ _caption_turn: final captions → turns (captions mode)
                                           └─ _meet_name_at → transcriber: name chunks, teach voiceprints
```

### The page (meet.js)

Meet's class names are obfuscated and change, and its words follow the UI
language, so the script relies only on:

- **Icons**, which are ligatures (the icon's text is its name in every language):
  in the call = `call_end` and a captions or chat icon are both visible (the
  lobby has `call_end` only). Captions on = the captions button's `aria-pressed`.
- **Tiles**: `[data-participant-id]` with the name in `span.notranslate`; yours
  holds `[data-self-name]`. Everyone seen stays listed (with `present`), plus
  anyone who spoke in captions without a tile on screen (big meetings).
- **Captions**: `div[role="region"][tabindex="0"]`. The layout is learned from
  the text Meet writes (TranscripTonic's method): a text node's element is the
  caption text, the element before it the speaker's name, their parent the
  block, its parent the list of blocks. A block is final 3 s after a newer one
  appears without it changing, 8 s after it stops changing when newest, when it
  scrolls away, or when you leave. Text added after a block was final becomes a
  new block.

Captions are turned on with Meet's own `c` shortcut, only when the button says
they are off and Koli wants them (`captions_wanted`), at most 3 tries.

Your own captions are labelled with a word for "You" in Meet's UI language
(`SELF_LABELS`) or your own name.

Leaving is seen when the call's buttons disappear (the "call ended" screen),
on `pagehide`, or when background.js sees the tab close or leave Meet. A click
on hang-up alone is not leaving: a host may cancel Meet's "end for everyone?"
choice.

### The link (meet_link.py, background.js)

Only the extension's service worker can reach `http://127.0.0.1:52881`
(`host_permissions`); a Meet page can't. The server accepts a request only with
the `X-Koli-Extension: 1` header and either no `Origin` or a
`chrome-extension://` one; preflights get no CORS headers, so no web page can
post. Bodies are capped at 1 MB and decoded as UTF-8 (Hebrew captions).
`GET /meet/hello` and every `POST /meet/events` answer with Koli's mode, state
and current meeting, for the popup and for `captions_wanted`.

The worker posts once a second (at once on join and leave), keeps up to 500
events while Koli is closed, and remembers each tab's call in
`chrome.storage.session`, since it may be stopped between messages.

The port opens in `MeetingService.ui_ready` (the app, never tests), closes on
shutdown, and follows the setting live.

### Start and end (session.py)

A call you joined enters the detector's sources on every tick (`_with_meet`).
A browser source of the same call keeps its output device, and both go by the
key `meet|<code>` so a "Not now" sticks. A joined source skips the speech
threshold and the countdown (`_call_ready(joined=True)`): auto starts at once,
ask offers. It still respects "Not now" and the quiet wait after you stopped
a meeting. `left` ends the meeting at once (`call_left`) instead of pausing
for `gap_minutes`. A tab not heard from for 30 s stops counting
(`HEARTBEAT_TIMEOUT_S`), and the usual source-gone rules apply.

### Names and voiceprints

`MeetFeed.name_at(code, t0, t1)` answers which other person was talking:
caption blocks give spans from first-seen minus 1 s (`CAPTION_LEAD_S`, captions
trail speech) to last change. It answers only when exactly one person's spans
overlap the stretch and cover at least half of it. Anything unsure is None:
a skipped chunk costs nothing, a voiceprint taught under the wrong name spoils it.

- `SpeakerTracker.named(name)` returns the speaker with that name (a speaker you
  renamed counts), or adds one with no voiceprint yet (`named_by_app`).
- `SpeakerTracker.teach(id, audio)` adds a chunk's embedding to that speaker.
  After `FIRM_COUNT` chunks it links to a saved voice it matches.
- The transcriber asks `name_at` before clustering. A one-voice chunk (or one
  voice of a split chunk) that Meet names goes to that person and teaches them
  (with at least 1 s of speech). Otherwise clustering works as before.
- In captions mode chunks are `voice_only`: no Whisper, and only one-voice
  loopback chunks Meet names teach a voiceprint.
- At meeting end `_keep_voices` saves each named speaker's voiceprint under
  their name in `voices.json`, like any speaker. `_merge_twins` keeps the real
  name when one voice was split between a Meet name and a "Speaker N".

### Captions as the transcript

While `meeting.google_meet` is `captions` and the extension says captions are
on, final caption blocks become turns. Yours become a `mic` turn as "You"
(dropped when "Include my side" is off or you excluded yourself). Everyone
else's become `loopback` turns under their Meet name. Whisper is skipped for
that meeting's chunks from the moment captions are on, so the start isn't
transcribed twice. If captions go off, Whisper takes over again.

### Where captions are read: the channel, then the page

Meet stops writing captions into its page while the tab is hidden (minimized,
covered, on another desktop) and writes them all at once on return. On
2026-11-03 that left 34 minutes with no lines, then 5164 words in one burst
under one timestamp. So there are two readers, and one rule for Koli:

- **The caption channel** (`meet_channel.js`, in the page's own world from
  document start). Meet receives captions on WebRTC data channels labelled
  `captions` and `captions_v2`, as undocumented protobuf: device, caption id,
  revision, a final mark, text. The script adds a listener and changes nothing.
  People come from the `collections` channel (found by shape, under wrappers
  Meet changes) and from the tiles, matched on the device id. It should keep
  flowing while hidden, since it isn't drawn; the log of the next real call
  tells (`hidden` counts captions that arrived with the tab hidden).
- **The page** (`meet.js`), as before, whenever the channel gives nothing that
  decodes. While hidden, what it holds is final and nothing more is read. For
  `BACKLOG_ABSORB_MS` after the return its text counts as already handed out.

The channel is given up for that tab when it speaks of someone nobody can name
while the tab is on screen, or goes silent for `CHANNEL_SILENT_MS` while the
page keeps writing. Until it has carried your own captions, those still come
from the page.

The status says `captions_live`: captions are on and can be read right now.
`MeetFeed.captions_cover(code, t0)` is the rule: a chunk is voice-only only if
captions are readable and were already when it began. So with the page reader,
Whisper transcribes the hidden stretch (names from voiceprints, a few seconds
of overlap possible at each end); with the channel it never needs to.

`MeetFeed` notes every change of tab, source and readability for the log, and
the channel's counts when the call ends.

### What the audio costs while captions are the transcript

Only the voiceprint model on `TEACH_CLIP_S` of a chunk Meet names (about 0.25 s
of CPU). The voice splitter isn't run: `name_at` already means one person and
nobody else. Elsewhere the splitter runs in its own low-priority process
(`meeting/diarize.py`), because sherpa-onnx's diarizer holds Python's lock for
its whole run: 5-8 s per 15 s chunk in the app froze every thread, including
the low-level keyboard and mouse hooks the whole PC's input waits on.

## Known limits

- The caption channel is undocumented and Meet may change it. What is expected
  to happen then is the page reader taking over, with the hidden-tab gap back.
- Your own voice isn't learned on the meeting microphone while captions are
  the transcript (`_check_you` doesn't run for voice-only chunks); it still is
  in dictation and in other meetings.
- Meet's "who is talking" highlight is not used. It exists only as rotating
  obfuscated classes (Vexa calls it "a known-bad foundation"), and a wrong
  span would teach the wrong voice. Captions carry the speaker's name instead,
  so names need captions on: in `names` mode the extension turns them on too.
- `SELF_LABELS` holds the "You" labels we know of; one missing for a UI
  language makes your captions show as a person with that label.
- Turn languages are left empty in captions mode (Meet doesn't say).
- Chrome only for now; any Chromium browser that loads unpacked extensions
  (Edge, Brave) should work the same.

## Sources

- TranscripTonic v3.4.4 (github.com/vivek-nexus/transcriptonic,
  `extension/content-scripts/google-meet/`): captions region, the
  text→name→block layout, the `c` shortcut, and icon ligatures for the lobby.
- Vexa (github.com/Vexa-ai/vexa, `core/meetings/modules/join/src/googlemeet/`):
  `[data-participant-id]` tiles, `span.notranslate` names, `[data-self-name]`,
  and junk participant names.
- Attendee (github.com/attendee-labs/attendee,
  `bots/google_meet_bot_adapter/google_meet_chromedriver_payload.js`): the
  labels and protobuf layouts of the caption and people messages.
