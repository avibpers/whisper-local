# tests/test_agent_chat.py
# The conversation with the agent: what opens it, what it shows, when it closes,
# and that "open download folder" runs "open downloads" (2026-11-06).

import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from koli.agent.chat import ChatSession  # noqa: E402


class FakeTimer:
    made = []

    def __init__(self, seconds, fn):
        self.seconds, self.fn, self.cancelled, self.daemon = seconds, fn, False, False
        FakeTimer.made.append(self)

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled:
            self.fn()


def session(seconds=20):
    events = []
    FakeTimer.made = []
    return ChatSession(publish=events.append, silence_seconds=lambda: seconds, timer=FakeTimer), events


def live_timer():
    return next(t for t in reversed(FakeTimer.made) if not t.cancelled)


class SessionTests(unittest.TestCase):
    def test_it_opens_once_and_starts_empty(self):
        chat, events = session()
        chat.open('test')
        chat.you('music')
        chat.open('again')
        self.assertEqual([e['event'] for e in events], ['open', 'turn'])
        chat.close()
        chat.open('new one')
        self.assertEqual(chat.turns, [], 'a new conversation starts empty')

    def test_turns_are_shown_while_open_only(self):
        chat, events = session()
        chat.you('nobody hears this')
        self.assertEqual(events, [])
        chat.open()
        chat.you('open downloads')
        chat.koli('Done: open downloads', 'done')
        self.assertEqual([(t['who'], t['text'], t['kind']) for t in chat.turns],
                         [('you', 'open downloads', ''), ('koli', 'Done: open downloads', 'done')])

    def test_quiet_closes_it(self):
        chat, events = session(seconds=30)
        chat.open()
        self.assertEqual(live_timer().seconds, 30)
        live_timer().fire()
        self.assertFalse(chat.is_open())
        self.assertEqual(events[-1]['event'], 'close')
        self.assertEqual(events[-1]['why'], 'quiet')

    def test_each_turn_restarts_the_quiet(self):
        chat, _ = session()
        chat.open()
        first = live_timer()
        chat.you('next song')
        self.assertTrue(first.cancelled)
        self.assertIsNot(live_timer(), first)

    def test_the_quiet_waits_while_the_ai_works_and_asks(self):
        chat, events = session()
        chat.open()
        chat.on_agent({'phase': 'thinking', 'text': 'Thinking…'})
        self.assertEqual(chat.state, 'working')
        self.assertTrue(all(t.cancelled for t in FakeTimer.made), 'no clock while it works')
        chat.on_agent({'phase': 'ask', 'text': "Only today's pictures?", 'needs': 'voice'})
        self.assertTrue(all(t.cancelled for t in FakeTimer.made), 'nor while it waits for your answer')
        chat.on_agent({'phase': 'done', 'text': 'Moved 3 pictures', 'lines': ['Move a.jpg']})
        chat.listening()
        self.assertTrue(chat.is_open())
        self.assertEqual(chat.turns[-1]['lines'], ['Move a.jpg'])
        live_timer().fire()
        self.assertFalse(chat.is_open())

    def test_an_old_timer_never_closes_a_new_conversation(self):
        chat, _ = session()
        chat.open()
        old = live_timer()
        chat.close()
        chat.open()
        old.cancelled = False
        old.fire()
        self.assertTrue(chat.is_open())

    def test_agent_events_outside_a_conversation_stay_in_the_bubble(self):
        chat, events = session()
        chat.on_agent({'phase': 'done', 'text': 'x'})
        self.assertEqual(events, [])

    def test_a_silence_setting_that_makes_no_sense_uses_the_default(self):
        chat, _ = session(seconds='soon')
        chat.open()
        self.assertEqual(live_timer().seconds, 20)
        chat2, _ = session(seconds=1)
        chat2.open()
        self.assertEqual(live_timer().seconds, 5, 'never shorter than 5 s')


class StopTests(unittest.TestCase):
    def test_saying_only_stop_ends_it(self):
        from koli.agent.chat import is_stop
        for said in ('Abort, abort, stop the task', 'stop', 'cancel that', "that's all", "that's all, thanks",
                     'never mind', 'please stop now', 'עצור', 'די', 'תפסיק את זה'):
            self.assertTrue(is_stop(said), said)

    def test_a_command_with_stop_in_it_is_still_a_command(self):
        from koli.agent.chat import is_stop
        for said in ('Stop the music', 'stop playing music', 'open downloads', 'thank you', 'close the browser tab'):
            self.assertFalse(is_stop(said), said)

    def test_the_music_goes_down_while_it_is_open(self):
        changes = []
        chat = ChatSession(publish=lambda p: None, timer=FakeTimer, on_change=changes.append)
        chat.open()
        chat.open()
        chat.close()
        chat.close()
        self.assertEqual(changes, [True, False])

    def test_a_failing_music_step_never_stops_the_conversation(self):
        def broken(is_open):
            raise RuntimeError('no audio device')
        chat = ChatSession(publish=lambda p: None, timer=FakeTimer, on_change=broken)
        chat.open()
        self.assertTrue(chat.is_open())


