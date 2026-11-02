# tests/test_clipboard_paste.py
# Pasting must give the user's clipboard back whole: every format (an image
# survives), never an empty restore that wipes it, and never over something
# the user copied during the wait. Logic runs on a fake clipboard; the real
# Windows snapshot/restore gets one round trip around the user's clipboard.

import ctypes
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from koli import clipboard_manager  # noqa: E402


# ── A clipboard in memory ────────────────────────────────────────────────────

class FakeClipboard:
    """Holds a snapshot-like list of items and a sequence number."""

    class Snapshot:
        def __init__(self, items):
            self.items = list(items)

        @property
        def empty(self):
            return not self.items

    def __init__(self, items):
        self.items = list(items)
        self.seq = 1
        self.restored = []

    def put(self, items):
        self.items = list(items)
        self.seq += 1

    def sequence(self):
        return self.seq

    def snapshot(self):
        return self.Snapshot(self.items)

    def restore(self, snap):
        self.restored.append(snap)
        self.put(snap.items)
        return True


def make_manager():
    with mock.patch.object(clipboard_manager.ClipboardManager, '_test_clipboard_access'), \
         mock.patch.object(clipboard_manager.ClipboardManager, '_print_status'), \
         mock.patch.object(clipboard_manager.keyboard, 'set_delay'):
        return clipboard_manager.ClipboardManager(
            auto_paste=True, delivery_method='paste', paste_hotkey='ctrl+v',
            paste_pre_paste_delay=0, paste_preserve_clipboard=True,
            paste_clipboard_restore_delay=0, type_also_copy_to_clipboard=True,
            type_auto_enter_delay=0, type_auto_enter_delay_per_100_chars=0,
            macos_key_simulation_delay=0)


class PasteRestoreTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeClipboard([(8, b'DIB image bytes')])   # a screenshot, no text
        self.manager = make_manager()
        patches = [
            mock.patch.object(clipboard_manager, 'clipboard', self.fake),
            mock.patch.object(clipboard_manager.keyboard, 'send_hotkey'),
            mock.patch.object(clipboard_manager, '_foreground_exe', return_value='notepad.exe'),
            mock.patch.object(self.manager, 'copy_text',
                              side_effect=lambda text: self.fake.put([(13, text)]) or True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_an_image_on_the_clipboard_survives_a_paste(self):
        self.assertTrue(self.manager._clipboard_paste('hello'))
        self.assertEqual(self.fake.items, [(8, b'DIB image bytes')])

    def test_an_empty_clipboard_is_left_empty(self):
        self.fake.items = []
        self.manager._clipboard_paste('hello')
        self.assertEqual(self.fake.items, [])

    def test_a_copy_made_during_the_wait_is_kept(self):
        def user_copies(*_keys):
            self.fake.put([(13, 'copied by the user')])
        clipboard_manager.keyboard.send_hotkey.side_effect = user_copies
        self.manager._clipboard_paste('hello')
        self.assertEqual(self.fake.items, [(13, 'copied by the user')])
        self.assertEqual(self.fake.restored, [])

    def test_an_unreadable_clipboard_is_not_restored(self):
        self.fake.snapshot = lambda: None
        self.manager._clipboard_paste('hello')
        self.assertEqual(self.fake.restored, [])
        self.assertEqual(self.fake.items, [(13, 'hello')])

    def test_reading_the_selection_gives_the_clipboard_back(self):
        def ctrl_c(*_keys):
            self.fake.put([(13, 'selected words')])
        clipboard_manager.keyboard.send_hotkey.side_effect = ctrl_c
        with mock.patch.object(clipboard_manager.pyperclip, 'paste', return_value='selected words'):
            self.assertEqual(self.manager.read_selection(0), 'selected words')
        self.assertEqual(self.fake.items, [(8, b'DIB image bytes')])

    def test_nothing_selected_reads_as_empty(self):
        # Ctrl+C with no selection leaves the clipboard untouched.
        self.assertEqual(self.manager.read_selection(0), '')
        self.assertEqual(self.fake.restored, [])
        self.assertEqual(self.fake.items, [(8, b'DIB image bytes')])

    def test_without_preserve_nothing_is_saved(self):
        self.manager.paste_preserve_clipboard = False
        self.manager._clipboard_paste('hello')
        self.assertEqual(self.fake.items, [(13, 'hello')])


class SendKeyTests(unittest.TestCase):
    def setUp(self):
        self.manager = make_manager()
        self.manager.execute_delivery = mock.Mock(return_value=True)
        patches = [mock.patch.object(clipboard_manager.keyboard, 'send_key'),
                   mock.patch.object(clipboard_manager.keyboard, 'send_hotkey')]
        self.send_key, self.send_hotkey = (p.start() for p in patches)
        for p in patches:
            self.addCleanup(p.stop)

    def test_the_main_setting_and_a_rules_keys(self):
        self.manager.deliver_transcription('hi', use_auto_enter=True)
        self.send_key.assert_called_once_with('enter')
        self.manager.send_key = 'ctrl+enter'
        self.manager.deliver_transcription('hi', use_auto_enter=True)
        self.send_hotkey.assert_called_with('ctrl', 'enter')
        self.manager.deliver_transcription('hi', use_auto_enter=True, send_key='shift+enter')
        self.send_hotkey.assert_called_with('shift', 'enter')

    def test_a_delayed_send_presses_the_rules_keys_when_it_fires(self):
        from koli.delayed_send import DelayedSend
        self.manager.delayed_send = DelayedSend(send_enter=self.manager.send_enter_key, foreground=lambda: {},
                                                publish=lambda *a: None, run_thread=False,
                                                typing_watch=mock.Mock(start=lambda *a: 1))
        self.manager.deliver_transcription('hi', use_auto_enter=True, allow_delay=True, send_delay=3,
                                           send_key='ctrl+enter')
        self.assertTrue(self.manager.delayed_send.send_now())
        self.send_hotkey.assert_called_once_with('ctrl', 'enter')
        # A cancelled countdown leaves nothing behind for the next send.
        self.manager.deliver_transcription('hi', use_auto_enter=True, allow_delay=True, send_delay=3,
                                           send_key='shift+enter')
        self.manager.delayed_send.cancel('test')
        self.manager.send_enter_key()
        self.send_key.assert_called_once_with('enter')

    def test_a_trailing_space_never_before_a_send(self):
        self.manager.trailing_space = True
        self.manager.deliver_transcription('Hello.')
        self.assertEqual(self.manager.execute_delivery.call_args[0][0], 'Hello. ')
        self.manager.deliver_transcription('Hello.', use_auto_enter=True)
        self.assertEqual(self.manager.execute_delivery.call_args[0][0], 'Hello.')


@unittest.skipUnless(sys.platform == 'win32' and not os.environ.get('CI'), 'Windows clipboard')
class WindowsClipboardRoundTripTests(unittest.TestCase):
    def test_every_format_comes_back(self):
        from koli.platform.windows import clipboard
        user32 = ctypes.WinDLL('user32')
        user32.RegisterClipboardFormatW.restype = ctypes.c_uint
        custom = user32.RegisterClipboardFormatW('Koli test format')
        users = clipboard.snapshot()
        try:
            text = 'שלום hello\0'.encode('utf-16-le')
            self.assertTrue(clipboard.restore(clipboard.Snapshot([(13, text), (custom, b'\x01\x02\x03')])))
            before = clipboard.sequence()
            got = dict(clipboard.snapshot().items)
            self.assertEqual(got[custom][:3], b'\x01\x02\x03')
            self.assertTrue(got[13].startswith('שלום hello'.encode('utf-16-le')))
            self.assertNotIn(1, got)   # CF_TEXT is made from the Unicode text; not saved twice
            self.assertEqual(clipboard.sequence(), before)   # reading doesn't count as a change
        finally:
            if users is not None:
                clipboard.restore(users)


if __name__ == '__main__':
    unittest.main()
