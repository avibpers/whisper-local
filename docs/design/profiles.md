# Profiles: named settings on top of your global ones

*2026-11-05, the owner's design. `profiles.py`, the layer in `config_manager.py`,
the Profiles card in Apps (`ui/web/js/screens/apps.js`).*

## What it is

A profile holds only the settings it changes. Everything else is "use global", so
your global settings stay the one place for what you want everywhere, and a profile
never overwrites them. Translation is just one setting in a profile
(`ai.translate`), not a feature of its own.

## Which one applies

For each dictation, the first that applies:

| | Set in | Example |
|---|---|---|
| 1. The key you hold | The profile's own key | Hold Ctrl+Win+Y to dictate with Translate |
| 2. The app's profile | The app's rule, "Profile in this app" | The main key translates in this app |
| 3. The default profile | "Make default" in Apps, the tray, the app window's selector | Koli translates everywhere |
| Nothing set | | Your global settings |

Only one profile applies at a time, on top of the global settings; they don't stack.
An app rule's own settings still apply over the profile. Holding a key lasts for one
dictation. To write what you said in an app with a profile, give the Dictation
profile, which sets nothing, a key of its own.

## How it works

`ConfigManager.config` is the global settings with the layer on top. The layer is
in memory only, is never saved, and every change you make goes to the global
settings; the Settings screen reads and edits those. `ProfileLayer.use()` puts a
profile on at the start of a recording (`_apply_recording_context`) and the default
back when the dictation ends. Settings that are read when used just follow. For those
a part of Koli holds in memory, the appliers set the new value and set it back,
without saving anything. A global change to a setting the active profile holds
waits until the profile goes.

## What a profile can hold

`schema.PROFILE_PATHS`: language, your words, delivery, writing, translation and
Polish. Not what rebuilds a component, like the speech model or the microphone:
loading a model takes over 10 seconds, so a hold-to-talk key would miss your first
words. Not what starts a recording either, like auto-dictation or the recording
mode, since a profile is chosen once the recording starts. Anything else in a
profile's file is ignored.

## Why not the old way

Activating a profile used to write its values into your global settings. The
Dictation and Notes profiles replaced the owner's model with `base` that way, and
their Hebrew came out as nonsense (2026-11-05).
