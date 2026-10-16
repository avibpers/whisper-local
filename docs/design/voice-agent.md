# AI actions: say what you want, Koli does it

*Design spec, 2026-10-14. Decisions made in conversation; implementation plan
in `docs/plans/2026-10-14-voice-agent-implementation.md`. Builds on
`docs/design/ai-providers.md` and `docs/design/wake-word.md`.*

## Goal

Work with the computer by speaking: "Hey Via, move today's photos from
Downloads to Pictures", "open the newest PDF in Documents". When no fixed voice
command matches, an AI model writes a short plan, Koli rehearses it, and runs
it. Everyday actions just happen, with Undo. Only dangerous ones stop and ask
for your spoken password. Requests you repeat become voice commands that run
with no AI at all.

## What was agreed

| Question | Decision |
|----------|----------|
| How it starts | The same way as voice commands: your own wake word, or the command hotkey. Fixed commands match first and run as today. No match: the AI takes it. |
| Who may start it | Only your own wake word (checked against your voiceprint) and the command hotkey. The generic "Hey Koli" works for any voice, so it never reaches the AI: unmatched sentences get "Sorry, I didn't understand". |
| Nonsense | Whisper unsure, or the AI says it isn't a request it can do: the bubble says "Sorry, I didn't understand" and what it heard. |
| What the AI writes | A short script that can call only Koli's own actions (below). It never touches files or the system directly. |
| Checks | Code check of the script (no AI involved), a rehearsal that lists exactly what would happen, a second AI call that checks the list against what you said, and a danger level computed from the list. |
| Approval | Your spoken password, in your voice (the words and your voiceprint). The bubble always has Cancel. |
| Undo | Every run is logged. Moves, copies, renames and new folders can be undone from the bubble, by saying "undo last action", or from the Activity list. |
| Repeats | The third run of the same plan in 14 days offers "Save it as a command?" with a phrase the AI suggests. The saved command runs the script fresh each time, through the same checks, without the AI. |
| First version | Files and folders, opening files, folders, apps and websites, your existing voice commands, and system commands the AI writes from its own knowledge of Windows or macOS (2026-10-15: "it should know how, not be taught each command"). Mail and reading aloud come later. |

## Flow

```
your word / command hotkey ──► Whisper ──► unsure? ──► "Sorry, I didn't understand"
                                   │
                         fixed command? ──yes──► runs as today
                                   │ no (and the source is yours, not generic "Hey Koli")
                                   ▼
                writer model ──► understood: false ──► "Sorry, I didn't understand" + what it heard
                                   │ script
                                   ▼
                code check (AST allow-list) ──fails──► "I couldn't do that safely"
                                   │
                                   ▼
                rehearsal: run the script with recording actions ──► list of operations
                                   │
                    changes anything? ──► reviewer model: "is this what was asked?"
                                   │
                    danger level of the list ──► approval needed? ──► bubble + your password
                                   │
                                   ▼
                replay the list for real (each source checked again), log it ──► "Moved 37 files · Undo"
                                   │
                    3rd same plan in 14 days ──► "Save it as a command?"
```

The script runs only once, in rehearsal. What runs for real is the list of
operations the rehearsal produced and the policy, the reviewer and your
approval judged. Each operation checks its source still exists; a missing one
is skipped and reported, never replaced by another file.

## Koli's actions (what a script can call)

Paths are plain strings or the file and folder records the actions return.
Records have `.name`, `.path`, `.ext`, `.size`, `.modified` (ISO date),
`.age_days` and `.folder`; nothing else.

