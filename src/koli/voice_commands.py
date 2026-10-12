# voice_commands.py
# Turns a spoken phrase into an action: send a hotkey, type canned text, or run a
# shell command, matched against user-defined triggers in `commands.yaml`
# (hot-reloaded on change). Security matters here — this is the one path that can
# execute arbitrary commands, so placeholders are expanded FIRST and the result
# is shlex-quoted and risk-checked before anything runs.
#
# A trigger may hold `{n}`, one spoken number 0-9 ("d {n}" hears "D 1" or "D
# one"), which fills `{n}` in the keys and text. Keys go to the app in front
# unless `send_to` names "windows" (no app gets them) or an app's .exe.
# `other_triggers` lists more ways to say a command, in any language.

import copy
import functools
import logging
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Callable, Optional

import pyperclip

from .defaults_merge import load_layered
from .utils import get_user_app_data_path, normalize_spoken
from .platform import keyboard, window_focus
from .ui.events import publish

DEFAULTS_FILE = "commands.defaults.yaml"

USER_FILE_HEADER = """\
# Koli — your voice commands
#
# The commands that ship with the app are applied underneath this file, so new
# ones reach you on every update. Here you only write what's yours:
#
#   commands:
#     # one of your own
#     - trigger: "open downloads"
#       run: "explorer %USERPROFILE%\\\\Downloads"
#
#     # change a shipped one, naming its trigger and the keys you want different
#     - trigger: "undo"
#       hotkey: "ctrl+shift+z"
#
#     # swap a shipped one's action: null clears the action it came with
#     - trigger: "maximize"
#       hotkey: null
#       type: "maximised"
#
#     # turn a shipped one off
#     - trigger: "show desktop"
#       disabled: true
#
#     # more ways to say it, in any language
#     - trigger: "play spotify"
#       other_triggers: ["תפעיל ספוטיפיי"]
#       run: "start spotify:"
#
#     # a spoken number: "D 1" presses Ctrl+Shift+1, "D two" Ctrl+Shift+2.
#     # send_to: windows keeps the keys away from the app in front; an app's
#     # .exe (chrome.exe) switches to it, presses them, and comes back.
#     - trigger: "d {n}"
#       hotkey: "ctrl+shift+{n}"
#       send_to: windows
#
# Full documentation: https://github.com/drajb/whisper-local/blob/master/docs/voice-commands.md

"""

RISKY_PATTERNS = re.compile(
    r'\b(rm\s+-r|del\s+/[sq]|format\s+\w:|shutdown|reg\s+delete|sudo|takeown|del\s+/f)\b'
    r'|>(?!\s*null)|\|\s*(out-file|tee)\b',
    flags=re.IGNORECASE,
)

# ── Spoken numbers: "d {n}" ──────────────────────────────────────────────────

NUMBER_PLACEHOLDER = '{n}'
NUMBER_WORDS = ('zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight', 'nine')
# The action values a spoken number is put into. `run` is left out on purpose:
# a number from speech has no business inside a shell command.
NUMBER_FIELDS = ('hotkey', 'type')


# "d {n}" -> a regex for the normalized text whose one group is the number.
# Spaces around the number are optional because Whisper writes "D1" as often
# as "D 1"; the lookarounds keep "and 1" and "D 12" from matching.
@functools.lru_cache(maxsize=256)
def _number_trigger_regex(trigger: str):
    number = r'\s*(\d|' + '|'.join(NUMBER_WORDS) + r')\s*'
    pieces = [re.escape(normalize_spoken(piece)) for piece in trigger.lower().split(NUMBER_PLACEHOLDER)]
    return re.compile(r'(?<!\w)' + number.join(pieces) + r'(?!\w)')


def _spoken_digit(word: str) -> str:
    return word if word.isdigit() else str(NUMBER_WORDS.index(word))


# ── Phrases: the trigger plus other_triggers ─────────────────────────────────
# `trigger` is the command's id (it layers shipped commands under yours), so
# more ways to say it, in any language, go in `other_triggers`.

def command_phrases(command: dict) -> list:
    phrases = [command.get('trigger') or ''] + list(command.get('other_triggers') or [])
    return [p for p in (str(x).strip() for x in phrases if isinstance(x, str)) if p]


# (phrase, command, is_main) for every phrase of every command, longest first,
# so "open notepad plus" wins over "open notepad" even across commands. A
# pattern-only command keeps one entry with an empty phrase.
def _phrases_longest_first(commands: list) -> list:
    pairs = []
    for command in commands:
        phrases = command_phrases(command) or ['']
        pairs.extend((phrase, command, index == 0) for index, phrase in enumerate(phrases))
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)
    return pairs


