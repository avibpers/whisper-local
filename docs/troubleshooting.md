# Troubleshooting

Most issues are caught by `koli --doctor` or `koli --selftest`. Run one of those first.

If you need to file a bug, run:

```bash
koli --bundle-logs
```

This creates a zip with redacted logs, `--doctor` output, and recent crashes. Attach it to your issue.

## Quick reference

| Symptom | Most likely cause | Fix |
|---|---|---|
| "Windows protected your PC" (SmartScreen) when running the `.exe` | The app is open-source but not code-signed (certificates cost money) | Click **More info → Run anyway**. Verify the download with the `.sha256` file from the release if you want certainty |
| First `.exe` launch sits for minutes with no window | pyapp is bootstrapping a private Python + installing the app (one-time) | Wait it out — subsequent launches are instant. Then the Whisper model download (~141 MB) follows |
| Recording starts but no text appears | Default Whisper task changed accidentally | Settings → General → Language = `auto` (or your language); Profile = `Dictation` |
| First word is cut off | (Should be fixed by pre-roll) Mic was slow to wake | Check `audio.host` is set to `WASAPI` on Windows |
| Hotkey does nothing | Another app holds the same combination | Change `hotkey.recording_hotkey` in Settings; restart |
| Text appears as ALL CAPS | Caps Lock was on when hotkey pressed | Push-to-talk releases reset modifiers — try toggling Caps Lock off |
| "Mic captured pure silence" in `--selftest` | OS denied mic permission to your shell/terminal | Windows: Settings → Privacy & Security → Microphone. macOS: System Settings → Privacy & Security → Microphone |
| Tray icon missing | Windows hides tray icons by default | Click the `^` in the taskbar, drag the Koli icon out |
| App seems frozen on launch | Whisper model is downloading (first run) | Wait — `tiny` is ~75 MB, `large` is ~2.9 GB. Watch `%APPDATA%\koli\app.log` |
| "Could not register hotkeys" | Another instance is already running, or another app owns the combo | `koli --quit` then relaunch. Or change the hotkey |
| GPU not used despite `device: cuda` | CTranslate2 CUDA runtime not installed | Run `koli --setup` and accept the GPU install prompt |
| Transcription accuracy is poor | Tiny model + noisy environment | Settings → General → Model = `base` or `small`. Settings → Audio → Noise suppression = on (`pip install 'koli[noise]'`) |
| `Ollama unreachable` in tray | Ollama service not running locally | `ollama serve` in a terminal, or disable in Settings → Post-process |
| Text appears twice in chat apps | `auto_send_key` pressed by accident | The Esc cancel + retry; consider switching off auto-send for that app via `app_rules.yaml` |
| Audio device unplugged mid-recording | USB mic disconnect | App should auto-recover to default input. If not, restart and tell the maintainer |
| Settings change didn't take effect | A background change (model, microphone) is still loading | Watch the task chip at the top of the app window; dictation uses the old setting until it finishes. If the chip shows an error, the setting was put back |
| The app window doesn't open, and the old Tk windows appear | The Microsoft Edge WebView2 runtime is missing, or `pywebview` isn't installed | Install WebView2 from [Microsoft](https://developer.microsoft.com/microsoft-edge/webview2/) (Windows 11 includes it). From source: `pip install pywebview`. The log says why the app window was unavailable |
| The app window opens blank | WebView2 is broken or blocked by policy | Run `koli --legacy-ui` for now, and repair the WebView2 runtime from Apps & Features |
| A notice on start says dictation runs on the CPU for now | The GPU's CUDA libraries are missing | Click **Install GPU support** in the notice, or use Diagnostics → Health check |
| Launching again doesn't start a new copy | That is on purpose: it opens the running app's window | Use `koli --replace` to restart it instead |
| `pyperclip.PyperclipException` on macOS | Missing `pbcopy`/`pbpaste` permissions | Open System Settings → Privacy → Accessibility, add Terminal |
| Stale lock prevents launch | Previous instance crashed without cleanup | `koli --quit` clears the lock. As a last resort delete `%APPDATA%\koli\Koli.pid` |

## When all else fails

1. **Reset the app to defaults:** delete `%APPDATA%\koli\user_settings.yaml` (Windows) or `~/.koli/user_settings.yaml` (macOS). The defaults will be regenerated on next launch. Your hotwords/commands/transforms are kept.
2. **Reinstall:** `pip uninstall koli && pip install koli`
3. **Wipe everything:** delete the entire `%APPDATA%\koli` directory — this resets settings, dictionary, stats, transcript history.
4. **File a bug:** run `koli --bundle-logs` and attach the resulting zip to a new issue via [bug report template](https://github.com/drajb/whisper-local/issues/new?template=bug_report.yml).