class MatchTests(unittest.TestCase):
    def _manager(self, *triggers):
        from koli.voice_commands import VoiceCommandManager
        manager = VoiceCommandManager.__new__(VoiceCommandManager)
        manager.logger = mock.Mock()
        manager.enabled = True
        manager.open_apps_by_name = False
        manager.commands = [{'trigger': t, 'hotkey': 'x'} for t in triggers]
        manager._reload_if_changed = lambda: None
        return manager

    def test_a_plural_or_a_missing_s_still_runs_the_command(self):
        manager = self._manager('open downloads')
        for said in ('open download folder', 'Open download folder.', 'open downloads', 'please open downloads now'):
            self.assertEqual((manager.match_command(said) or {}).get('trigger'), 'open downloads', said)

    def test_words_must_still_come_in_a_row(self):
        manager = self._manager('open downloads')
        for said in ('open the downloads folder', 'download and open', 'opened downloads'):
            self.assertIsNone(manager.match_command(said), said)

    def test_short_words_and_double_s_are_left_alone(self):
        from koli.voice_commands import phrase_in
        self.assertFalse(phrase_in('bus', 'bu stop'))
        self.assertFalse(phrase_in('press enter', 'pres enter'))
        self.assertTrue(phrase_in('next song', 'next songs'))


class PipelineTests(unittest.TestCase):
    def _sm(self, agent_enabled=True):
        from koli.state_manager import StateManager
        sm = StateManager.__new__(StateManager)
        sm.logger = mock.Mock()
        sm.config_manager = mock.Mock()
        sm.config_manager.get_logging_config.return_value = {}
        sm.config_manager.get_path.side_effect = lambda path, default=None: default
        manager = MatchTests._manager(self, 'open downloads', 'music')
        manager.execute_command = mock.Mock()
        sm.voice_command_manager = manager
        sm.agent = mock.Mock(busy=False)
        sm.agent.handle.side_effect = lambda text: sm.agent_chat.on_agent({'phase': 'done', 'text': f'Did: {text}'})
        sm._agent_enabled = lambda: agent_enabled
        self.events = []
        sm.agent_chat = ChatSession(publish=self.events.append, timer=FakeTimer)
        return sm

    def test_a_fixed_command_in_the_conversation_says_it_ran(self):
        sm = self._sm()
        sm.agent_chat.open()
        self.assertTrue(sm._handle_command_transcription('open download folder', source='personal'))
        self.assertEqual(sm.agent_chat.turns[-1]['text'], 'Done: open downloads')

    def test_anything_else_goes_to_the_ai_and_the_conversation_listens_after(self):
        sm = self._sm()
        sm.agent_chat.open()
        sm._handle_command_transcription('make a folder called Invoices', source='personal')
        sm.agent.handle.assert_called_once_with('make a folder called Invoices')
        self.assertEqual(sm.agent_chat.turns[-1]['text'], 'Did: make a folder called Invoices')
        self.assertEqual(sm.agent_chat.state, 'listening')

    def test_the_ai_taking_a_first_command_opens_the_conversation(self):
        sm = self._sm()
        sm._handle_command_transcription('make a folder called Invoices', source='personal')
        self.assertTrue(sm.agent_chat.is_open())
        self.assertEqual([t['who'] for t in sm.agent_chat.turns], ['you', 'koli'])

    def test_a_miss_with_the_ai_off_says_so(self):
        sm = self._sm(agent_enabled=False)
        sm.agent_chat.open()
        self.assertFalse(sm._handle_command_transcription('do something clever', source='personal'))
        self.assertIn('AI actions are off', sm.agent_chat.turns[-1]['text'])

    def test_stop_in_the_conversation_ends_it_and_stops_the_ai(self):
        sm = self._sm()
        sm.agent.busy = True
        sm.agent_chat.open()
        self.assertTrue(sm._handle_command_transcription('Abort, abort, stop the task', source='personal'))
        sm.agent.handle.assert_not_called()
        sm.agent.cancel.assert_called_once()
        self.assertFalse(sm.agent_chat.is_open())
        self.assertEqual(sm.agent_chat.turns[-1]['text'], 'Stopped.')

    def test_stop_outside_the_conversation_is_an_ordinary_command(self):
        sm = self._sm()
        sm._handle_command_transcription('stop', source='personal')
        sm.agent.handle.assert_called_once_with('stop')

    def test_the_music_control_lowers_to_your_level_and_never_pauses(self):
        sm = self._sm()
        sm.config_manager.config = {'audio': {'lower_volume': {'enabled': True, 'level': 10}, 'pause_media_on_record': True}}
        control = mock.Mock()
        sm.__dict__['_chat_media'] = control
        sm._chat_music(True)
        control.on_recording_start.assert_called_once_with({'lower_volume': {'enabled': True, 'level': 10}})
        sm._chat_music(False)
        control.on_recording_end.assert_called_once()

    def test_closing_stops_what_the_ai_is_doing(self):
        sm = self._sm()
        sm.agent.busy = True
        sm.agent_chat.open()
        sm.close_agent_chat()
        sm.agent.cancel.assert_called_once()
        self.assertFalse(sm.agent_chat.is_open())

    def test_said_after_anything_else_opens_it_and_shows_what_you_said(self):
        from tests.test_wake_word import WakeCommandStateManagerTests
        case = WakeCommandStateManagerTests()
        sm = case._sm(heard='open download folder', commands=[{'trigger': 'open downloads', 'run': 'x'}])
        sm._wake_listener.personal.return_value = True
        sm.agent_chat = ChatSession(publish=lambda p: None, timer=FakeTimer)
        sm.run_wake_command(audio=[0.0], only_media=False, report_miss=False, chat=True)
        self.assertTrue(sm.agent_chat.is_open())
        self.assertEqual([t['text'] for t in sm.agent_chat.turns], ['open download folder', 'Done: open downloads'])


if __name__ == '__main__':
    unittest.main()
