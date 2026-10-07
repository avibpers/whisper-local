Local faster-whisper speech-to-text app with global hotkeys for Windows 10+ and macOS

Open-source project maintained as `drajb/whisper-local` by Rohit Burani. Internal Python module name remains `whisper_key`; the config directory remains `%APPDATA%\whisperkey` (`~/.whisperkey` on macOS).

- Start here: `state_manager.py` coordinates all components workflow
- The app window and floating bar: `ui/host.py` runs them, `ui/bridge.py` is everything the pages can call, and `ui/web/` holds the pages. See [App window and floating bar](#app-window-and-floating-bar).

## Component Architecture

| Component | File | Primary Responsibility | Key Technologies |
|-----------|------|----------------------|------------------|
| **Entry Point** | `main.py` | Component initialization, signal handling | logging, threading |
| **State Coordination** | `state_manager.py` | Component orchestration & workflow | threading, logging |
| **Audio Capture** | `audio_recorder.py` | Microphone recording & audio buffering | sounddevice, numpy |
| **Audio Feedback** | `audio_feedback.py` | Recording event sound notifications | playsound3 |
| **Speech Recognition** | `whisper_engine.py` | Audio transcription using AI | faster-whisper |
| **Model Management** | `model_registry.py` | Whisper model registry & cache detection | faster-whisper |
| **Voice Activity Detection** | `voice_activity_detection.py` | Continuous VAD monitoring & silence detection | ten-vad, threading |
| **Clipboard Operations** | `clipboard_manager.py` | Text copying & auto-paste functionality | pyperclip, ctypes SendInput (Win), Quartz (Mac) |
| **Hotkey Detection** | `hotkey_listener.py` | Global hotkey monitoring | global-hotkeys (Win), NSEvent (Mac) |
| **Configuration** | `config_manager.py` | YAML settings management & validation | ruamel.yaml |
| **System Integration** | `system_tray.py` | Tray icon; a short menu (Open, Pause, Profile, Recent, Quit) with the app window, the full menu with `--legacy-ui` | pystray, Pillow |
| **Instance Management** | `instance_manager.py` | Single instance enforcement | win32event (Win), fcntl (Mac) |
| **Voice Commands** | `voice_commands.py` | Trigger matching & command execution | subprocess |
| **Platform Abstraction** | `platform/` | OS-specific implementations | pywin32 (Win), pyobjc (Mac) |
| **GPU Onboarding** | `onboarding.py` | GPU setup prompt & package installation | subprocess |
| **Hardware Detection** | `hardware_detection.py` | Platform GPU detection wrapper | - |
| **Terminal UI** | `terminal_ui.py` | Interactive terminal prompts | - |
| **Utilities** | `utils.py` | Common utility functions | - |
| **Diagnostics** | `doctor.py` | `--doctor` health checks across all subsystems | - |
| **Self-Test** | `selftest.py` | `--selftest` automated mic/model/transcribe/clipboard check | sounddevice |
| **Log Bundler** | `bundle_logs.py` | `--bundle-logs` redacted diagnostic zip for bug reports | zipfile, re |
| **Local API Server** | `local_server.py` | `--serve` OpenAI-compatible Whisper HTTP endpoint | http.server |
| **Settings GUI (legacy)** | `settings_ui.py` | Tkinter settings editor, used with `--legacy-ui` only | tkinter |
| **Transcript History** | `transcript_log.py` (+ `history_window.py`, legacy) | Journal of past transcriptions; shown in the app's History screen | json |
| **Hotkey Cheat Sheet (legacy)** | `cheat_sheet.py` | Tk window listing the hotkeys; the app shows them in Diagnostics | tkinter |
| **Window Launcher** | `window_launcher.py` | Opens Tk windows in a child process where Tk needs the main thread (macOS) | subprocess, json |
| **First-Run Welcome (legacy)** | `first_run.py` | Tk welcome window; the app window has its own welcome steps | tkinter |
| **Terminal Title** | `terminal_title.py` | Animated terminal tab title reflecting app state | - |
| **Level Overlay (legacy)** | `level_overlay.py` | Tk level pill, replaced by the floating bar (`ui/floating_bar.py`) | tkinter |
| **Fallback Window (legacy)** | `fallback_window.py` | Tk capture window when no text field is focused; the app shows the text on its Live screen | tkinter |
| **Transforms** | `transforms.py` | Wispr-style AI text transforms via Ollama | ruamel.yaml |
| **Dictionary** | `dictionary.py` | Hotword add/remove/list + add-word dialog | tkinter, ruamel.yaml |
| **Profiles** | `profiles.py` | Dictation/Chat/Code/Notes/Translate presets | ruamel.yaml |
| **Per-App Rules** | `app_rules.py` | Foreground-app-specific behaviour overrides | ruamel.yaml |
| **Layered Defaults** | `defaults_merge.py` | Loads shipped defaults under the user's app_rules/commands/transforms/profiles, and migrates pre-0.21 full copies | ruamel.yaml |
| **Text Post-Process** | `text_postprocess.py` | Inline formatting, smart formatting, voice editing, corrections + optional Ollama polish | urllib |
| **Dictation Cleanup** | `dictation_cleanup.py` | Backtrack ("actually 3"), stutter removal, spoken lists, lowercase | re |
| **Snippets** | `snippets.py` | Spoken shortcuts expanded inline during dictation | re |
| **Styles** | `styles.py` | Named writing styles (formal/casual/very casual/verbatim), global or per app | - |
| **Hotkey Gestures** | `hotkey_gestures.py` | Double-tap-to-lock state machine for push-to-talk | threading |
| **Whisper Mode** | `audio_gain.py` | Boosts quiet recordings before transcription | numpy |
| **Corrections** | `corrections.py` | Persist post-transcription replacements (history "Fix this everywhere") | ruamel.yaml |
| **System Audio** | `system_audio.py` | `--transcribe-system` loopback capture (experimental, opt-in) | soundcard |
| **Streaming** | `streaming_manager.py` + `streaming_recognizer.py` | Real-time partial transcription (experimental) | sherpa-onnx |
| **Noise Suppression** | `noise_suppression.py` | Optional spectral-gating noise reduction | noisereduce |
| **Stats** | `stats.py` | Usage stats, streaks, daily summary | json |
| **Audit Log** | `audit_log.py` | Optional append-only delivery audit trail | - |
| **Update Check** | `update_check.py` | Opt-in daily GitHub release check | urllib |
| **Uninstall** | `uninstall.py` | `--uninstall` removes settings/data/autostart, models opt-in | shutil |
| **Settings I/O** | `settings_io.py` | `--export-settings` / `--import-settings` | shutil |
| **Offline Models** | `model_transfer.py` | `--export-model` / `--import-model` for HF-blocked networks | shutil, ruamel.yaml |
| **Vocab Import** | `vocab_import.py` | `--import-vocab` hotword mining from a folder | - |
| **Setup Wizard** | `setup_wizard.py` | `--setup` interactive first-time configuration | - |
| **Autostart** | `autostart.py` | Opt-in launch-on-login (Windows Run key / macOS LaunchAgent) | winreg |
| **Console Welcome (legacy)** | `onboarding_tutorial.py` | First-launch console banner, skipped when the app window runs | - |
| **Whisper.cpp Backend** | `whisper_engine_cpp.py` | Opt-in `whisper_cpp` backend mirroring WhisperEngine's API | pywhispercpp |
| **Foreground App** | `platform/*/foreground.py` | Detects the active window/app for per-app rules | pywin32 / pyobjc |
| **Console Control** | `platform/windows/console.py` | Show/hide/own the console window (pyapp builds) | ctypes |

## App window and floating bar

The default UI: a floating bar while the app is closed, and an app
window with Simple and Advanced modes. Settings apply the moment they change.
`--legacy-ui` brings back the Tk windows for one release.

| Component | File | Primary Responsibility | Key Technologies |
|-----------|------|----------------------|------------------|
| **UI Host** | `ui/host.py` | Creates both windows, pushes events to the pages, owns the main thread while the UI runs | pywebview (WebView2) |
| **Bridge** | `ui/bridge.py` | Every call a page can make; answers `{ok, data}` or `{ok: false, error}` | - |
| **Event Bus** | `ui/events.py` | Publish/subscribe for state, levels, pipeline stages, tasks, settings, logs | threading |
| **Log Stream** | `ui/log_stream.py` | Ring buffer of log records for the Live console | logging |
| **Background Tasks** | `ui/tasks.py` | Model downloads, engine and microphone swaps, GPU install, with progress | threading |
| **Floating Bar** | `ui/floating_bar.py` | The level-overlay interface, drawn by `web/bar.html` | - |
| **Instance Channel** | `ui/instance_ipc.py` | A second launch opens the running window; `--quit` asks it to quit | multiprocessing.connection |
| **Window Helpers** | `ui/win32.py` | No-focus bar, rounded corners, dark title bar, DPI, work area (Windows) | ctypes |
| **Settings Schema** | `settings/schema.py` | Label, help, control, section and apply mode for every setting | - |
| **Settings Service** | `settings/service.py` | Validate, save, apply, undo; watches the settings file for hand edits | threading |
| **Appliers** | `settings/appliers.py` | Applies each change live, on the next recording, or by a background swap | - |
| **Simple Mode** | `settings/simple_mode.py` | Plain-language answers to and from settings | - |
| **Services** | `services/*.py` | Models, history, insights, vocabulary, commands, rules and profiles, diagnostics, system | - |
| **Web pages** | `ui/web/` | `index.html` (app window), `bar.html` (floating bar); Preact + htm, no build step | Preact, htm |

Run `index.html?mock=1` from a static server to work on the pages in a
browser; `tools/make_mock_data.py` refreshes the mock's data from the defaults.

## Project Structure

```
whisper-local/
├── whisper-local.py                   # Development wrapper script
├── pyproject.toml                     # Dependencies & PyPI config
├── CLAUDE.md                          # Claude AI project instructions
├── README.md                          # Project documentation
├── CHANGELOG.md                       # Version history and changes
│
├── src/
│   └── whisper_key/                   # Python package
│       ├── __init__.py                # Package initialization
│       ├── main.py                    # Main application entry point
│       ├── config.defaults.yaml       # Default configuration template
│       ├── commands.defaults.yaml     # Default voice commands template
│       ├── assets/                    # Application assets
│       │   ├── sounds/                # Audio feedback sounds
│       ├── platform/                  # Platform abstraction layer
│       │   ├── __init__.py            # Platform detection & import routing
│       │   └── {macos,windows}/       # Platform-specific implementations
│       │       ├── assets/            # Platform-specific assets
│       │       ├── app.py             # Thread requirements, getch()
│       │       ├── hotkeys.py         # Hotkey detection
│       │       ├── icons.py           # Tray icons
│       │       ├── instance_lock.py   # Instance control
│       │       ├── keyboard.py        # Key simulation
│       │       ├── paths.py           # Path management
│       │       ├── gpu.py              # GPU detection (Windows)
│       │       └── permissions.py     # Permission management
│       ├── audio_feedback.py          # Audio feedback for recording events
│       ├── audio_recorder.py          # Sounddevice audio capture
│       ├── clipboard_manager.py       # Clipboard & auto-paste operations
│       ├── config_manager.py          # YAML configuration management
│       ├── hotkey_listener.py         # Global hotkey detection
│       ├── instance_manager.py        # Single instance enforcement
│       ├── model_registry.py          # Whisper model registry & caching
│       ├── onboarding.py              # GPU setup prompt & installation
│       ├── state_manager.py           # Component coordination & workflow
│       ├── system_tray.py             # System tray icon & menu
│       ├── utils.py                   # Common utility functions
│       ├── voice_activity_detection.py # Voice activity detection
│       ├── voice_commands.py          # Voice command matching & execution
│       ├── hardware_detection.py       # Platform GPU detection wrapper
│       ├── terminal_ui.py             # Interactive terminal prompts
│       ├── whisper_engine.py          # Faster-whisper transcription
│
├── docs/                              # Project documentation
│   ├── project-index.md
│   ├── voice-commands.md              # Voice commands user guide
│   ├── design/                        # Design docs
│   ├── plans/                         # Planning docs
│   ├── research/                      # Research docs
│   ├── roadmap/                       # Feature roadmap & user stories
│   │   ├── roadmap.md                 # Active feature roadmap
│   │   └── completed.md               # Completed user stories
│   └── ...
│
├── .temp/                             # Temporary working files (gitignored)
└── pyapp-build/                       # pyapp build script and config
```

---

*Last Updated: 2026-06-11 | Project Status: Active Development*