| Action | Does |
|--------|------|
| `folder(name)` | A known folder: `downloads`, `desktop`, `documents`, `pictures`, `music`, `videos`, `home`, or a drive like `"D:"` |
| `find_files(where, kind=, name_contains=, ext=, newer_than_days=, older_than_days=, subfolders=False)` | Files, at most 500, three folders deep, 3 seconds. `kind`: image, video, audio, document, archive |
| `find_folders(where, name_contains=, subfolders=False)` | Folders, same caps |
| `newest(items, count=1)`, `oldest`, `largest` | Sorted picks |
| `last()` | What the previous action made, moved or renamed, where it is now (empty after 5 minutes, after Undo, or when it's gone) |
| `open(target)` | A file, folder or `https://` link. Refuses programs and scripts (`.exe`, `.bat`, `.cmd`, `.ps1`, `.vbs`, `.js`, `.msi`, `.lnk`, `.scr`, `.com`, `.reg`) |
| `open_app(name)` | An app by name, found among Start-menu shortcuts. Never a path |
| `copy(items, to)`, `move(items, to)` | Never overwrites: a taken name gets " (2)" |
| `rename(item, new_name)` | A name only, no folders in it |
| `make_folder(where, name, new=False)` | Returns the folder. One already there is reused and noted (the bubble says so); `new=True` always makes one, at a free name like "New folder (2)" |
| `delete(items)` | To the Recycle Bin, never permanent |
| `system(command, undo="", before="")` | A command the AI writes itself (PowerShell 5.1 on Windows, zsh or AppleScript on a Mac) for everything that isn't files: sound, screen, network, settings pages, questions about the computer. Its output is the answer in the bubble. `undo` reverses it; `before` prints the value it changes, which fills `{before}` in `undo` (a plain value only). At most 5 per request |
| `run_command(phrase)` | One of your fixed voice commands (not an AI-made one) |
| `say(text)` | A short answer shown in the bubble |

Built-ins: `len`, `range` (up to 1000), `str`, `int`, `min`, `max`, `abs`,
`round`.

## The code check

An allow-list over the script's syntax tree, the only thing that decides
whether it may run:

- Allowed: assignments, `for` (over lists from the actions, or `range`),
  `if`/`elif`/`else`, `break`/`continue`, calls of the actions and built-ins by
  name, comparisons, `and`/`or`/`not`, `+ - * /`, f-strings, lists, constants.
- Rejected: imports, attribute access other than the record fields above,
  any name or string containing `__`, `lambda`, comprehensions, `while`,
  `with`, `try`, function or class definitions, `global`, `**`, numbers over
  one million.
- It runs with only the actions and built-ins as globals, no `__builtins__`,
  at most 20000 line steps, 1000 operations (above the 500 files a search
  finds, so "move all 495 pictures" is one request; it needs the password as
  more than 50 files) and 10 seconds.

## Danger levels (from the operation list)

| Operations | Level |
|------------|-------|
| open, open_app, run_command, say, finding | runs |
| copy, move, rename, make_folder inside your folders, 50 files or fewer | runs, with Undo |
| delete | password |
| more than 50 files in one run | password |
| anything outside your folders | password |
| the reviewer says it doesn't match what you said | password, with its reason |
| a system command rated "read" (reads, shows, opens a window or settings page) | runs |
| a system command rated "setting" with an undo (volume, brightness, dark mode, Wi-Fi) | runs, with Undo |
| a system command rated "setting" without an undo, "change", "danger", or not rated | password |
| a system command (or its undo or `before`) on the danger list | password, whatever the rating |

**The danger list** (`agent/system.py`) is Koli's own, checked with no AI:
deleting; writing, moving or copying files (the file actions do that, with
the Recycle Bin and Undo); erasing disks; saving downloaded files; sending
data to the internet (POST, a body, or a web address built from `$`
variables); hidden or encoded code; installing or removing software; closing
programs; shutting down; security, firewall and permission changes;
scheduled tasks and services; running as administrator; another shell
(`cmd`, `powershell`, `wscript`…); a program from a folder, a script or an
installer; the machine registry (`HKLM`) and startup keys; .NET file,
environment and registry calls; `DllImport`; `wmic`; any `Remove-`,
`Disable-` or `Clear-` command. Reading a little text from a fixed web
address ("what's my public IP") is not on it. A system command runs in PowerShell with no
profile and no prompts, without a window, for at most 20 s.

Your folders: Desktop, Documents, Downloads, Pictures, Music, Videos, OneDrive
folders and the rest of your home folder except AppData, and every drive other
than the Windows drive.

## The models

`llm.complete('commands', …)`, its own route in Settings › AI (`ai.commands`).

**Writer** gets: the sentence, the action catalog, your system ("Windows 11,
PowerShell 5.1"), your folders' names, today's date, your fixed command
phrases, and the last five runs' summaries. Never file names: discovery
happens in the rehearsal. It answers
`{"understood", "reply", "summary", "command_name", "script"}`. When it doesn't
know a reliable way, it says so in `reply`, in one sentence: the bubble shows
it, and Activity keeps it as "couldn't do", so what's missing is visible.

**Reviewer** gets the sentence and the operation list (file names included,
marked as data) and answers `{"matches": bool, "reason", "commands"}`, where
`commands` explains and rates each system command in order: `explain`, `done`
(the bubble's words before and after), `risk` (read, setting, change, danger)
and `why`. It is called for every plan with a system command, saved commands
included. An instruction hidden in a file name can't make anything run,
because what runs is the list it was shown and the policy judged; a rating
can't let a command on the danger list through.

## Approval

The password is recorded on the Commands screen like the wake word: typed,
then said three times. Koli keeps the phrase, the ways Whisper wrote it (it
may never write it as typed: "Cooley approve blue"), and a voiceprint of you
saying it per microphone, all encrypted with the AI keys
(`llm/credentials.py`), never in the settings file. Recording the same phrase
on another microphone adds to it; a new phrase starts over. On a microphone
without a recording, the wake word's voiceprint is used instead.

When an action needs it, the bubble shows what will happen ("Delete 12 files
from Downloads") and "Say your password". Koli listens once (8 seconds): every
word must match the phrase or one of Whisper's recorded spellings (0.7 alike
each, so a spelling Whisper varies passes but "approve green" for "approve
blue" doesn't; joined words compare at 0.9), and the voice must match your
recordings of the password on this microphone at your wake word's "Sounds
like you" level, never below 0.45. Whisper isn't told the phrase. Two tries.
The log has the voice score and, when approval is needed, the reasons.

Also protected: your known folders, your home folder and drive roots are
never moved, renamed or deleted; open() opens only documents, pictures,
music, videos, web pages and folders (an allow-list: `.py` would run); delete
refuses drives without a Recycle Bin (USB, network shares, which count as
outside your folders).

No voiceprint on this microphone (you use the hotkey and never recorded a
word): the bubble shows an **Approve** button and says why. No password set:
nothing runs, and the bubble offers to set one.

## Undo

`%APPDATA%\koli\agent\history.jsonl` keeps the last 500 runs: sentence, script,
operations, what each did, the outcome. Undo reverses a run's operations in
reverse order: a move goes back (to a free name if the old one was taken), a
copy goes to the Recycle Bin, a rename goes back, a new empty folder is
removed. A delete can't be restored automatically: undo opens the Recycle Bin
and says which files to restore.

A follow-up is one action with what it followed: undoing either undoes both,
newest first (the rename goes back, then the folder it renamed is removed).

"Undo" alone stays Ctrl+Z (a shipped command). The agent's undo is "undo last
action" (also in Hebrew), which wins over "undo" because longer phrases match
first.

## Follow-ups

"Make a new folder in Downloads", then "call it Invoices": within 5 minutes
of an action that made, moved or renamed something, the writer is told what
you said, what it did, and how many files and folders `last()` returns (no
names or paths), and the reviewer sees the earlier request too, so "it" makes
sense. A script that calls `last()` is saved with `follows` (the earlier
run's id), never offered as a command, and undone together with it.

## Questions

The writer may ask one short question instead of guessing when something
it needs is missing or could mean two things ("Which file should I rename,
and to what?"), with `question` in its answer and no script. Koli shows it
in the bubble (`ask`, with Cancel) and listens for the answer at once, with
no wake word: up to 10 s, ended by a 1.2 s pause, and in your voice when your
wake voiceprint is on this microphone. The answer goes back to the writer as
`conversation` with the request.

The rehearsal asks too: when a script got nowhere because a folder name
wasn't found and a real one is close ("API" for `avi`, 0.5 alike), Koli asks
"There's no folder “API” in ~\Downloads. Did you mean “avi”?" itself. A yes
rehearses again with that name (the writer never sees it); any other answer
goes to the writer with the question. At most two questions per request; no
answer, or Cancel, does nothing. The history keeps the questions and answers.

## Listening to a sentence

A first command after your own word, with AI actions on, may be a whole
sentence: it isn't cut at 4 s (up to 20 s while you talk), and only a 2 s
pause ends it. At each short pause (0.45 s, at most two), Koli checks quietly
whether what you said so far is exactly one of your fixed commands' phrases;
if it is, the command runs then, as fast as before. "Open downloads and move
the pictures there" doesn't run "open downloads" at the pause, because the
check needs the whole phrase, not one inside it. "Hey Koli", follow-ups and
AI actions turned off keep the short limits.

## Repeats become commands

Each run has a signature: the sequence of actions called with their literal
arguments, file lists left out. On the third run with the same signature in
14 days (not already saved, not declined) the bubble offers to save it, with
the writer's `command_name`. Saving adds a command with a `script:` action to
`commands.yaml`, marked `made_by: ai`. Running it skips the writer only: code
check, rehearsal, policy and approval run every time.

## Bubble

A new `agent` event: `thinking`, `approve` (summary, password or click),
`working`, `done` (what was really done, from the results, in the past
tense, with Show and Undo), `nothing` (nothing matched, or what was already
there), `failed`, `not_understood` (what it heard), `offer_save` (phrase;
Save, Not now, Never).

A message of up to 7 words (48 letters) is one row: badge, words, buttons.
A longer one is a 380 px card: the words wrap 7-8 a line, a small icon sits
in the first line, and the buttons go underneath. The bubble window follows
its size. A message stays up 4 s plus about 90 ms a letter (never less than
its phase's minimum, 12 s for done), and longer while the mouse is on it.
When the model says why it can't do something, that sentence is the message.

The bubble's text comes from what ran, never from the script's `say()`: a
script writes its `say()` before anything happens, so it may claim a change
that didn't happen. `say()` is shown only for answers with no change. **Show**
opens Explorer at the last thing the run made, moved or renamed, with it
selected; the place comes from the run's own results in the history, never
from the page.

## Commands screen

An **AI actions** card: on/off, the model, the approval password (set, change,
test), and **Activity**: the last 20 runs with what they did and Undo. Saved AI
commands appear among the voice commands as "AI script".

## Settings

`ai.commands.enabled` (on), `ai.commands.model` ('' = default),
`ai.commands.timeout` (40 s).

## Code

- `agent/actions.py`: the actions, records, caps, rehearsal and real modes, undo.
- `agent/sandbox.py`: the code check and the run.
- `agent/policy.py`: your folders, danger levels.
- `agent/system.py`: the danger list, running a system command, your system's name.
- `agent/planner.py`: writer and reviewer prompts and parsing.
- `agent/journal.py`: history, undo, signatures, repeat offers.
- `agent/approval.py`: password setup and check.
- `agent/runner.py`: the flow above, events to the bubble.
- `platform/*/files.py`: Recycle Bin (`SHFileOperationW` on Windows), Start-menu
  apps, opening the Recycle Bin; macOS stubs.
- Hooks: `state_manager` (sources, hand-off, no retry when the AI takes it),
  `voice_commands` and `services/commands` (`script` and `agent` actions),
  `commands.defaults.yaml` ("undo last action"), bridge, `bar.js`, the
  Commands screen, schema and defaults.

## Testing

Unit tests on a temporary folder with canned scripts and no model: every
action in rehearsal and for real, never overwriting, caps, executables
refused; the code check with escape attempts (`__class__`, `getattr`, `open`,
`eval`, `import`, a ten-million-step loop); danger levels; replay skipping a
vanished file; undo of each operation; signatures and the third-time offer;
the runner with fake models (not understood, mismatch, approval granted,
refused, timed out); the hook (generic "Hey Koli" never reaches the AI, a
fixed command still wins). Then a live run with Luna, then your real test.
