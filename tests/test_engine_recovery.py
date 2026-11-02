# tests/test_engine_recovery.py
# A decode that hangs or hits a graphics card error must not freeze Koli:
# the engine thread gives up at a deadline and a fresh one takes over, and
# the engine moves to a fresh CPU model and tries the clip once more.
# Unloading when idle frees the model and the next use loads it again.

import logging
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from koli import whisper_engine  # noqa: E402
from koli.whisper_engine import EngineTimeout, WhisperEngine, _EngineThread  # noqa: E402


class EngineThreadTests(unittest.TestCase):
    def setUp(self):
        self.release = threading.Event()
        self.addCleanup(self.release.set)   # lets the abandoned thread finish

    def test_a_hung_call_is_given_up_and_the_next_runs_on_a_fresh_thread(self):
        engine = _EngineThread('test-engine')
        first = engine._ensure_started() and engine._thread
        with self.assertRaises(EngineTimeout):
            engine.run_with_deadline(0.1, self.release.wait)
        self.assertEqual(engine.run(lambda: 'answered'), 'answered')
        self.assertIsNot(engine._thread, first)

    def test_work_queued_behind_a_hung_call_still_runs(self):
        engine = _EngineThread('test-engine')
        results = []
        hung = threading.Thread(target=lambda: self.assertRaises(
            EngineTimeout, engine.run_with_deadline, 0.3, self.release.wait))
        hung.start()
        while engine._thread is None:
            pass
        waiter = threading.Thread(target=lambda: results.append(engine.run(lambda: 'queued')))
        waiter.start()
        hung.join(5)
        waiter.join(5)
        self.assertEqual(results, ['queued'])

    def test_errors_still_come_back_to_the_caller(self):
        engine = _EngineThread('test-engine')
        with self.assertRaisesRegex(ValueError, 'bad'):
            engine.run_with_deadline(5, lambda: (_ for _ in ()).throw(ValueError('bad')))


class FakeModel:
    def __init__(self, error=None, answer='ok'):
        self.error, self.answer = error, answer


def bare_engine(device='cuda'):
    engine = WhisperEngine.__new__(WhisperEngine)
    engine.logger = logging.getLogger('test')
    engine.model_key = 'base'
    engine.device, engine.compute_type = device, 'float16' if device == 'cuda' else 'int8'
    engine.model = FakeModel(error=RuntimeError('CUDA failed with error out of memory'))
    engine.language_model = FakeModel()
    engine._get_model_source = lambda key: key
    return engine


class EngineRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.thread = _EngineThread('test-engine')
        patches = [mock.patch.object(whisper_engine, 'ENGINE_THREAD', self.thread),
                   mock.patch.object(whisper_engine, 'WhisperModel',
                                     side_effect=lambda *a, **k: FakeModel(answer=f"cpu:{k.get('device')}")),
                   mock.patch.object(whisper_engine, '_report_problem')]
        self.report = patches[2].start()
        for p in patches[:2]:
            p.start()
        for p in patches:
            self.addCleanup(p.stop)

    # The decode as the engine runs it: whatever its current model does.
    @staticmethod
    def decode_with(engine):
        def decode():
            if engine.model.error:
                raise engine.model.error
            return engine.model.answer
        return decode

    def test_a_gpu_error_moves_to_the_cpu_and_tries_again(self):
        engine = bare_engine('cuda')
        self.assertEqual(engine._run_decode(self.decode_with(engine), 3.0), 'cpu:cpu')
        self.assertEqual((engine.device, engine.compute_type), ('cpu', 'int8'))
        self.assertIsNone(engine.language_model)
        self.assertIn('graphics card', self.report.call_args[0][0])

    def test_an_ordinary_error_is_not_retried(self):
        engine = bare_engine('cuda')
        engine.model = FakeModel(error=ValueError('bad audio'))
        with self.assertRaisesRegex(ValueError, 'bad audio'):
            engine._run_decode(self.decode_with(engine), 3.0)
        self.assertEqual(engine.device, 'cuda')

    def test_a_hang_on_the_cpu_reloads_the_cpu_model(self):
        engine = bare_engine('cpu')
        engine.model = FakeModel()
        release = threading.Event()
        self.addCleanup(release.set)
        calls = []

        def decode():
            calls.append(engine.model)
            if len(calls) == 1:
                release.wait()
            return engine.model.answer
        with mock.patch.object(engine, '_deadline_for', return_value=0.2):
            self.assertEqual(engine._run_decode(decode, 3.0), 'cpu:cpu')
        self.assertIsNot(calls[0], calls[1])
        self.assertIn('restarted', self.report.call_args[0][0])

    def test_a_second_failure_on_the_same_model_doesnt_reload_twice(self):
        engine = bare_engine('cuda')
        self.assertTrue(engine._recover(RuntimeError('CUDA error'), 0))
        loaded = engine.model
        self.assertTrue(engine._recover(RuntimeError('CUDA error'), 0))
        self.assertIs(engine.model, loaded)
        self.assertEqual(whisper_engine.WhisperModel.call_count, 1)

    def test_deadlines_grow_with_the_clip(self):
        engine = bare_engine('cuda')
        self.assertEqual(engine._deadline_for(2), 60.0)
        self.assertEqual(engine._deadline_for(30), 300.0)
        engine.device = 'cpu'
        self.assertEqual(engine._deadline_for(2), 300.0)
        self.assertEqual(engine._deadline_for(60), 1200.0)


