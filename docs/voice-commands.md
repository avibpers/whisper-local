# Voice Commands

Voice command mode lets you trigger actions by speaking. Press **Alt+Win** (default) to start recording, speak a trigger phrase, then press the stop key. If the transcription matches a trigger, the associated action runs.

Or skip the hotkey: turn on the [wake word](#wake-word) and say **"Hey Koli, next song"**.

## Configuration

Voice commands are configured in `commands.yaml`, located in your config folder:
- **Windows:** `%APPDATA%\koli\commands.yaml`
- **macOS:** `~/.koli/commands.yaml`

You can open this folder from the system tray menu: **Open Config Folder**.

## Format

Each command has a **trigger** phrase and exactly one action: `run`, `hotkey`, or `type`.

```yaml
commands:
  # Send a keyboard shortcut
  - trigger: "undo"
    hotkey: "ctrl+z"

  # Deliver pre-written text
  - trigger: "my email"
    type: "user@example.com"

  # Run a shell command
  - trigger: "open notepad"
    run: 'notepad.exe'
```

| Field | Description |
|-------|-------------|
| **trigger** | Phrase to match (case-insensitive, punctuation ignored). It is also the command's name: a shipped command is changed by naming its trigger |
| **other_triggers** | More phrases for the same command, in any language: `["תפעיל ספוטיפיי", "start spotify"]` |
| **run** | Shell command (`cmd.exe` on Windows, `/bin/sh` on macOS) |
| **hotkey** | Keyboard shortcut to send (e.g. `ctrl+z`, `win+left`) |
| **send_to** | Where a `hotkey` goes: left out for the active window, `windows` for no app, or an app's `.exe` |
| **type** | Pre-written text to deliver to the active window |

## Matching

- Triggers are matched as substrings — saying "please open notepad" matches `"open notepad"`
- Longer triggers are checked first, so `"open notepad plus plus"` won't accidentally match `"open notepad"`
- First match wins
- `other_triggers` are matched the same way, and compete on length with every other phrase
- Hebrew and other scripts match as written (Whisper writes Hebrew in Hebrew letters)

```yaml
  - trigger: "play spotify"
    other_triggers: ["תפעיל ספוטיפיי"]
    run: "start spotify:"
```

### Spoken numbers: `{n}`

Put `{n}` in a trigger where you say one number from 0 to 9, as a digit or a word. The same `{n}` in the `hotkey` or `type` is replaced by that number, so one command covers ten:

```yaml
  # "D 1" presses Ctrl+Shift+1, "D two" presses Ctrl+Shift+2
  - trigger: "d {n}"
    hotkey: "ctrl+shift+{n}"
```

"D 1", "D1", "D-1" and "D one" all match; "D 12" does not. `{n}` appears once per trigger and is never put into a `run` command. In the app, record the keys with any number and switch on **Last key = the number you said**.

## Command Types

### Hotkey commands

Send keyboard shortcuts to the active window. Use `+` to combine keys.

```yaml
  - trigger: "undo"
    hotkey: "ctrl+z"
  - trigger: "select all"
    hotkey: "ctrl+a"
  - trigger: "snap left"
    hotkey: "win+left"
  - trigger: "show desktop"
    hotkey: "win+d"
```

#### Where the keys go: `send_to`

By default the keys go to the app in front. When that app would catch them first, aim them elsewhere:

- `send_to: windows`: Koli brings an invisible window of its own to the front, presses the keys there, and returns you to your window. No app gets them, so only Windows' shortcuts and other programs' global shortcuts (a Discord or OBS hotkey) react. For a keyboard-language shortcut, keep the app you're in: Windows switches the language of the window in front. (The desktop isn't used: it reacts to keys like Ctrl+Shift+1, which changes its icon size.)
- `send_to: chrome.exe`: Koli switches to that app's most recently used window, presses the keys, and switches back. If the app isn't open, nothing is pressed and Koli says so.

```yaml
  - trigger: "mute discord"
    hotkey: "ctrl+shift+m"
    send_to: windows
  - trigger: "new tab"
    hotkey: "ctrl+t"
    send_to: chrome.exe
```

In the app, pick **Send to** next to the keys. "A specific app…" lists the apps open right now. Windows only; on macOS a command with `send_to` doesn't run and says why.

### Type commands

Deliver pre-written text to the active window using the same method as transcription (clipboard paste or direct typing, depending on your `clipboard.delivery_method` setting).

If you stop recording with the **auto-send key** (Alt by default), Enter is sent after the text — useful for chat apps and terminals.

```yaml
  - trigger: "my email"
    type: "user@example.com"
  - trigger: "my address"
    type: "123 Main Street, City, State 12345"
```

### Media keys

`hotkey` takes the media keys too, so any player follows (Spotify, YouTube in the browser, …): `media_play_pause`, `media_next`, `media_prev`, `media_stop`, `volume_up`, `volume_down`, `volume_mute`. Koli ships commands for them in English and Hebrew ("next song" / "השיר הבא", "louder" / "תגביר", …). Windows only for now.

### Shell commands

Run any shell command. The command runs asynchronously — it won't block the app.

```yaml
  - trigger: "open notepad"
    run: 'notepad.exe'
  - trigger: "open browser"
    run: 'start https://www.google.com'
  - trigger: "open downloads"
    run: 'explorer "%USERPROFILE%\Downloads"'
  - trigger: "lock screen"
    run: 'rundll32.exe user32.dll,LockWorkStation'
```

## Hotkey

The command hotkey and stop key are configured in your user settings (`user_settings.yaml`):
```yaml
hotkey:
  command_hotkey: "alt+win | macos: fn+command"
  stop_key: "ctrl | macos: fn"
  auto_send_key: "alt | macos: option"
```

- **stop_key** — stops recording and executes the matched command
- **auto_send_key** — same as stop key, but also sends Enter after `type` commands (ignored for `run` and `hotkey`)

Both keys are shared between transcription and command modes.

## Wake word

Say the wake word and a command, with no hotkey: **"Hey Koli, next song"** in one breath, or **"Hey Koli"**, a pause, then the command. After a command runs, Koli keeps listening for 5 seconds, so **"louder"** or **"next"** works without the wake word again.

Turn it on in **Commands → Wake word**. It listens on your dictation microphone while Koli runs, and never while you dictate, during a meeting, while Koli is paused or while the PC is locked. It needs voice commands on.

- **"Hey Koli"** works for any voice out of the box. Because anyone (or the TV) could say it, the 5 seconds after a command only run media commands, and Koli checks less often while it keeps hearing speech that isn't the wake word.
- **Your own word, in your voice:** press **Record your own word** and say it three times, in any language. Koli keeps what Whisper wrote each time and a voiceprint of you saying it, on this microphone. From then on only that word wakes Koli, only in your voice, and the 5 seconds after a command run any command. Record it again on another microphone to use it there. **Back to "Hey Koli"** removes it.
- **Test** says whether one try would have woken Koli, what it heard and, for your own word, your voice score next to the one it needs.

```yaml
wake_word:
  enabled: false
  phrases: []            # your word's spellings, filled by recording it
  only_my_voice: false   # set by recording your word
  sensitivity: medium    # low (strict), medium, high (wakes more easily)
  voice_match: 0.45      # how much your word must sound like you (0-1)
  command_timeout_seconds: 5
  follow_up_seconds: 5
```
