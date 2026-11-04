# tests/test_audio_capture.py
# The recorder's capture path: a stereo stream is mixed to one channel at the
# callback (a flattened two-channel array interleaved left and right into
# garbled audio before).

import collections
import logging
import sys
import threading
import time
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from koli.audio_recorder import AudioRecorder  # noqa: E402


# A recorder with only what the callback touches; no stream is opened.
def bare_recorder(recording=True):
    rec = AudioRecorder.__new__(AudioRecorder)
    rec.logger = logging.getLogger('test')
    rec.is_recording = recording
    rec.level_preview = False
    rec._current_level = 0.0
    rec._buffer_lock = threading.Lock()
    rec._buffer = collections.deque()
    rec._preroll_max_chunks = 4
    rec._taps = []
    rec.continuous_vad = None
    rec.continuous_streaming = None
    rec.stream_partials = False
    rec._vad_blocksize = 256
    rec._needs_resampling_cached = False
    return rec


class StereoMixTests(unittest.TestCase):
    def test_a_stereo_chunk_is_mixed_to_one_channel(self):
        rec = bare_recorder()
        left = np.full(256, 0.4, dtype=np.float32)
        right = np.full(256, 0.0, dtype=np.float32)
        rec._audio_callback(np.stack([left, right], axis=1), 256, None, None)
        chunk = rec._buffer[-1]
        self.assertEqual(chunk.shape, (256, 1))
        self.assertTrue(np.allclose(chunk, 0.2))
        self.assertEqual(chunk.dtype, np.float32)

    def test_a_mono_chunk_is_kept_as_it_is(self):
        rec = bare_recorder()
        mono = np.linspace(-1, 1, 256, dtype=np.float32).reshape(-1, 1)
        rec._audio_callback(mono, 256, None, None)
        self.assertTrue(np.array_equal(rec._buffer[-1], mono))

    def test_int16_stays_int16(self):
        rec = bare_recorder()
        stereo = np.full((256, 2), 1000, dtype=np.int16)
        rec._audio_callback(stereo, 256, None, None)
        self.assertEqual(rec._buffer[-1].dtype, np.int16)


# ── Microphones by name and recovery ────────────────────────────────────────

from unittest import mock  # noqa: E402

from koli import audio_recorder  # noqa: E402

DEVICES = [
    {'name': 'Microphone (EMEET Piko+)', 'hostapi': 0, 'max_input_channels': 1},       # MME
    {'name': 'Speakers (Realtek)', 'hostapi': 1, 'max_input_channels': 0},
    {'name': 'Microphone (EMEET Piko+)', 'hostapi': 1, 'max_input_channels': 1},       # WASAPI
    {'name': 'Microphone (Logitech USB Headse', 'hostapi': 0, 'max_input_channels': 1},
]
HOSTS = [{'name': 'MME'}, {'name': 'Windows WASAPI'}]


class MicByNameTests(unittest.TestCase):
    def test_same_mic_matches_a_name_cut_by_mme(self):
        self.assertTrue(audio_recorder.same_mic('Microphone (Logitech USB Headset)', 'Microphone (Logitech USB Headse'))
        self.assertTrue(audio_recorder.same_mic('microphone (emeet piko+)', 'Microphone (EMEET Piko+)'))
        self.assertFalse(audio_recorder.same_mic('Microphone (EMEET Piko+)', 'Microphone (EMEET'))
        self.assertFalse(audio_recorder.same_mic('', 'x'))

    def test_a_name_is_found_on_the_chosen_driver_first(self):
        with mock.patch.object(audio_recorder.sd, 'query_devices', return_value=DEVICES), \
             mock.patch.object(audio_recorder.sd, 'query_hostapis', return_value=HOSTS):
            self.assertEqual(audio_recorder.find_input_device('Microphone (EMEET Piko+)', 'Windows WASAPI'), 2)
            self.assertEqual(audio_recorder.find_input_device('Microphone (EMEET Piko+)', 'MME'), 0)
            # Automatic is WASAPI on Windows, though MME is listed first.
            with mock.patch.object(audio_recorder, 'app_is_windows', return_value=True):
                self.assertEqual(audio_recorder.find_input_device('Microphone (EMEET Piko+)'), 2)
            with mock.patch.object(audio_recorder, 'app_is_windows', return_value=False):
                self.assertEqual(audio_recorder.find_input_device('Microphone (EMEET Piko+)'), 0)
            self.assertEqual(audio_recorder.find_input_device('Microphone (Logitech USB Headset)'), 3)
            self.assertIsNone(audio_recorder.find_input_device('Microphone (BY-V20)'))
            self.assertIsNone(audio_recorder.resolve_input('default'))
            self.assertEqual(audio_recorder.resolve_input(7), 7)
            with self.assertRaises(LookupError):
                audio_recorder.resolve_input('Microphone (BY-V20)')

    def test_a_microphone_that_isnt_plugged_in_records_from_the_default_until_it_is(self):
        rec = bare_recorder(recording=False)
        with mock.patch.object(audio_recorder.desktop_session, 'is_remote', lambda: False), \
             mock.patch.object(audio_recorder, 'find_input_device', return_value=None), \
             mock.patch.object(rec, '_resolve_hostapi'), \
             mock.patch.object(AudioRecorder, '_device_name', side_effect=lambda d: 'the default mic'), \
             mock.patch.object(audio_recorder, '_report_problem') as report:
            rec.resolve_device('Microphone (BY-V20)')
        self.assertIsNone(rec.device)
        self.assertEqual(rec.chosen_name, 'Microphone (BY-V20)')
        self.assertIn("isn't plugged in", report.call_args[0][0])

    def test_a_device_number_remembers_its_name(self):
        rec = bare_recorder(recording=False)
        with mock.patch.object(audio_recorder.desktop_session, 'is_remote', lambda: False), \
             mock.patch.object(audio_recorder.sd, 'query_devices', lambda d=None: DEVICES[d]), \
             mock.patch.object(rec, '_resolve_hostapi'):
            rec.resolve_device(2)
        self.assertEqual((rec.device, rec.chosen_name), (2, 'Microphone (EMEET Piko+)'))