# Which of a command's phrases is in `text` (the longest), or '' when only
# its pattern matched. For "Test a phrase" on the Commands screen.
def phrase_heard(command: dict, text: str) -> str:
    normalized = normalize_spoken(text)
    for phrase in sorted(command_phrases(command), key=len, reverse=True):
        if NUMBER_PLACEHOLDER in phrase:
            if _number_trigger_regex(phrase).search(normalized):
                return phrase
        elif normalize_spoken(phrase) in normalized:
            return phrase
    return ''


# Keys that control what's playing. A command made only of these is safe to
# run from anything the microphone hears, lyrics included.
MEDIA_KEYS = frozenset({'media_play_pause', 'media_next', 'media_prev', 'media_stop',
                        'volume_up', 'volume_down', 'volume_mute'})


def is_media_command(command: dict) -> bool:
    keys = command.get('hotkey')
    if not isinstance(keys, str) or not keys.strip() or command.get('then'):
        return False
    return all(key.strip().lower() in MEDIA_KEYS for key in keys.split('+'))


# A copy of the command with `{n}` replaced in its keys and text, then-steps
# included. The loaded command keeps its placeholder for the next match.
def _with_number(command: dict, digit: str) -> dict:
    filled = copy.deepcopy(command)
    for action in [filled] + [s for s in filled.get('then') or [] if isinstance(s, dict)]:
        for field in NUMBER_FIELDS:
            if isinstance(action.get(field), str):
                action[field] = action[field].replace(NUMBER_PLACEHOLDER, digit)
    return filled


