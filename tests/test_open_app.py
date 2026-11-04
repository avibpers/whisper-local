# Tests for "app <name>": which phrases become the command, and which open
# window a name brings up. Nothing here moves real windows.

import sys
import unittest
from unittest import mock

from koli import voice_commands
from koli.voice_commands import VoiceCommandManager


# ── The phrase ───────────────────────────────────────────────────────────────

class OpenAppPhraseTests(unittest.TestCase):
    def _manager(self, commands=(), enabled=True):
        manager = VoiceCommandManager.__new__(VoiceCommandManager)
        manager.commands = [dict(command) for command in commands]
        manager.open_apps_by_name = enabled
        manager._reload_if_changed = mock.Mock()
        return manager

    def test_app_and_a_name_is_the_command(self):
        command = self._manager().match_command('App, Soul.')
        self.assertEqual(command['open_app'], 'soul')

    def test_the_toggle_turns_it_off(self):
        self.assertIsNone(self._manager(enabled=False).match_command('app soul'))

    def test_a_sentence_with_the_word_app_is_not_the_command(self):
        manager = self._manager()
        self.assertIsNone(manager.match_command('open the app soul'))
        self.assertIsNone(manager.match_command('app'))

    def test_a_phrase_inside_the_name_does_not_run_its_command(self):
        command = self._manager([{'trigger': 'copy', 'hotkey': 'ctrl+c'}]).match_command('app copy')
        self.assertEqual(command['open_app'], 'copy')

    def test_your_own_command_with_that_name_stays_yours(self):
        command = self._manager([{'trigger': 'app store', 'run': 'x'}]).match_command('app store')
        self.assertEqual(command['trigger'], 'app store')
        self.assertNotIn('open_app', command)

    def test_a_narrower_question_never_gets_it(self):
        self.assertIsNone(self._manager().match_command('app soul', only=lambda _: True))

    # At a short pause you may still be talking: no window yet is not a miss.
    def test_at_a_pause_only_a_name_with_a_window_runs(self):
        manager = self._manager()
        with mock.patch.object(voice_commands.window_focus, 'find_window', return_value=None, create=True):
            self.assertIsNone(manager.match_command('app soul', exact=True))
        with mock.patch.object(voice_commands.window_focus, 'find_window', return_value=7, create=True):
            self.assertEqual(manager.match_command('app soul', exact=True)['open_app'], 'soul')

    def test_a_missing_window_says_so_and_stays_with_the_command(self):
        manager = self._manager()
        manager.logger = mock.Mock()
        command = manager.match_command('app soul')
        with mock.patch.object(voice_commands.window_focus, 'open_window', create=True,
                               side_effect=LookupError('no open app is called “soul”')), \
                mock.patch.object(voice_commands, 'publish') as publish:
            manager.execute_command(command)
        self.assertIn('no open app', publish.call_args[0][1]['message'])


# ── The window ───────────────────────────────────────────────────────────────

@unittest.skipUnless(sys.platform == 'win32', 'the window list is Windows only')
class FindWindowTests(unittest.TestCase):
    # Most recently used first, as Windows lists them.
    WINDOWS = [
        (1, 'cursor.exe', 'Notes about telegram - Koli (Workspace) - Cursor'),
        (2, 'chrome.exe', 'Soul docs - Avi'),
        (3, 'cursor.exe', 'Soul tools access permis… - Soul - Cursor'),
        (4, 'cursor.exe', 'main.py - Soul - Cursor'),
        (5, 'telegram.exe', 'Telegram'),
        (6, 'msedge.exe', 'Sign in - Personal - Microsoft​ Edge'),
    ]

    def _find(self, name, front=99):
        from koli.platform.windows import window_focus
        with mock.patch.object(window_focus, '_app_windows', return_value=self.WINDOWS) as listed, \
                mock.patch.object(window_focus, '_user32') as user32:
            user32.GetForegroundWindow.return_value = front
            found = window_focus.find_window(name)
        listed.assert_called_once_with(every_desktop=True)
        return found

    def test_the_main_name_finds_the_last_used_window(self):
        self.assertEqual(self._find('soul'), 2, 'a two-part title is "<main name> - <rest>"')
        self.assertEqual(self._find('koli'), 1)

    def test_the_app_name_finds_the_last_used_window(self):
        self.assertEqual(self._find('cursor'), 1)
        self.assertEqual(self._find('chrome'), 2, 'by the .exe, when the title does not say it')
        self.assertEqual(self._find('edge'), 6)

    def test_a_document_in_the_title_does_not_count(self):
        self.assertEqual(self._find('telegram'), 5, 'not window 1, which only mentions it')
        self.assertIsNone(self._find('notes'))
        self.assertIsNone(self._find('tools'))

    def test_the_start_of_a_word_is_enough_but_not_the_middle(self):
        self.assertEqual(self._find('sou'), 2)
        self.assertIsNone(self._find('oul'))

    def test_saying_it_again_moves_to_the_next(self):
        self.assertEqual(self._find('cursor', front=1), 3)
        self.assertEqual(self._find('cursor', front=3), 1)

    def test_the_only_one_in_front_stays(self):
        self.assertEqual(self._find('telegram', front=5), 5)

    def test_no_such_window(self):
        self.assertIsNone(self._find('spotify'))


if __name__ == '__main__':
    unittest.main()