# A stream that opens, never calls back, and says it is active.
class SilentStream:
    active = True

    def __init__(self, **_kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RecoveryTests(unittest.TestCase):
    def test_a_stream_that_stops_sending_audio_is_treated_as_lost(self):
        rec = bare_recorder(recording=False)
        rec._capture_running, rec._stream_error = True, None
        rec._recording_rate, rec.channels, rec.device = 16000, 1, None
        rec.chosen_name = None
        with mock.patch.object(audio_recorder.sd, 'InputStream', SilentStream), \
             mock.patch.object(audio_recorder.sd, 'sleep', lambda ms: None), \
             mock.patch.object(AudioRecorder, 'STALL_SECONDS', 0.05):
            with self.assertRaisesRegex(RuntimeError, 'no audio'):
                rec._run_stream()

    def test_the_stream_is_reopened_however_many_times_it_fails(self):
        rec = bare_recorder(recording=False)
        rec._capture_running, rec._stream_error = True, None
        outcomes = [OSError('device unavailable')] * 6 + [False]

        def run_stream():
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        with mock.patch.object(rec, '_run_stream', side_effect=run_stream), \
             mock.patch.object(rec, '_reopen') as reopen, \
             mock.patch.object(audio_recorder.time, 'sleep'):
            rec._run_capture()
        self.assertEqual(reopen.call_count, 6)
        self.assertEqual(outcomes, [])

    # Python held up (a model call holding its lock) isn't a dead microphone.
    def test_a_stall_while_python_was_held_up_is_not_a_lost_stream(self):
        rec = bare_recorder(recording=False)
        rec._capture_running, rec._stream_error = True, None
        rec._recording_rate, rec.channels, rec.device = 16000, 1, None
        rec.chosen_name = None
        turns = []

        def sleep(ms):
            turns.append(ms)
            if len(turns) == 1:
                time.sleep(0.2)        # the held-up turn: longer than the stall limit
            elif len(turns) == 2:
                rec._capture_running = False
        with mock.patch.object(audio_recorder.sd, 'InputStream', SilentStream), \
             mock.patch.object(audio_recorder.sd, 'sleep', sleep), \
             mock.patch.object(AudioRecorder, 'STALL_SECONDS', 0.1), \
             mock.patch.object(AudioRecorder, 'LOOP_STARVED_S', 0.05):
            self.assertFalse(rec._run_stream())

    # A stream that ran fine for a long time starts the count over, so its
    # reopen doesn't wait as if it had been failing all along.
    def test_a_healthy_stream_starts_the_failure_count_over(self):
        rec = bare_recorder(recording=False)
        rec._capture_running, rec._stream_error = True, None
        waits = []
        runs = iter([60.0, 60.0, 60.0, None])

        def run_stream():
            ran = next(runs)
            if ran is None:
                return False
            rec._stream_opened_at = time.monotonic() - ran
            raise RuntimeError('no audio for 2 s')
        with mock.patch.object(rec, '_run_stream', side_effect=run_stream), \
             mock.patch.object(rec, '_reopen'), \
             mock.patch.object(rec, '_wait_before_retry', side_effect=waits.append):
            rec._run_capture()
        self.assertEqual(waits, [1, 1, 1])

    def test_failing_to_open_keeps_counting(self):
        rec = bare_recorder(recording=False)
        rec._capture_running, rec._stream_error = True, None
        waits = []
        outcomes = [OSError('no device')] * 3 + [False]

        def run_stream():
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        with mock.patch.object(rec, '_run_stream', side_effect=run_stream), \
             mock.patch.object(rec, '_reopen'), \
             mock.patch.object(rec, '_wait_before_retry', side_effect=waits.append):
            rec._run_capture()
        self.assertEqual(waits, [1, 2, 3])

    # A long absence waits long, until something could have changed.
    def test_a_long_absence_waits_until_the_microphone_list_changes(self):
        rec = bare_recorder(recording=False)
        rec._capture_running = True
        lists = iter([['A'], ['A'], ['A', 'B']])
        started = time.monotonic()
        with mock.patch.object(rec, '_live_microphones', side_effect=lambda: next(lists)), \
             mock.patch.object(AudioRecorder, 'RETRY_LIST_CHECK_S', 0.01), \
             mock.patch.object(AudioRecorder, 'RETRY_SLOW_WAIT_S', 30.0):
            rec._wait_before_retry(AudioRecorder.RETRY_QUICK_TRIES + 1)
        self.assertLess(time.monotonic() - started, 5.0)

    def test_a_recording_never_sits_out_the_long_wait(self):
        rec = bare_recorder(recording=True)
        rec._capture_running = True
        with mock.patch.object(audio_recorder.time, 'sleep') as sleep:
            rec._wait_before_retry(AudioRecorder.RETRY_QUICK_TRIES + 50)
        sleep.assert_called_once_with(AudioRecorder.RETRY_MAX_WAIT_S)

    def test_a_recording_keeps_its_audio_across_a_rate_change(self):
        rec = bare_recorder(recording=True)
        rec._buffer.extend([np.zeros((480, 1), np.float32), np.ones((480, 1), np.float32)])
        rec._carry_buffer(48000, 16000)
        self.assertEqual(len(rec._buffer), 1)
        self.assertEqual(rec._buffer[0].shape, (320, 1))
        idle = bare_recorder(recording=False)
        idle._buffer.append(np.zeros((480, 1), np.float32))
        idle._carry_buffer(48000, 16000)
        self.assertEqual(len(idle._buffer), 0)

    def test_your_microphone_coming_back_is_noticed_when_idle(self):
        rec = bare_recorder(recording=False)
        rec.device, rec.chosen_name = None, 'Microphone (EMEET Piko+)'
        live = mock.Mock(microphones=lambda: ['Microphone (Logitech USB Headset)', 'Microphone (EMEET Piko+)'])
        with mock.patch.object(audio_recorder.desktop_session, 'is_remote', lambda: False), \
             mock.patch.dict(sys.modules, {'koli.platform.audio_sessions': live}), \
             mock.patch('koli.platform.audio_sessions', live, create=True):
            self.assertTrue(rec._mine_is_back())
            rec.is_recording = True
            self.assertFalse(rec._mine_is_back(), 'never in the middle of a dictation')
            rec.is_recording = False
            rec.may_refresh_devices = lambda: False
            self.assertFalse(rec._mine_is_back(), 'not while a meeting holds the device list')


class ReleaseTailTests(unittest.TestCase):
    def sm(self, ms, auto=False):
        from koli.state_manager import StateManager
        sm = StateManager.__new__(StateManager)
        sm.config_manager = mock.Mock(get_path=lambda path, default=None: ms if path == 'audio.release_tail_ms' else default)
        sm._auto_started = auto
        return sm

    def test_a_hotkey_recording_keeps_listening_a_moment(self):
        from koli import state_manager
        with mock.patch.object(state_manager.time, 'sleep') as sleep:
            self.sm(300)._record_release_tail()
            sleep.assert_called_once_with(0.3)
            sleep.reset_mock()
            self.sm(0)._record_release_tail()
            self.sm(300, auto=True)._record_release_tail()
            self.sm(5000)._record_release_tail()   # nonsense values are ignored
            sleep.assert_not_called()


class SilentMicTests(unittest.TestCase):
    def test_a_silent_microphone_is_named_once_a_minute(self):
        rec = bare_recorder(recording=False)
        rec.device = 3
        with mock.patch.object(AudioRecorder, '_device_name', side_effect=lambda d: 'Microphone (EMEET Piko+)'), \
             mock.patch.object(audio_recorder, '_report_problem') as report:
            rec._report_silent_mic()
            rec._report_silent_mic()
        report.assert_called_once()
        self.assertIn('No sound came from Microphone (EMEET Piko+)', report.call_args[0][0])
        self.assertIn('Privacy', report.call_args[0][0])


if __name__ == '__main__':
    unittest.main()
