# tests/test_history.py
# History keeps what Whisper heard before clean-up, deletes one entry,
# keeps only so many days when asked, and can try the last empty recording
# again.

import datetime
import json
import logging
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from koli import transcript_log  # noqa: E402
from koli.services import history  # noqa: E402


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patch = mock.patch.object(transcript_log, 'get_user_app_data_path', return_value=self.dir.name)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(setattr, transcript_log, '_keep_days', 0)

    def lines(self):
        path = Path(self.dir.name) / transcript_log._LOG_FILE
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line]

    def test_what_whisper_heard_is_kept_only_when_it_differs(self):
        transcript_log.record_transcript('Hello, world.', heard='hello world')
        transcript_log.record_transcript('Same.', heard='Same.')
        entries = self.lines()
        self.assertEqual(entries[0]['heard'], 'hello world')
        self.assertNotIn('heard', entries[1])

    def test_one_entry_is_deleted(self):
        transcript_log.record_transcript('first')
        transcript_log.record_transcript('second')
        target = transcript_log.load_transcripts()[1]     # newest first: "first"
        self.assertTrue(history.delete(target['timestamp'], 'first'))
        self.assertEqual([e['text'] for e in self.lines()], ['second'])
        with self.assertRaises(RuntimeError):
            history.delete(target['timestamp'], 'first')

    def test_old_entries_go_when_days_are_set(self):
        path = Path(self.dir.name) / transcript_log._LOG_FILE
        old = (datetime.datetime.now() - datetime.timedelta(days=10)).isoformat(timespec='seconds')
        new = datetime.datetime.now().isoformat(timespec='seconds')
        path.write_text(json.dumps({'timestamp': old, 'text': 'old'}) + '\n'
                        + json.dumps({'timestamp': new, 'text': 'new'}) + '\n', encoding='utf-8')
        transcript_log.set_retention(3)
        self.assertEqual([e['text'] for e in self.lines()], ['new'])
        transcript_log.set_retention(0)
        transcript_log.record_transcript('newer')
        self.assertEqual(len(self.lines()), 2)


class RetryTests(unittest.TestCase):
    def sm(self, answer):
        from koli.state_manager import StateManager
        sm = StateManager.__new__(StateManager)
        sm.logger = logging.getLogger('test')
        engine = mock.Mock(last_info={'language': 'en'})
        engine.transcribe_audio.return_value = answer
        sm.dictation_engine = lambda feature='dictation': engine
        sm.config_manager = mock.Mock(get_path=lambda path, default=None: default)
        sm.config_manager.get_postprocess_config.return_value = {'capitalize_first': True}
        sm._with_learned_corrections = lambda cfg: cfg
        sm.clipboard_manager = mock.Mock()
        sm.recent_transcriptions = deque(maxlen=5)
        return sm

    def test_an_empty_recording_can_be_tried_again(self):
        sm = self.sm('send it now')
        sm._keep_failed_recording(object(), 3.0)
        self.assertEqual(sm.last_failed_recording()['seconds'], 3.0)
        with mock.patch('koli.state_manager.record_transcript') as record:
            self.assertEqual(sm.retry_last_recording(), 'Send it now')
        sm.clipboard_manager.copy_text.assert_called_once_with('Send it now')
        self.assertEqual(record.call_args.kwargs['heard'], 'send it now')
        self.assertIsNone(sm.last_failed_recording())

    def test_still_empty_says_so_and_keeps_the_clip(self):
        sm = self.sm(None)
        sm._keep_failed_recording(object(), 3.0)
        with self.assertRaisesRegex(RuntimeError, 'Still no words'):
            sm.retry_last_recording()
        self.assertIsNotNone(sm.last_failed_recording())

    def test_a_file_is_transcribed_into_history_under_its_name(self):
        import numpy as np
        sm = self.sm('the call notes')
        with mock.patch.object(history, 'decode_file', return_value=np.zeros(16000 * 90, np.float32)), \
             mock.patch('koli.state_manager.record_transcript') as record:
            self.assertEqual(sm.transcribe_file(r'C:\calls\Monday call.m4a'), 'The call notes')
        self.assertEqual(record.call_args.kwargs['window'], {'exe': 'file', 'title': 'Monday call.m4a'})
        self.assertEqual(record.call_args.kwargs['duration_s'], 90.0)
        sm.clipboard_manager.copy_text.assert_called_once_with('The call notes')

    def test_a_file_with_no_words_fails_with_its_name(self):
        import numpy as np
        sm = self.sm(None)
        with mock.patch.object(history, 'decode_file', return_value=np.zeros(16000, np.float32)):
            with self.assertRaisesRegex(RuntimeError, 'silence.wav'):
                sm.transcribe_file('silence.wav')

    def test_a_tiny_clip_is_not_kept(self):
        sm = self.sm('x')
        sm._keep_failed_recording(object(), 0.2)
        self.assertIsNone(sm.last_failed_recording())


if __name__ == '__main__':
    unittest.main()