class VoiceCommandManager:
    def __init__(self, enabled=True, clipboard_manager=None, log_transcriptions=False, ollama_config_provider=None):
        self.enabled = enabled
        self.clipboard_manager = clipboard_manager
        self.log_transcriptions = log_transcriptions
        self.ollama_config_provider = ollama_config_provider
        self.logger = logging.getLogger(__name__)

        if not self.enabled:
            self.logger.info("Voice commands disabled by configuration")
            return

        self.commands_path = os.path.join(get_user_app_data_path(), "commands.yaml")
        self.commands = self._load_commands()
        self._commands_mtime = self._read_mtime()
        self.logger.info(f"Loaded {len(self.commands)} voice commands")

    # The shipped commands are layered under the user's file, so new ones added
    # in an update reach everybody. Shared by startup and the hot reload.
    def _load_commands(self) -> list:
        merged, _ = load_layered(
            DEFAULTS_FILE, Path(self.commands_path), "commands",
            USER_FILE_HEADER, id_key="trigger")
        return self._validate_commands(merged)

    def _validate_commands(self, raw_commands: list) -> list:
        valid = []
        for i, cmd in enumerate(raw_commands):
            trigger = cmd.get('trigger', '')
            has_match = bool(trigger or cmd.get('match_regex'))
            # A null action means "not this one": it's how a user override in
            # commands.yaml clears the action a shipped command came with
            # before naming a different one.
            action_count = sum(1 for key in ('run', 'hotkey', 'type', 'rephrase')
                               if cmd.get(key) is not None)

            if not has_match:
                self.logger.warning(f"Command {i}: missing trigger and match_regex, skipping")
                continue

            if action_count != 1:
                self.logger.warning(f"Command '{trigger}': needs exactly one of 'run', 'hotkey', 'type', or 'rephrase', skipping")
                continue

            others = cmd.get('other_triggers')
            if others is not None and not isinstance(others, list):
                self.logger.warning(f"Command '{trigger}': other_triggers must be a list of phrases, ignoring it")
                cmd = {k: v for k, v in cmd.items() if k != 'other_triggers'}

            valid.append(cmd)
        return valid

    # The command a phrase runs, or None. A `{n}` phrase returns a copy with
    # the spoken number filled in. `only` limits which commands may match (a
    # follow-up after "Hey Koli" runs media commands only).
    def match_command(self, text: str, only: Optional[Callable[[dict], bool]] = None) -> Optional[dict]:
        self._reload_if_changed()
        normalized = normalize_spoken(text)

        for phrase, command, is_main in _phrases_longest_first(self.commands):
            if only is not None and not only(command):
                continue

            # A pattern belongs to the command, not a phrase: checked once.
            regex_pattern = command.get('match_regex') if is_main else None
            if regex_pattern:
                try:
                    if re.search(regex_pattern, text, flags=re.IGNORECASE):
                        return command
                except re.error as e:
                    self.logger.warning(f"Invalid regex in command '{command.get('trigger', '')}': {e}")
                    continue

            if NUMBER_PLACEHOLDER in phrase:
                found = _number_trigger_regex(phrase).search(normalized)
                if found:
                    return _with_number(command, _spoken_digit(found.group(1)))
                continue

            if phrase and normalize_spoken(phrase) in normalized:
                return command

        return None

    # Expand ${clipboard} / ${selection} template vars. When shell_safe is True
    # (i.e. the result is headed for a shell `run:` command), each substituted
    # value is shlex.quote()'d so injected metacharacters can't break out of an
    # argument. shlex.quote is POSIX-correct; on Windows cmd.exe it isn't a
    # complete defence, which is why _execute_shell ALSO forces a confirmation
    # whenever a run: command contains template vars (see _execute_action).
    def _expand_template(self, value: str, shell_safe: bool = False) -> str:
        if not value or '${' not in value:
            return value
        try:
            clipboard_text = pyperclip.paste() or ''
        except Exception:
            clipboard_text = ''

        def _sub(text: str) -> str:
            return shlex.quote(text) if shell_safe else text

        if '${selection}' in value:
            import time
            original = clipboard_text
            try:
                keyboard.send_hotkey('ctrl', 'c')
                time.sleep(0.1)
                selection_text = pyperclip.paste() or ''
            except Exception:
                selection_text = ''
            if selection_text and selection_text != original:
                value = value.replace('${selection}', _sub(selection_text))
                try: pyperclip.copy(original)
                except Exception: pass
            else:
                value = value.replace('${selection}', _sub(''))

        if '${clipboard}' in value:
            value = value.replace('${clipboard}', _sub(clipboard_text))
        return value

    def _read_mtime(self) -> float:
        try:
            return os.path.getmtime(self.commands_path)
        except OSError:
            return 0.0

    def _reload_if_changed(self):
        if not getattr(self, 'commands_path', None):
            return
        current_mtime = self._read_mtime()
        if current_mtime <= self._commands_mtime:
            return

        self.logger.info(f"Detected change to {self.commands_path}, reloading commands")
        try:
            self.commands = self._load_commands()
            self._commands_mtime = current_mtime
            self.logger.info(f"Reloaded {len(self.commands)} voice commands")
            print(f"   🔄 Reloaded {len(self.commands)} voice commands from commands.yaml")
        except Exception as e:
            self.logger.error(f"Failed to reload commands: {e}")
            print(f"   ⚠ Failed to reload commands.yaml: {e}")

    # `confirm_runs`: the command came from a voice anyone could have spoken
    # ("Hey Koli" from a TV or a video), so its run steps always ask first,
    # and don't run at all where no dialog can ask.
    def execute_command(self, command: dict, use_auto_enter: bool = False, confirm_runs: bool = False):
        trigger = command.get('trigger', '')
        self._execute_action(command, trigger, use_auto_enter, confirm_runs=confirm_runs)
        for step in command.get('then', []) or []:
            if isinstance(step, dict):
                self._execute_action(step, trigger + " · then", use_auto_enter=False, confirm_runs=confirm_runs)

    def _execute_action(self, command: dict, trigger: str, use_auto_enter: bool = False, confirm_runs: bool = False):
        # Tested by value, not key presence, to match _validate_commands: a
        # user override that clears an action writes `hotkey: null`, and the
        # key is still there.
        if command.get('run') is not None:
            # If the command pulls in clipboard/selection content, that content is
            # untrusted — force a confirmation so the user always sees the final
            # command before it runs, regardless of the risky-pattern heuristic.
            had_untrusted = '${' in (command['run'] or '')
            expanded = self._expand_template(command['run'], shell_safe=True)
            self._execute_shell(expanded, trigger,
                                 require_confirm=command.get('confirm', None),
                                 force_confirm=had_untrusted or confirm_runs,
                                 fail_closed=confirm_runs)
        elif command.get('hotkey') is not None:
            self._send_hotkey(command['hotkey'], trigger, command.get('send_to'))
        elif command.get('type') is not None:
            self._deliver_text(self._expand_template(command['type']), trigger, use_auto_enter)
        elif command.get('rephrase') is not None:
            self._execute_rephrase(command['rephrase'], trigger)
        elif command.get('delay') is not None:
            import time
            try:
                seconds = float(command['delay'])
            except (TypeError, ValueError):
                seconds = 0.0
            seconds = max(0.0, min(60.0, seconds))
            time.sleep(seconds)

    def _execute_rephrase(self, instruction: str, trigger: str):
        import time
        from .text_postprocess import _ollama_polish

        if not self.ollama_config_provider:
            print("   ✗ Rephrase requires Ollama config; nothing wired")
            return

        try:
            original_clipboard = pyperclip.paste()
        except Exception:
            original_clipboard = ''

        keyboard.send_hotkey('ctrl', 'c')
        time.sleep(0.12)
        try:
            selection = pyperclip.paste()
        except Exception:
            selection = ''

        if not selection or selection == original_clipboard:
            print(f"   ✗ Nothing selected to rephrase ({trigger})")
            try: pyperclip.copy(original_clipboard)
            except Exception: pass
            return

        full_prompt = (
            f"{instruction}\n\n"
            "Output ONLY the rewritten text with no preamble, no quotes, no commentary.\n\n"
            f"Input:\n{selection}"
        )
        ollama_cfg = dict(self.ollama_config_provider() or {})
        ollama_cfg['enabled'] = True
        ollama_cfg['prompt'] = '{text}'

        polished = _ollama_polish(full_prompt, ollama_cfg)
        if not polished:
            print("   ✗ Rephrase failed — Ollama unreachable or returned nothing")
            try: pyperclip.copy(original_clipboard)
            except Exception: pass
            return

        try:
            pyperclip.copy(polished)
            time.sleep(0.05)
            keyboard.send_hotkey('ctrl', 'v')
            time.sleep(0.2)
        finally:
            try: pyperclip.copy(original_clipboard)
            except Exception: pass

        self.logger.info(f"Rephrased via '{trigger}': {len(selection)} → {len(polished)} chars")
        print(f"   ✓ Rephrased: {trigger}")

    # `fail_closed`: when no dialog can ask (not Windows), a command that must
    # be confirmed doesn't run, instead of running unasked.
    def _execute_shell(self, run_str: str, trigger: str, require_confirm: Optional[bool] = None,
                       force_confirm: bool = False, fail_closed: bool = False):
        is_risky = bool(RISKY_PATTERNS.search(run_str))
        # Explicit confirm: setting wins; otherwise confirm if risky OR if the
        # command was built from untrusted clipboard/selection content.
        if require_confirm is not None:
            needs_confirm = require_confirm or force_confirm
        else:
            needs_confirm = is_risky or force_confirm
        if needs_confirm and not self._confirm_risky_command(trigger, run_str, fail_closed=fail_closed):
            self.logger.info(f"User declined risky command '{trigger}'")
            print(f"   ✗ Cancelled: {trigger}")
            return

        try:
            subprocess.Popen(run_str, shell=True)
            self.logger.info(f"Executed command '{trigger}': {run_str}")
            print(f"   Executed: {trigger}")
        except Exception as e:
            self.logger.error(f"Failed to execute command '{trigger}': {e}")
            print(f"   Failed to execute command: {e}")

    def _confirm_risky_command(self, trigger: str, run_str: str, fail_closed: bool = False) -> bool:
        if os.name == 'nt':
            try:
                import ctypes
                MB_YESNO = 0x4
                MB_ICONWARNING = 0x30
                MB_DEFBUTTON2 = 0x100
                IDYES = 6
                result = ctypes.windll.user32.MessageBoxW(
                    0,
                    f"Voice command '{trigger}' would run:\n\n{run_str}\n\nProceed?",
                    "Koli — Confirm command",
                    MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2,
                )
                return result == IDYES
            except Exception as e:
                self.logger.error(f"Could not show confirm dialog: {e}")
                return False
        if fail_closed:
            self.logger.warning(f"Command '{trigger}' needs confirming and no dialog can ask here; not run")
            return False
        return True

    # Press the keys in the app in front, or with `send_to` inside another
    # window: "windows" (an invisible Koli window, so only Windows and other
    # programs' global shortcuts react) or an app's .exe. Focus returns to
    # where the user was. A closed target app presses nothing and says so.
    def _send_hotkey(self, hotkey_str: str, trigger: str, send_to: Optional[str] = None):
        keys = [k.strip() for k in hotkey_str.lower().split('+')]
        try:
            if send_to:
                window_focus.run_focused(send_to, lambda: keyboard.send_hotkey(*keys))
            else:
                keyboard.send_hotkey(*keys)
            where = f" to {send_to}" if send_to else ''
            self.logger.info(f"Sent hotkey '{trigger}'{where}: {hotkey_str}")
            print(f"   ✓ Sent hotkey{where}: {trigger} [{hotkey_str}]")
        except LookupError as e:
            self.logger.warning(f"Hotkey '{trigger}' not sent: {e}")
            publish('notify', {'kind': 'command', 'message': f"“{trigger}” didn't run: {e}"})
            print(f"   ✗ Not sent: {e}")
        except Exception as e:
            self.logger.error(f"Failed to send hotkey '{trigger}': {e}")
            print(f"   Failed to send hotkey: {e}")

    def _deliver_text(self, text: str, trigger: str, use_auto_enter: bool = False):
        try:
            if self.clipboard_manager:
                self.clipboard_manager.deliver_transcription(text, use_auto_enter)
                if self.log_transcriptions:
                    self.logger.info(f"Delivered text '{trigger}': {text}")
                else:
                    self.logger.info(f"Delivered text for '{trigger}'")
                print(f"   ✓ Typed: {text}")
            else:
                self.logger.error("No clipboard manager available for type command")
                print("   Failed: clipboard manager not available")
        except Exception as e:
            self.logger.error(f"Failed to deliver text '{trigger}': {e}")
            print(f"   Failed to deliver text: {e}")
