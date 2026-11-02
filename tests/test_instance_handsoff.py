# tests/test_instance_handsoff.py
# A --test copy started while your Koli runs stays hands-off (no hotkeys,
# auto-dictation, wake word, meetings): both copies used to hear the hotkey
# and every dictation was pasted twice. The lock check never takes the lock.

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from koli import main  # noqa: E402


class HandsOffTests(unittest.TestCase):
    def test_only_a_test_copy_next_to_your_koli_is_hands_off(self):
        with mock.patch.object(main.instance_lock, 'is_held', return_value=True) as held:
            self.assertTrue(main.test_copy_is_hands_off(True))
            held.assert_called_once_with('Koli')
            self.assertFalse(main.test_copy_is_hands_off(False))
        with mock.patch.object(main.instance_lock, 'is_held', return_value=False):
            self.assertFalse(main.test_copy_is_hands_off(True))

    def test_a_failing_check_never_blocks_startup(self):
        with mock.patch.object(main.instance_lock, 'is_held', side_effect=OSError('no mutex API')):
            self.assertFalse(main.test_copy_is_hands_off(True))


@unittest.skipUnless(sys.platform == 'win32', 'Windows named mutex')
class WindowsLockProbeTests(unittest.TestCase):
    def test_a_held_lock_is_seen_and_not_taken(self):
        import win32api
        from koli.platform.windows import instance_lock
        name = 'KoliProbeTest_Handsoff'
        self.assertFalse(instance_lock.is_held(name))
        handle = instance_lock.acquire_lock(name)
        try:
            self.assertIsNotNone(handle)
            self.assertTrue(instance_lock.is_held(name))
            self.assertTrue(instance_lock.is_held(name), 'probing twice still sees it')
        finally:
            win32api.CloseHandle(handle)
        self.assertFalse(instance_lock.is_held(name))


class QuitFallbackTests(unittest.TestCase):
    # Quitting a test copy must never reach your Koli's PID file.
    def test_the_fallback_reads_the_pid_file_of_the_instance_asked_for(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'Koli.pid').write_text('111')
            Path(folder, 'Koli_test.pid').write_text('222')
            with mock.patch.object(main, 'get_user_app_data_path', return_value=folder), \
                 mock.patch.object(main.os, 'kill') as kill:
                main._send_quit_to_running_instance('Koli_test')
            kill.assert_called_once()
            self.assertEqual(kill.call_args[0][0], 222)


if __name__ == '__main__':
    unittest.main()