# ── Unloading when idle ──────────────────────────────────────────────────────

class IdleUnloadEngineTests(unittest.TestCase):
    def test_an_idle_unload_is_loaded_again_on_the_next_use(self):
        engine = bare_engine('cuda')
        engine.unload = lambda: setattr(engine, 'model', None)

        def load():
            engine.model = FakeModel(answer='reloaded')
        engine._load_model = load
        self.assertTrue(engine.unload_idle())
        self.assertTrue(engine.idle_unloaded)
        self.assertTrue(engine.ensure_loaded())
        self.assertEqual(engine.model.answer, 'reloaded')
        self.assertFalse(engine.idle_unloaded)

    def test_a_swapped_out_engine_is_never_loaded_again(self):
        engine = bare_engine('cuda')
        engine.model = None          # unload() after a model swap
        engine._load_model = mock.Mock()
        self.assertFalse(engine.ensure_loaded())
        engine._load_model.assert_not_called()


class IdleUnloadWatchTests(unittest.TestCase):
    def sm(self, minutes=5, **flags):
        from koli.state_manager import StateManager
        sm = StateManager.__new__(StateManager)
        sm.logger = logging.getLogger('test')
        values = {'whisper.unload_after_minutes': minutes, **flags}
        sm.config_manager = mock.Mock(get_path=lambda path, default=None: values.get(path, default))
        sm.is_idle = lambda: True
        sm.meetings = None
        sm.whisper_engine = mock.Mock(unload_idle=mock.Mock(return_value=True))
        sm._engine_used_at = 0.0
        return sm

    def test_the_model_is_unloaded_after_the_idle_minutes(self):
        sm = self.sm(minutes=5)
        self.assertFalse(sm._maybe_unload_idle_model(now=4 * 60))
        self.assertTrue(sm._maybe_unload_idle_model(now=5 * 60 + 1))

    def test_never_by_default_or_while_something_needs_it(self):
        self.assertFalse(self.sm(minutes=0)._maybe_unload_idle_model(now=1e6))
        self.assertFalse(self.sm(**{'wake_word.enabled': True})._maybe_unload_idle_model(now=1e6))
        self.assertFalse(self.sm(**{'auto_dictation.enabled': True})._maybe_unload_idle_model(now=1e6))
        busy = self.sm()
        busy.meetings = mock.Mock(is_running=lambda: True)
        self.assertFalse(busy._maybe_unload_idle_model(now=1e6))
        recording = self.sm()
        recording.is_idle = lambda: False
        self.assertFalse(recording._maybe_unload_idle_model(now=1e6))

    def test_a_recording_starts_loading_an_unloaded_model(self):
        sm = self.sm()
        sm.whisper_engine = mock.Mock(idle_unloaded=True)
        with mock.patch('koli.state_manager.threading.Thread') as thread:
            sm._touch_engine()
        thread.assert_called_once()
        self.assertIs(thread.call_args.kwargs['target'], sm.whisper_engine.ensure_loaded)


if __name__ == '__main__':
    unittest.main()
