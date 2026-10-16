# clipboard_manager.py
# Delivers transcribed text to the active app: either pastes (clipboard + Ctrl/Cmd+V,
# restoring the user's prior clipboard) or types it directly via SendInput, with an
# optional auto-Enter for chat apps, sent at once or after a short wait you can
# cancel (delayed_send.py). Cross-platform through the platform.keyboard layer.

import logging
import time
from typing import Optional

import pyperclip

from .delayed_send import count_words, wait_seconds
from .platform import keyboard
from .utils import parse_hotkey


def _foreground_exe() -> str:
    try:
        from .platform import foreground
        return (foreground.get_foreground_app() or {}).get('exe') or ''
    except Exception:
        return ''

class ClipboardManager:
    def __init__(self, auto_paste, delivery_method, paste_hotkey,
                 paste_pre_paste_delay, paste_preserve_clipboard,
                 paste_clipboard_restore_delay,
                 type_also_copy_to_clipboard, type_auto_enter_delay,
                 type_auto_enter_delay_per_100_chars,
                 macos_key_simulation_delay, auto_send_delay=0):
        self.logger = logging.getLogger(__name__)
        # Seconds to wait before an auto-send presses Enter (0 = at once), and
        # the countdown that does the waiting (a DelayedSend, set by main.py).
        self.auto_send_delay = auto_send_delay
        self.delayed_send = None
        self.auto_paste = auto_paste
        self.delivery_method = keyboard.validate_delivery_method(delivery_method)
        self.paste_hotkey = paste_hotkey
        self.paste_keys = parse_hotkey(paste_hotkey)
        self.paste_pre_paste_delay = paste_pre_paste_delay
        self.paste_preserve_clipboard = paste_preserve_clipboard
        self.paste_clipboard_restore_delay = paste_clipboard_restore_delay
        self.type_also_copy_to_clipboard = type_also_copy_to_clipboard
        self.type_auto_enter_delay = type_auto_enter_delay
        self.type_auto_enter_delay_per_100_chars = type_auto_enter_delay_per_100_chars
        keyboard.set_delay(macos_key_simulation_delay)
        if self.delivery_method == "paste":
            self._test_clipboard_access()
        self._print_status()

    def _test_clipboard_access(self):
        try:
            pyperclip.paste()
            self.logger.info("Clipboard access test successful")

        except Exception as e:
            self.logger.error(f"Clipboard access test failed: {e}")
            raise

    def _print_status(self):
        hotkey_display = self.paste_hotkey.upper()
        if self.auto_paste:
            if self.delivery_method == "type":
                print("   ✓ Auto-paste is ENABLED using direct text injection")
            else:
                print(f"   ✓ Auto-paste is ENABLED using clipboard paste ({hotkey_display})")
        else:
            print(f"   ✗ Auto-paste is DISABLED - paste manually with {hotkey_display}")

    # May the app put text on the clipboard WITHOUT the user asking?
    #
    # `type_also_copy_to_clipboard: false` with delivery_method "type" is an
    # explicit request for clipboard-free dictation, usually because a
    # clipboard manager is logging history or the machine is shared. The
    # delivery path already respected it; the recovery paths did not
    # (issue #12). An explicit Copy button is always still allowed - this
    # governs automatic copies only.
    @property
    def silent_copy_allowed(self) -> bool:
        if self.delivery_method == "type":
            return bool(self.type_also_copy_to_clipboard)
        return True

    def copy_text(self, text: str) -> bool:
        if not text:
            return False

        try:
            self.logger.info(f"Copying text to clipboard ({len(text)} chars)")
            pyperclip.copy(text)
            return True

        except Exception as e:
            self.logger.error(f"Failed to copy text to clipboard: {e}")
            return False

    def get_clipboard_content(self) -> Optional[str]:
        try:
            clipboard_content = pyperclip.paste()

            if clipboard_content:
                return clipboard_content
            else:
                return None

        except Exception as e:
            self.logger.error(f"Failed to paste text from clipboard: {e}")
            return None

    def copy_with_notification(self, text: str) -> bool:
        if not text:
            return False

        success = self.copy_text(text)

        if success:
            print("   ✓ Copied to clipboard")
            print("   ✓ You can now paste with Ctrl+V in any application!")

        return success

    def clear_clipboard(self) -> bool:
        try:
            pyperclip.copy("")
            return True

        except Exception as e:
            self.logger.error(f"Failed to clear clipboard: {e}")
            return False

    def _type_delivery(self, text: str) -> bool:
        try:
            keyboard.type_text(text)
            if self.type_also_copy_to_clipboard:
                pyperclip.copy(text)
            print("   ✓ Auto-pasted via text injection")
            return True
        except Exception as e:
            self.logger.error(f"Failed to inject text: {e}")
            return False

    def _clipboard_paste(self, text: str) -> bool:
        try:
            original_content = None
            if self.paste_preserve_clipboard:
                original_content = pyperclip.paste()

            if not self.copy_text(text):
                return False

            time.sleep(self.paste_pre_paste_delay)
            keyboard.send_hotkey(*self.paste_keys)

            print("   ✓ Auto-pasted via key simulation")
            # Which app got the keys: "pasted" only means they were sent.
            self.logger.info(f"Pasted at the cursor (in {_foreground_exe() or 'an unknown app'})")

            if original_content is not None:
                time.sleep(self.paste_clipboard_restore_delay)
                pyperclip.copy(original_content)

            return True

        except Exception as e:
            self.logger.error(f"Failed to simulate paste keypress: {e}")
            return False

    def execute_delivery(self, text: str) -> bool:
        if self.delivery_method == "type":
            return self._type_delivery(text)
        else:
            return self._clipboard_paste(text)

    def send_enter_key(self) -> bool:
        try:
            self.logger.info("Sending ENTER key to active application")
            keyboard.send_key('enter')
            print("   ✓ Text submitted with ENTER!")

            return True

        except Exception as e:
            self.logger.error(f"Failed to send ENTER key: {e}")
            return False

    # The Enter of an auto-send: pressed now, or handed to the countdown when a
    # wait is set and this send may wait (an app rule's automatic auto-send;
    # the send key always sends at once). `delay` is an app rule's own wait,
    # which replaces the main one; an "Auto" wait follows `text`'s length.
    # True when Enter was pressed or is on its way.
    def auto_send(self, allow_delay: bool = False, delay=None, text: str = '') -> bool:
        if delay is None:
            delay = getattr(self, 'auto_send_delay', 0)
        delay = wait_seconds(delay, count_words(text))
        if allow_delay and delay > 0 and getattr(self, 'delayed_send', None) is not None:
            self.delayed_send.schedule(delay)
            return True
        return self.send_enter_key()

    def send_is_pending(self) -> bool:
        ds = getattr(self, 'delayed_send', None)
        return bool(ds is not None and ds.is_pending())

    def deliver_transcription(self,
                              transcribed_text: str,
                              use_auto_enter: bool = False,
                              allow_delay: bool = False,
                              send_delay=None) -> bool:

        try:
            if self.auto_paste:
                success = self.execute_delivery(transcribed_text)
                if success and use_auto_enter:
                    if self.delivery_method == "type":
                        delay = self.type_auto_enter_delay + len(transcribed_text) / 100 * self.type_auto_enter_delay_per_100_chars
                        if delay > 0:
                            time.sleep(delay)
                    success = self.auto_send(allow_delay, send_delay, text=transcribed_text)
            else:
                print("📋 Copying to clipboard...")
                success = self.copy_with_notification(transcribed_text)

            return success

        except Exception as e:
            self.logger.error(f"Delivery workflow failed: {e}")
            return False

    def update_auto_paste(self, enabled: bool):
        self.auto_paste = enabled
        self._print_status()
