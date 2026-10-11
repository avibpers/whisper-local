# tests/test_meeting.py
# Meeting mode: the store, the session state machine, detection, chunking,
# speaker clustering and the transcriber, all without audio hardware.
import datetime
import logging
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

TZ = datetime.timezone(datetime.timedelta(hours=3))


def at(minute, second=0):
    return datetime.datetime(2026, 10, 11, 14, minute, second, tzinfo=TZ)


class StoreTests(unittest.TestCase):
    def setUp(self):
        from koli.meeting.store import MeetingStore
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MeetingStore(Path(self.tmp.name))

    def test_new_meeting_has_you_and_no_turns(self):
        from koli.meeting.store import new_id, new_meeting
        m = new_meeting(new_id(at(30, 12)), 'Discord', {'app': 'discord.exe', 'kind': 'native', 'title': '', 'code': ''}, at(30, 12))
        self.assertEqual(m['id'], '2026-10-11-143012')
        self.assertEqual(m['speakers'], [{'id': 'you', 'name': 'You', 'colour': 0}])
        self.assertEqual(m['turns'], [])
        self.assertEqual(m['started_at'], '2026-10-11T14:30:12+03:00')
        self.assertIsNone(m['ended_at'])

    def test_save_load_list_delete(self):
        from koli.meeting.store import new_meeting
        m = new_meeting('2026-10-11-143012', 'Discord', {'app': 'discord.exe', 'kind': 'native', 'title': '', 'code': ''}, at(30, 12))
        m['turns'].append({'start': 3.2, 'end': 7.9, 'stream': 'loopback', 'speaker': 's1', 'text': 'hello', 'lang': 'en'})
        m['speakers'].append({'id': 's1', 'name': 'Speaker 1', 'colour': 1, 'centroid': [1.0, 0.0], 'count': 1})
        self.store.save(m)
        self.assertEqual(self.store.load(m['id'])['turns'][0]['text'], 'hello')
        heads = self.store.list()
        self.assertEqual(len(heads), 1)
        self.assertEqual(heads[0]['id'], m['id'])
        self.assertEqual(heads[0]['turns'], 1)
        self.assertEqual(heads[0]['speakers'], ['You', 'Speaker 1'])
        self.assertNotIn('centroid', str(heads[0]))
        self.assertTrue(self.store.delete(m['id']))
        self.assertEqual(self.store.list(), [])
        self.assertFalse(self.store.delete(m['id']))

    def test_list_is_newest_first_and_skips_broken_files(self):
        from koli.meeting.store import new_meeting
        for mid in ('2026-10-10-090000', '2026-10-11-090000'):
            self.store.save(new_meeting(mid, 'x', {'app': 'zoom.exe', 'kind': 'native', 'title': '', 'code': ''}, at(0)))
        (Path(self.tmp.name) / '2026-10-12-090000.json').write_text('{not json', encoding='utf-8')
        self.assertEqual([h['id'] for h in self.store.list()], ['2026-10-11-090000', '2026-10-10-090000'])

    def test_export_formats(self):
        from koli.meeting.store import export_text, new_meeting
        m = new_meeting('2026-10-11-143012', 'Team sync', {'app': 'discord.exe', 'kind': 'native', 'title': '', 'code': ''}, at(30, 12))
        m['speakers'].append({'id': 's1', 'name': 'Ilona', 'colour': 1})
        m['turns'] += [{'start': 3.0, 'end': 5.0, 'stream': 'loopback', 'speaker': 's1', 'text': 'Hi', 'lang': 'en'},
                       {'start': 6.0, 'end': 8.0, 'stream': 'mic', 'speaker': 'you', 'text': 'Hello', 'lang': 'en'}]
        txt = export_text(m, 'txt')
        self.assertIn('[00:03] Ilona: Hi', txt)
        self.assertIn('[00:06] You: Hello', txt)
        md = export_text(m, 'md')
        self.assertTrue(md.startswith('# Team sync'))
        self.assertIn('**Ilona**', md)
        with self.assertRaises(ValueError):
            export_text(m, 'pdf')

    def test_throttled_saver_writes_once_until_flushed(self):
        from koli.meeting.store import ThrottledSaver, new_meeting
        m = new_meeting('2026-10-11-143012', 'x', {'app': 'zoom.exe', 'kind': 'native', 'title': '', 'code': ''}, at(0))
        saver = ThrottledSaver(self.store, interval=60.0)
        saver.mark(m)
        self.assertTrue(self.store.path(m['id']).exists())   # the first mark writes at once
        m['turns'].append({'start': 1.0, 'end': 2.0, 'stream': 'mic', 'speaker': 'you', 'text': 'a', 'lang': 'en'})
        saver.mark(m)
        self.assertEqual(self.store.load(m['id'])['turns'], [])   # the next one waits
        saver.flush()
        self.assertEqual(len(self.store.load(m['id'])['turns']), 1)


class FakeClock:
    def __init__(self, start):
        self.t = start
        self.timers = []   # (due, fn, alive)

    def now(self):
        return self.t

    def schedule(self, seconds, fn):
        entry = [self.t + datetime.timedelta(seconds=seconds), fn, True]
        self.timers.append(entry)
        return lambda: entry.__setitem__(2, False)

    def advance(self, seconds):
        self.t += datetime.timedelta(seconds=seconds)
        for entry in list(self.timers):
            if entry[2] and entry[0] <= self.t:
                entry[2] = False
                entry[1]()


def discord(mode='auto'):
    return {'key': 'discord.exe', 'app': 'discord.exe', 'kind': 'native', 'title': '', 'code': '', 'mode': mode}


def meet(code='abc-defg-hij', mode='auto'):
    return {'key': f'chrome.exe|Meet - {code}', 'app': 'chrome.exe', 'kind': 'browser', 'title': f'Meet - {code}',
            'code': code, 'mode': mode}


def started_at(meeting):
    from koli.meeting.store import started_ts
    return started_ts(meeting)


class SessionTests(unittest.TestCase):
    def setUp(self):
        from koli.meeting.session import SessionManager
        from koli.meeting.store import MeetingStore
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MeetingStore(Path(self.tmp.name))
        self.clock = FakeClock(at(30))
        self.captures = []
        self.events = []
        self.settings = {'meeting.gap_minutes': 10, 'meeting.start_speech_seconds': 3, 'meeting.language': 'auto',
                         'meeting.include_me': True}
        self.sm = SessionManager(self.store, start_capture=lambda m: self.captures.append(('start', m['id'])),
                                 stop_capture=lambda: self.captures.append(('stop',)),
                                 publish=lambda topic, payload: self.events.append((topic, payload)),
                                 settings=lambda path, default=None: self.settings.get(path, default),
                                 now=self.clock.now, schedule=self.clock.schedule)

    def live(self, source=None):
        source = source or discord()
        self.sm.on_sources([source])
        self.sm.on_speech(3.5)
        self.assertEqual(self.sm.state, 'live')
        return self.sm.current()

    def test_source_without_speech_does_not_start(self):
        self.sm.on_sources([discord()])
        self.sm.on_speech(1.0)
        self.assertEqual(self.sm.state, 'idle')
        self.assertEqual(self.captures, [])
        self.sm.on_sources([])          # the ping ended
        self.sm.on_sources([discord()])
        self.sm.on_speech(1.0)          # speech counts from this appearance only
        self.assertEqual(self.sm.state, 'idle')
        self.sm.on_speech(2.5)
        self.assertEqual(self.sm.state, 'live')
        self.assertEqual(self.captures, [('start', self.sm.current()['id'])])

    def test_auto_start_names_and_saves_the_meeting(self):
        m = self.live()
        self.assertEqual(m['title'], 'Discord')
        self.assertEqual(m['source']['app'], 'discord.exe')
        self.assertTrue(self.store.path(m['id']).exists())
        self.assertEqual(self.events[-1][0], 'meeting')
        self.assertEqual(self.events[-1][1]['state'], 'live')

    def test_ask_mode_offers_then_starts_on_accept(self):
        self.sm.on_sources([discord(mode='ask')])
        self.sm.on_speech(4.0)
        self.assertEqual(self.sm.state, 'offered')
        self.assertEqual(self.captures, [])
        self.sm.offer_accept()
        self.assertEqual(self.sm.state, 'live')

    def test_declined_offer_is_not_asked_again_until_source_goes_away(self):
        self.sm.on_sources([discord(mode='ask')])
        self.sm.on_speech(4.0)
        self.sm.offer_decline()
        self.assertEqual(self.sm.state, 'idle')
        self.sm.on_sources([discord(mode='ask')])
        self.sm.on_speech(4.0)
        self.assertEqual(self.sm.state, 'idle')
        self.sm.on_sources([])
        self.sm.on_sources([discord(mode='ask')])
        self.sm.on_speech(4.0)
        self.assertEqual(self.sm.state, 'offered')

    def test_source_gone_pauses_and_same_source_resumes(self):
        m = self.live()
        self.sm.add_turn({'t0': started_at(m) + 5, 't1': started_at(m) + 8, 'stream': 'loopback', 'speaker': 's1',
                          'text': 'hi', 'lang': 'en'})
        self.sm.on_sources([])
        self.assertEqual(self.sm.state, 'paused')
        self.assertEqual(self.captures[-1], ('stop',))
        self.clock.advance(120)
        self.sm.on_sources([discord()])
        self.assertEqual(self.sm.state, 'live')
        self.assertEqual(self.sm.current()['id'], m['id'])
        self.assertEqual(self.sm.state_payload()['notice'], 'resumed')
        self.assertEqual(self.sm.current()['gaps'], [{'start': 0.0, 'end': 120.0, 'reason': 'paused'}])

    def test_source_back_after_gap_starts_new_meeting(self):
        m = self.live()
        self.sm.on_sources([])
        self.clock.advance(10 * 60 + 1)
        self.assertEqual(self.sm.state, 'idle')
        saved = self.store.load(m['id'])
        self.assertIsNotNone(saved['ended_at'])
        self.clock.advance(5)
        self.sm.on_sources([discord()])
        self.sm.on_speech(4.0)
        self.assertEqual(self.sm.state, 'live')
        self.assertNotEqual(self.sm.current()['id'], m['id'])
        self.assertEqual(self.sm.state_payload()['notice'], 'new')

    def test_different_code_within_gap_starts_new_and_can_continue_previous(self):
        m = self.live(meet('abc-defg-hij'))
        self.sm.on_sources([])
        self.clock.advance(60)
        self.sm.on_sources([meet('zzz-zzzz-zzz')])
        self.sm.on_speech(4.0)
        self.assertEqual(self.sm.state, 'live')
        self.assertNotEqual(self.sm.current()['id'], m['id'])
        self.assertEqual(self.sm.state_payload()['notice'], 'new')
        self.assertTrue(self.sm.state_payload()['can_continue'])
        base = started_at(self.sm.current())
        self.sm.add_turn({'t0': base + 1, 't1': base + 2, 'stream': 'mic', 'speaker': 'you', 'text': 'late', 'lang': 'en'})
        new_id = self.sm.current()['id']
        self.sm.continue_previous()
        self.assertEqual(self.sm.current()['id'], m['id'])
        self.assertEqual(self.sm.current()['turns'][-1]['text'], 'late')
        self.assertFalse(self.store.path(new_id).exists())

    def test_same_code_resumes_even_from_another_browser_window(self):
        m = self.live(meet('abc-defg-hij'))
        self.sm.on_sources([])
        self.clock.advance(30)
        self.sm.on_sources([meet('abc-defg-hij')])
        self.assertEqual(self.sm.current()['id'], m['id'])

    def test_resumed_meeting_can_be_split_into_a_new_one(self):
        m = self.live()
        base = started_at(m)
        self.sm.add_turn({'t0': base + 1, 't1': base + 2, 'stream': 'mic', 'speaker': 'you', 'text': 'first', 'lang': 'en'})
        self.sm.on_sources([])
        self.clock.advance(60)
        self.sm.on_sources([discord()])
        self.sm.add_turn({'t0': base + 70, 't1': base + 72, 'stream': 'mic', 'speaker': 'you', 'text': 'second', 'lang': 'en'})
        self.sm.split_resumed()
        self.assertNotEqual(self.sm.current()['id'], m['id'])
        self.assertEqual([t['text'] for t in self.sm.current()['turns']], ['second'])
        self.assertEqual([t['text'] for t in self.store.load(m['id'])['turns']], ['first'])
        self.assertIsNotNone(self.store.load(m['id'])['ended_at'])

    def test_manual_start_without_source_and_end(self):
        self.sm.start()
        self.assertEqual(self.sm.state, 'live')
        self.assertEqual(self.sm.current()['source']['kind'], 'manual')
        self.assertEqual(self.sm.current()['title'], 'Meeting')
        self.sm.end()
        self.assertEqual(self.sm.state, 'idle')
        self.assertEqual(self.captures, [('start', self.store.list()[0]['id']), ('stop',)])
        self.assertEqual(self.events[-2][1]['state'], 'ended')
        self.assertEqual(self.events[-1][1]['state'], 'idle')

    def test_new_ends_current_and_starts_fresh_with_same_source(self):
        m = self.live()
        self.sm.new()
        self.assertEqual(self.sm.state, 'live')
        self.assertNotEqual(self.sm.current()['id'], m['id'])
        self.assertEqual(self.sm.current()['source'], m['source'])
        self.assertIsNotNone(self.store.load(m['id'])['ended_at'])

    def test_pause_and_exclude_record_gaps(self):
        m = self.live()
        self.sm.pause()
        self.assertEqual(self.sm.state, 'paused')
        self.assertTrue(self.sm.state_payload()['paused_by_user'])
        self.clock.advance(10)
        self.sm.resume()
        self.assertEqual(self.sm.state, 'live')
        self.clock.advance(5)
        self.sm.set_excluded(True)
        self.clock.advance(20)
        self.sm.set_excluded(False)
        self.assertEqual(m['gaps'], [{'start': 0.0, 'end': 10.0, 'reason': 'paused'},
                                     {'start': 15.0, 'end': 35.0, 'reason': 'excluded'}])

    def test_user_pause_is_not_resumed_by_the_source(self):
        self.live()
        self.sm.pause()
        self.sm.on_sources([discord()])
        self.assertEqual(self.sm.state, 'paused')

    def test_turns_speakers_and_rename(self):
        m = self.live()
        base = started_at(m)
        self.sm.ensure_speaker({'id': 's1', 'name': 'Speaker 1', 'colour': 1, 'centroid': [1.0], 'count': 1})
        self.sm.add_turn({'t0': base + 3.25, 't1': base + 7.9, 'stream': 'loopback', 'speaker': 's1', 'text': 'hi', 'lang': 'en'})
        self.assertEqual(m['turns'], [{'start': 3.25, 'end': 7.9, 'stream': 'loopback', 'speaker': 's1', 'text': 'hi', 'lang': 'en'}])
        self.assertEqual(self.events[-1], ('meeting_turn', {'id': m['id'], 'turn': m['turns'][0]}))
        self.sm.rename_speaker('s1', 'Ilona')
        self.assertEqual(m['speakers'][1]['name'], 'Ilona')
        self.assertEqual(self.sm.state_payload()['speakers'][1], {'id': 's1', 'name': 'Ilona', 'colour': 1})

    def test_payload_shape(self):
        p = self.sm.state_payload()
        self.assertEqual(p['state'], 'idle')
        self.live()
        self.sm.set_behind(12.5)
        p = self.sm.state_payload()
        for key in ('state', 'id', 'title', 'source', 'started_at', 'elapsed_s', 'paused_by_user', 'excluded', 'behind_s',
                    'speakers', 'notice', 'turns', 'can_continue', 'can_split', 'offer'):
            self.assertIn(key, p)
        self.assertEqual(p['behind_s'], 12.5)


RULES = [
    {'id': 'discord', 'match': ['discord.exe'], 'meeting': 'default'},
    {'id': 'zoom', 'match': ['zoom.exe'], 'meeting': 'ask'},
    {'id': 'meet', 'match': ['chrome.exe', 'msedge.exe', 'brave.exe'], 'window_title': ['Meet - '], 'meeting': 'default'},
    {'id': 'teams-web', 'match': ['chrome.exe', 'msedge.exe'], 'window_title': ['Microsoft Teams'], 'meeting': 'off'},
    {'id': 'terminals', 'match': ['cmd.exe', 'WindowsTerminal.exe'], 'auto_send': False},
]


class DetectorTests(unittest.TestCase):
    def test_native_app_with_active_session_is_a_source(self):
        from koli.meeting.detector import find_sources
        sources = find_sources([{'pid': 1, 'exe': 'discord.exe', 'state': 1}], [], RULES, 'auto')
        self.assertEqual(sources, [{'key': 'discord.exe', 'app': 'discord.exe', 'kind': 'native', 'title': '', 'code': '',
                                    'mode': 'auto'}])

    def test_inactive_session_and_unlisted_app_are_ignored(self):
        from koli.meeting.detector import find_sources
        sessions = [{'pid': 1, 'exe': 'discord.exe', 'state': 0}, {'pid': 2, 'exe': 'spotify.exe', 'state': 1},
                    {'pid': 3, 'exe': 'cmd.exe', 'state': 1}]
        self.assertEqual(find_sources(sessions, [], RULES, 'auto'), [])

    def test_rule_mode_wins_over_default_and_off_drops(self):
        from koli.meeting.detector import find_sources
        sessions = [{'pid': 1, 'exe': 'zoom.exe', 'state': 1}, {'pid': 2, 'exe': 'chrome.exe', 'state': 1}]
        tabs = [{'exe': 'chrome.exe', 'title': 'Microsoft Teams - Audio playing', 'media': 'Audio playing', 'hwnd': 9}]
        sources = find_sources(sessions, tabs, RULES, 'auto')
        self.assertEqual([(s['app'], s['mode']) for s in sources], [('zoom.exe', 'ask')])

    def test_browser_tab_behind_others_with_code(self):
        from koli.meeting.detector import find_sources
        sessions = [{'pid': 2, 'exe': 'chrome.exe', 'state': 1}]
        tabs = [{'exe': 'chrome.exe', 'title': 'Inbox - Gmail', 'media': '', 'hwnd': 9},
                {'exe': 'chrome.exe', 'title': 'Meet - abc-defg-hij', 'media': 'Camera and microphone in use', 'hwnd': 9}]
        sources = find_sources(sessions, tabs, RULES, 'auto')
        self.assertEqual(sources, [{'key': 'chrome.exe|Meet - abc-defg-hij', 'app': 'chrome.exe', 'kind': 'browser',
                                    'title': 'Meet - abc-defg-hij', 'code': 'abc-defg-hij', 'mode': 'auto'}])

    def test_browser_tab_without_audio_session_is_not_a_source(self):
        from koli.meeting.detector import find_sources
        tabs = [{'exe': 'chrome.exe', 'title': 'Meet - abc-defg-hij', 'media': '', 'hwnd': 9}]
        self.assertEqual(find_sources([{'pid': 2, 'exe': 'chrome.exe', 'state': 0}], tabs, RULES, 'auto'), [])

    def test_strip_media_suffix(self):
        from koli.meeting.detector import strip_media
        self.assertEqual(strip_media('Meet - abc-defg-hij - Microphone in use'), ('Meet - abc-defg-hij', 'Microphone in use'))
        self.assertEqual(strip_media('Inbox - Gmail'), ('Inbox - Gmail', ''))
        self.assertEqual(strip_media('Tab - Audio muted'), ('Tab', 'Audio muted'))

    def test_detector_tick_reports_every_time(self):
        from koli.meeting.detector import MeetingDetector
        seen = []
        d = MeetingDetector(on_sources=seen.append, rules_provider=lambda: RULES, mode_provider=lambda: 'auto',
                            sessions_fn=lambda: [{'pid': 1, 'exe': 'discord.exe', 'state': 1}], tabs_fn=lambda: [])
        d.tick()
        d.tick()
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0][0]['app'], 'discord.exe')

    def test_detector_survives_probe_errors(self):
        from koli.meeting.detector import MeetingDetector
        seen = []

        def boom():
            raise RuntimeError('COM failed')
        d = MeetingDetector(on_sources=seen.append, rules_provider=lambda: RULES, mode_provider=lambda: 'auto',
                            sessions_fn=boom, tabs_fn=lambda: [])
        d.tick()
        self.assertEqual(seen, [[]])

    # Loaded by path: the macOS package itself needs fcntl, which Windows lacks.
    def test_platform_stubs_import_everywhere(self):
        import importlib.util
        for name, fn in (('audio_sessions', 'active'), ('browser_tabs', 'tabs')):
            path = ROOT / 'src' / 'koli' / 'platform' / 'macos' / f'{name}.py'
            spec = importlib.util.spec_from_file_location(f'stub_{name}', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertEqual(getattr(module, fn)(), [])


class CaptureTests(unittest.TestCase):
    # A fake VAD: a frame is speech when its mean absolute level is above 0.1.
    @staticmethod
    def prob(frame):
        return 0.9 if float(np.abs(frame).mean()) > 0.1 else 0.05

    def make(self, **kw):
        from koli.meeting.capture import Chunker
        self.chunks = []
        self.speech = []
        return Chunker('loopback', self.prob, self.chunks.append, on_speech=self.speech.append, **kw)

    @staticmethod
    def seconds(level, s, rate=16000):
        return np.full(int(s * rate), level, dtype=np.float32)

    def test_cuts_at_a_pause_and_keeps_wall_clock_times(self):
        c = self.make()
        t = 1000.0
        for block in (self.seconds(0.5, 1.0), self.seconds(0.0, 0.8)):
            t += len(block) / 16000
            c.feed(block, t)
        self.assertEqual(len(self.chunks), 1)
        ch = self.chunks[0]
        self.assertEqual(ch['stream'], 'loopback')
        self.assertAlmostEqual(ch['t0'], 1000.0, places=2)
        self.assertLessEqual(ch['t1'], 1001.5)       # trailing silence trimmed
        self.assertGreaterEqual(ch['t1'], 1001.0)
        self.assertAlmostEqual(ch['speech_s'], 1.0, places=1)
        self.assertGreater(sum(self.speech), 0.9)     # on_speech saw about a second

    def test_cuts_at_max_length_while_still_speaking(self):
        c = self.make(max_s=2.0)
        t = 0.0
        for _ in range(5):
            block = self.seconds(0.5, 0.5)
            t += 0.5
            c.feed(block, t)
        self.assertEqual(len(self.chunks), 1)
        self.assertAlmostEqual(len(self.chunks[0]['audio']) / 16000, 2.0, places=1)

    def test_drops_too_short_speech_and_pure_silence(self):
        c = self.make()
        c.feed(self.seconds(0.5, 0.1), 0.1)
        c.feed(self.seconds(0.0, 1.0), 1.1)
        c.feed(self.seconds(0.0, 3.0), 4.1)
        self.assertEqual(self.chunks, [])

    def test_flush_emits_the_open_chunk(self):
        c = self.make()
        c.feed(self.seconds(0.5, 0.7), 0.7)
        self.assertEqual(self.chunks, [])
        c.flush()
        self.assertEqual(len(self.chunks), 1)

    def test_loopback_stream_reports_after_retries(self):
        from koli.meeting.capture import LoopbackStream
        errors = []
        attempts = []

        def open_fn():
            attempts.append(1)
            raise RuntimeError('no loopback device')
        s = LoopbackStream(on_block=lambda a, t: None, on_error=errors.append, retries=2, open_fn=open_fn, retry_wait=0.0)
        s.start()
        s._thread.join(2.0)
        self.assertEqual(len(attempts), 3)
        self.assertIn('no loopback device', errors[0])

    def test_mic_tap_respects_allow(self):
        from koli.meeting.capture import MicTap

        class Recorder:
            def __init__(self):
                self.taps = []

            def add_tap(self, fn):
                self.taps.append(fn)

            def remove_tap(self, fn):
                self.taps.remove(fn)
        rec = Recorder()
        got = []
        allowed = {'ok': True}
        tap = MicTap(lambda: rec, lambda audio, when: got.append(audio), lambda: allowed['ok'])
        tap.start()
        rec.taps[0](np.zeros(160, dtype=np.float32), 1.0)
        allowed['ok'] = False
        rec.taps[0](np.zeros(160, dtype=np.float32), 2.0)
        self.assertEqual(len(got), 1)
        tap.stop()
        self.assertEqual(rec.taps, [])

    def test_recorder_calls_taps_with_16k_mono(self):
        from koli.audio_recorder import AudioRecorder
        rec = AudioRecorder.__new__(AudioRecorder)
        rec._taps = []
        rec._recording_rate = 48000
        rec._needs_resampling_cached = True
        rec.logger = logging.getLogger('test')
        got = []
        rec.add_tap(lambda audio, when: got.append(audio))
        rec._notify_taps(np.zeros((4800, 1), dtype=np.float32))
        self.assertEqual(got[0].shape, (1600,))
        self.assertEqual(got[0].dtype, np.float32)


class SpeakerTests(unittest.TestCase):
    @staticmethod
    def unit(*xs):
        v = np.array(xs, dtype=np.float32)
        return v / np.linalg.norm(v)

    def test_same_voice_joins_far_voice_is_new(self):
        from koli.meeting.speakers import SpeakerTracker
        vecs = {'a': self.unit(1, 0, 0), 'a2': self.unit(0.95, 0.1, 0), 'b': self.unit(0, 1, 0)}
        t = SpeakerTracker(embed=lambda audio: vecs[audio])
        sid, spk = t.label('a')
        self.assertEqual(sid, 's1')
        self.assertEqual(spk['name'], 'Speaker 1')
        self.assertEqual(spk['colour'], 1)
        sid2, spk2 = t.label('a2')
        self.assertEqual(sid2, 's1')
        self.assertEqual(spk2['count'], 2)
        sid3, spk3 = t.label('b')
        self.assertEqual(sid3, 's2')
        self.assertEqual(spk3['colour'], 2)

    def test_centroid_moves_toward_new_samples(self):
        from koli.meeting.speakers import SpeakerTracker
        vecs = {'a': self.unit(1, 0), 'a2': self.unit(0.8, 0.6)}
        t = SpeakerTracker(embed=lambda audio: vecs[audio], similarity=0.5)
        t.label('a')
        _, spk = t.label('a2')
        c = np.array(spk['centroid'])
        self.assertGreater(c[1], 0.2)
        self.assertAlmostEqual(float(np.linalg.norm(c)), 1.0, places=4)

    def test_load_restores_speakers_and_numbering(self):
        from koli.meeting.speakers import SpeakerTracker
        t = SpeakerTracker(embed=lambda audio: self.unit(0, 0, 1))
        t.load([{'id': 'you', 'name': 'You', 'colour': 0},
                {'id': 's1', 'name': 'Ilona', 'colour': 1, 'centroid': [1.0, 0.0, 0.0], 'count': 4},
                {'id': 's2', 'name': 'Speaker 2', 'colour': 2, 'centroid': [0.0, 1.0, 0.0], 'count': 2}])
        sid, spk = t.label('x')
        self.assertEqual(sid, 's3')
        self.assertEqual(spk['colour'], 3)
        self.assertEqual([s['id'] for s in t.export()], ['s1', 's2', 's3'])

    def test_no_embedder_means_unknown(self):
        from koli.meeting.speakers import SpeakerTracker
        t = SpeakerTracker(embed=None)
        self.assertEqual(t.label(np.zeros(16000)), (SpeakerTracker.UNKNOWN, None))
        t2 = SpeakerTracker(embed=lambda audio: None)
        self.assertEqual(t2.label(np.zeros(16000)), (SpeakerTracker.UNKNOWN, None))

    def test_colours_wrap_after_seven(self):
        from koli.meeting.speakers import SpeakerTracker
        t = SpeakerTracker(embed=lambda audio: audio, similarity=0.99)
        labels = []
        for i in range(9):
            v = np.zeros(9, dtype=np.float32)
            v[i] = 1.0
            labels.append(t.label(v))
        self.assertEqual([spk['colour'] for _, spk in labels][:8], [1, 2, 3, 4, 5, 6, 7, 1])

    def test_registry_knows_speaker_models(self):
        from koli.model_registry import ModelRegistry
        r = ModelRegistry(speaker_models_config={'eres2net': {
            'source': 'csukuangfj/speaker-embedding-models', 'label': 'ERes2Net',
            'files': {'model': '3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx'}}})
        self.assertIn('eres2net', r.speaker_models)
        self.assertFalse(r.is_speaker_model_cached('nope'))


class FakeEngine:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []
        self.last_info = {}

    def transcribe_audio(self, audio, language=None):
        self.calls.append((len(audio), language))
        text = self.answers.pop(0) if self.answers else None
        self.last_info = {'language': 'he' if text and 'שלום' in text else 'en'}
        return text


class TranscriberTests(unittest.TestCase):
    def run_chunks(self, chunks, answers, busy=None, tracker=None, language='auto'):
        from koli.meeting.transcriber import MeetingTranscriber
        from koli.meeting.speakers import SpeakerTracker
        engine = FakeEngine(answers)
        self.turns, self.speakers, self.pending, self.behind = [], [], [], []
        busy_state = busy or {'busy': False}
        t = MeetingTranscriber(engine_provider=lambda: engine, is_busy=lambda: busy_state['busy'],
                               tracker=tracker or SpeakerTracker(embed=lambda a: np.array([1.0, 0.0])),
                               language_provider=lambda: language,
                               on_turn=self.turns.append, on_speaker=self.speakers.append,
                               on_pending=self.pending.append, on_behind=self.behind.append, poll_s=0.01)
        t.start()
        for c in chunks:
            t.submit(c)
        deadline = time.time() + 3
        while t.queued_seconds() > 0 or t.working:
            if time.time() > deadline:
                break
            time.sleep(0.01)
        t.stop()
        return engine

    @staticmethod
    def chunk(stream, seconds=1.0, t0=100.0):
        return {'audio': np.zeros(int(16000 * seconds), dtype=np.float32), 'stream': stream, 't0': t0,
                't1': t0 + seconds, 'speech_s': seconds}

    def test_mic_turn_is_you_and_loopback_turn_is_labelled(self):
        engine = self.run_chunks([self.chunk('mic'), self.chunk('loopback', t0=105)], ['hello', 'שלום'])
        self.assertEqual([(t['speaker'], t['text'], t['lang']) for t in self.turns],
                         [('you', 'hello', 'en'), ('s1', 'שלום', 'he')])
        self.assertEqual(self.speakers[0]['id'], 's1')
        self.assertEqual(engine.calls[0][1], 'auto')
        self.assertEqual(self.pending[0], 'mic')
        self.assertIsNone(self.pending[-1])

    def test_empty_transcription_makes_no_turn(self):
        self.run_chunks([self.chunk('loopback')], [None])
        self.assertEqual(self.turns, [])
        self.assertEqual(self.speakers, [])
        self.assertIsNone(self.pending[-1])

    def test_waits_while_dictation_is_busy(self):
        busy = {'busy': True}
        from koli.meeting.transcriber import MeetingTranscriber
        from koli.meeting.speakers import SpeakerTracker
        engine = FakeEngine(['late'])
        turns = []
        t = MeetingTranscriber(engine_provider=lambda: engine, is_busy=lambda: busy['busy'], tracker=SpeakerTracker(),
                               language_provider=lambda: 'en', on_turn=turns.append, on_speaker=lambda s: None,
                               on_pending=lambda s: None, on_behind=lambda s: None, poll_s=0.01)
        t.start()
        t.submit(self.chunk('mic'))
        time.sleep(0.1)
        self.assertEqual(engine.calls, [])
        busy['busy'] = False
        time.sleep(0.3)
        t.stop()
        self.assertEqual([x['text'] for x in turns], ['late'])
        self.assertEqual(engine.calls[0][1], 'en')

    def test_behind_reports_queued_seconds(self):
        self.run_chunks([self.chunk('mic', 2.0), self.chunk('mic', 3.0)], ['a', 'b'])
        self.assertIn(5.0, self.behind)
        self.assertEqual(self.behind[-1], 0.0)

    def test_unknown_speaker_when_no_model(self):
        from koli.meeting.speakers import SpeakerTracker
        self.run_chunks([self.chunk('loopback')], ['x'], tracker=SpeakerTracker(embed=None))
        self.assertEqual(self.turns[0]['speaker'], '?')


class EngineLanguageTests(unittest.TestCase):
    def test_route_honours_a_per_call_language(self):
        from koli.whisper_engine import WhisperEngine
        e = WhisperEngine.__new__(WhisperEngine)
        e.model, e.model_key, e.language, e.initial_prompt = 'M', 'small', 'en', None
        e.language_model = None
        self.assertEqual(e._route(np.zeros(10))[2], 'en')
        self.assertEqual(e._route(np.zeros(10), language='he')[2], 'he')
        self.assertIsNone(e._route(np.zeros(10), language='auto')[2])


class MeetingSettingsTests(unittest.TestCase):
    def defaults(self):
        import yaml
        from koli.config_manager import _resolve_platform_values
        return _resolve_platform_values(yaml.safe_load(
            (ROOT / 'src' / 'koli' / 'config.defaults.yaml').read_text(encoding='utf-8')))

    def test_defaults_match_the_spec(self):
        d = self.defaults()['meeting']
        self.assertTrue(d['enabled'])
        self.assertEqual(d['detect'], 'auto')
        self.assertTrue(d['include_me'])
        self.assertEqual(d['gap_minutes'], 10)
        self.assertEqual(d['start_speech_seconds'], 3)
        self.assertEqual(d['language'], 'auto')
        self.assertEqual(d['model'], '')
        self.assertEqual(d['speaker_model'], 'eres2net')
        self.assertIn('eres2net', d['speaker_models'])
        self.assertEqual(d['panel']['opacity'], 85)
        self.assertEqual(d['panel']['position'], 'right-edge')
        self.assertEqual((d['panel']['width'], d['panel']['height']), (380, 420))
        self.assertTrue(d['panel']['hide_from_sharing'])
        self.assertFalse(self.defaults()['overlay']['hide_from_sharing'])
        self.assertEqual(self.defaults()['hotkey']['meeting_hotkey'], '')

    def test_schema_section_and_simple_question(self):
        from koli.settings import schema, simple_mode
        self.assertIn('meetings', [s['id'] for s in schema.SECTIONS])
        self.assertEqual(schema.get('meeting.detect').choices,
                         [['auto', 'Automatically'], ['ask', 'Ask me first'], ['off', 'Never']])
        self.assertEqual(schema.get('meeting.panel.custom_x').mode, schema.HIDDEN)
        self.assertEqual(simple_mode.to_patch('calls', 'ask'), {'meeting.detect': 'ask'})
        self.assertEqual(simple_mode.from_settings(lambda p: {'meeting.detect': 'off'}.get(p))['calls'], 'off')

    def test_appliers_route_meeting_keys(self):
        from koli.settings.appliers import _PREFIX_HANDLERS, _LISTENER_KEYS, Appliers
        self.assertIn('hotkey.meeting_hotkey', _LISTENER_KEYS)
        self.assertTrue(any(prefix == 'meeting.' and fn is Appliers.meeting for prefix, fn in _PREFIX_HANDLERS))


class FakeStateManager:
    def __init__(self):
        self.recording = False
        self.processing = False
        self.whisper_engine = FakeEngine(['hello'])
        self.app_rules = type('R', (), {'rules': RULES})()
        self.audio_recorder = type('Rec', (), {'add_tap': lambda s, f: None, 'remove_tap': lambda s, f: None})()

    def is_idle(self):
        return not (self.recording or self.processing)

    def is_transcription_recording(self):
        return self.recording


class FakeConfig:
    def __init__(self, values):
        self.values = values

    def get_path(self, path, default=None):
        return self.values.get(path, default)


class ServiceTests(unittest.TestCase):
    def make(self, enabled=True):
        from koli.services.meetings import MeetingService
        from koli.meeting.store import MeetingStore
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        values = {'meeting.enabled': enabled, 'meeting.detect': 'auto', 'meeting.include_me': True,
                  'meeting.gap_minutes': 10, 'meeting.start_speech_seconds': 3, 'meeting.language': 'auto',
                  'meeting.model': '', 'meeting.speaker_model': 'eres2net'}
        ctx = type('Ctx', (), {})()
        ctx.state_manager = FakeStateManager()
        ctx.config_manager = FakeConfig(values)
        ctx.model_registry = None
        self.events = []
        fake_loopback = type('L', (), {'start': lambda s: None, 'stop': lambda s: None})
        svc = MeetingService(ctx, store=MeetingStore(Path(self.tmp.name)),
                             publish=lambda t, p: self.events.append((t, p)),
                             loopback_factory=lambda on_block, on_error: fake_loopback(),
                             embedder_factory=lambda: None, loopback_available=lambda: True)
        self.addCleanup(svc.shutdown)
        return svc

    def test_manual_start_and_end_round_trip(self):
        svc = self.make()
        self.assertEqual(svc.state()['state'], 'idle')
        svc.start()
        self.assertEqual(svc.state()['state'], 'live')
        svc.end()
        self.assertEqual(svc.state()['state'], 'idle')
        heads = svc.list()
        self.assertEqual(len(heads), 1)
        self.assertEqual(svc.get(heads[0]['id'])['turns'], [])
        self.assertIn('Meeting', svc.export(heads[0]['id'], 'txt'))
        self.assertTrue(svc.delete(heads[0]['id']))

    def test_toggle_starts_then_ends(self):
        svc = self.make()
        svc.toggle()
        self.assertEqual(svc.state()['state'], 'live')
        svc.toggle()
        self.assertEqual(svc.state()['state'], 'idle')

    def test_detector_runs_only_when_enabled(self):
        svc = self.make(enabled=False)
        self.assertIsNone(svc.detector)
        svc.apply_setting('meeting.enabled', True)
        self.assertIsNotNone(svc.detector)
        svc.apply_setting('meeting.enabled', False)
        self.assertIsNone(svc.detector)

    def test_mic_blocks_are_dropped_while_dictating_or_excluded(self):
        svc = self.make()
        svc.start()
        self.assertTrue(svc._mic_allowed())
        svc.ctx.state_manager.recording = True
        self.assertFalse(svc._mic_allowed())
        svc.ctx.state_manager.recording = False
        svc.set_excluded(True)
        self.assertFalse(svc._mic_allowed())
        svc.set_excluded(False)
        svc.ctx.config_manager.values['meeting.include_me'] = False
        self.assertFalse(svc._mic_allowed())

    def test_events_topics_registered(self):
        from koli.ui.events import TOPICS
        self.assertIn('meeting', TOPICS)
        self.assertIn('meeting_turn', TOPICS)

    def test_loopback_extra_is_installable(self):
        from koli.services.system import EXTRAS
        self.assertEqual(EXTRAS['loopback'][0], 'soundcard')


class PanelPlacementTests(unittest.TestCase):
    def test_panel_docks_right_lower_half_and_clamps(self):
        from koli.ui.floating_bar import panel_origin
        area = (0, 0, 1920, 1040)
        x, y = panel_origin(area, (380, 420), 1.0, 'right-edge', None)
        self.assertEqual(x, 1920 - 380 - 6)
        self.assertGreater(y, 1040 // 2 - 1)
        self.assertLessEqual(y + 420, 1040 - 6)
        x, y = panel_origin(area, (380, 420), 1.0, 'custom', (1900, 1000))
        self.assertEqual((x, y), (1920 - 380, 1040 - 420))
        x, y = panel_origin(area, (380, 420), 1.0, 'top-left', None)
        self.assertEqual((x, y), (6, 6))


class FakeLoop:
    # A loopback stream the test drives by hand: blocks go in through on_block.
    made = []

    def __init__(self, on_block, on_error):
        self.on_block = on_block
        self.on_error = on_error
        self.started = False
        self.stopped = False
        FakeLoop.made.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class ReviewFixTests(unittest.TestCase):
    # The chunker's callbacks run outside its lock: one that re-enters the
    # chunker (or takes another lock) must not deadlock the audio thread.
    def test_chunker_callbacks_run_outside_its_lock(self):
        import threading
        from koli.meeting.capture import Chunker
        chunks = []
        holder = {}

        def on_chunk(chunk):
            chunks.append(chunk)
            holder['c'].flush()
        c = Chunker('mic', lambda f: 0.9 if float(np.abs(f).mean()) > 0.1 else 0.0, on_chunk)
        holder['c'] = c

        def run():
            c.feed(np.full(16000, 0.5, dtype=np.float32), 1.0)
            c.feed(np.zeros(16000, dtype=np.float32), 2.0)
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(2.0)
        self.assertFalse(t.is_alive(), 'feed deadlocked on its own lock')
        self.assertEqual(len(chunks), 1)

    # The session calls start/stop capture outside its lock, so a capture
    # callback (or anything it triggers on another thread) can read the state.
    def test_session_capture_callbacks_run_outside_its_lock(self):
        import threading
        from koli.meeting.session import SessionManager
        from koli.meeting.store import MeetingStore
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        seen = []
        holder = {}

        def start_capture(m):
            t = threading.Thread(target=lambda: seen.append(holder['sm'].state_payload()['state']), daemon=True)
            t.start()
            t.join(1.0)
            seen.append('alive' if t.is_alive() else 'finished')
        clock = FakeClock(at(30))
        sm = SessionManager(MeetingStore(Path(tmp.name)), start_capture=start_capture, stop_capture=lambda: None,
                            publish=lambda t, p: None, settings=lambda p, d=None: d, now=clock.now, schedule=clock.schedule)
        holder['sm'] = sm
        sm.start()
        self.assertEqual(seen, ['live', 'finished'])

    def _session(self):
        from koli.meeting.session import SessionManager
        from koli.meeting.store import MeetingStore
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = MeetingStore(Path(tmp.name))
        clock = FakeClock(at(30))
        self.reloads = []
        sm = SessionManager(store, start_capture=lambda m: None, stop_capture=lambda: None, publish=lambda t, p: None,
                            settings=lambda p, d=None: {'meeting.gap_minutes': 10, 'meeting.start_speech_seconds': 3}.get(p, d),
                            now=clock.now, schedule=clock.schedule, on_speakers=self.reloads.append)
        return sm, store, clock

    # A chunk transcribed after its meeting ended still lands in that
    # meeting's file (nothing is dropped), never in the next meeting.
    def test_turn_of_an_ended_meeting_lands_in_its_file(self):
        sm, store, clock = self._session()
        sm.start()
        m = sm.current()
        base = started_at(m)
        sm.end()
        sm.add_turn({'meeting_id': m['id'], 't0': base + 5, 't1': base + 8, 'stream': 'mic', 'speaker': 'you',
                     'text': 'last words', 'lang': 'en'})
        saved = store.load(m['id'])
        self.assertEqual(saved['turns'][-1]['text'], 'last words')
        self.assertEqual(saved['turns'][-1]['start'], 5.0)

    def test_turn_of_the_old_meeting_never_lands_in_the_new_one(self):
        sm, store, clock = self._session()
        sm.start()
        m = sm.current()
        base = started_at(m)
        sm.new()
        sm.add_turn({'meeting_id': m['id'], 't0': base + 1, 't1': base + 2, 'stream': 'mic', 'speaker': 'you',
                     'text': 'old', 'lang': 'en'})
        self.assertEqual(sm.current()['turns'], [])
        self.assertEqual(store.load(m['id'])['turns'][-1]['text'], 'old')

    # A Meet tab sitting in the lobby must not start a meeting because YouTube
    # plays in another tab: when any tab of that browser carries a media
    # state, only tabs with one count.
    def test_lobby_tab_does_not_count_while_another_tab_plays(self):
        from koli.meeting.detector import find_sources
        sessions = [{'pid': 2, 'exe': 'chrome.exe', 'state': 1}]
        lobby = {'exe': 'chrome.exe', 'title': 'Meet - abc-defg-hij', 'media': '', 'hwnd': 9}
        youtube = {'exe': 'chrome.exe', 'title': 'Cat videos - YouTube', 'media': 'Audio playing', 'hwnd': 9}
        self.assertEqual(find_sources(sessions, [lobby, youtube], RULES, 'auto'), [])
        in_call = {**lobby, 'media': 'Microphone in use'}
        self.assertEqual([s['code'] for s in find_sources(sessions, [in_call, youtube], RULES, 'auto')], ['abc-defg-hij'])
        # A browser whose tabs report no media state at all still counts by title.
        self.assertEqual(len(find_sources(sessions, [lobby], RULES, 'auto')), 1)

    # Continuing the previous meeting (and splitting a resumed one) tells the
    # speaker tracker which speakers it is now working with.
    def test_speakers_reload_after_continue_previous_and_split(self):
        sm, store, clock = self._session()
        sm.on_sources([meet('abc-defg-hij')])
        sm.on_speech(4.0)
        sm.ensure_speaker({'id': 's1', 'name': 'Ilona', 'colour': 1, 'centroid': [1.0], 'count': 2})
        sm.on_sources([])
        clock.advance(60)
        sm.on_sources([meet('zzz-zzzz-zzz')])
        sm.on_speech(4.0)
        sm.continue_previous()
        self.assertEqual([s['name'] for s in self.reloads[-1]], ['You', 'Ilona'])
        sm.split_resumed()
        self.assertEqual([s['name'] for s in self.reloads[-1]], ['You', 'Ilona'])

    def test_meeting_events_are_not_coalesced(self):
        from koli.ui.host import COALESCE
        self.assertNotIn('meeting', COALESCE)

    def test_meeting_model_setting_is_hidden(self):
        from koli.settings import schema
        self.assertEqual(schema.get('meeting.model').mode, schema.HIDDEN)

    def _service(self, **kw):
        from koli.services.meetings import MeetingService
        from koli.meeting.store import MeetingStore
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        values = {'meeting.enabled': False, 'meeting.detect': 'auto', 'meeting.include_me': True,
                  'meeting.gap_minutes': 10, 'meeting.start_speech_seconds': 3, 'meeting.language': 'auto',
                  'meeting.model': '', 'meeting.speaker_model': 'eres2net'}
        ctx = type('Ctx', (), {})()
        ctx.state_manager = FakeStateManager()
        ctx.config_manager = FakeConfig(values)
        ctx.model_registry = None
        FakeLoop.made = []
        self.probs = []

        def prob_factory():
            self.probs.append(1)
            return lambda frame: 0.9
        args = dict(store=MeetingStore(Path(tmp.name)), publish=lambda t, p: None, loopback_factory=FakeLoop,
                    embedder_factory=lambda: None, speech_prob_factory=prob_factory, loopback_available=lambda: True)
        args.update(kw)
        svc = MeetingService(ctx, **args)
        self.addCleanup(svc.shutdown)
        return svc

    # Detection end to end: a source appears, the idle listener hears speech,
    # the meeting starts, and the meeting's own streams take over.
    def test_sources_then_speech_start_a_meeting_automatically(self):
        svc = self._service()
        svc._on_sources([discord()])
        self.assertEqual(len(FakeLoop.made), 1, 'an idle listener opens while a source waits for speech')
        idle = FakeLoop.made[0]
        for i in range(40):
            idle.on_block(np.full(1600, 0.5, dtype=np.float32), 100.0 + i * 0.1)
        self.assertEqual(svc.state()['state'], 'live')
        self.assertTrue(idle.stopped)
        self.assertEqual(len(FakeLoop.made), 2)
        self.assertTrue(FakeLoop.made[1].started)
        self.assertGreaterEqual(len(self.probs), 3, 'each stream gets its own voice detector')
        svc._on_sources([])
        self.assertEqual(svc.state()['state'], 'paused')
        self.assertTrue(FakeLoop.made[1].stopped)

    def test_idle_listener_closes_when_the_source_goes_away(self):
        svc = self._service()
        svc._on_sources([discord()])
        svc._on_sources([])
        self.assertTrue(FakeLoop.made[0].stopped)
        self.assertEqual(svc.state()['state'], 'idle')

    # Without the loopback package nothing starts, and the problem says why.
    def test_missing_loopback_package_refuses_to_start(self):
        problems = []
        svc = self._service(loopback_available=lambda: False, report=problems.append)
        svc.start()
        self.assertEqual(svc.state()['state'], 'idle')
        self.assertIn('loopback', problems[0].lower())
        svc.apply_setting('meeting.enabled', True)
        self.assertIsNone(svc.detector)

    # Ending a meeting keeps what is still queued; it drains into the file.
    def test_end_does_not_drop_queued_chunks(self):
        svc = self._service()
        svc.start()
        svc.ctx.state_manager.processing = True      # the engine is busy, so the chunk stays queued
        svc.transcriber.submit({'audio': np.zeros(16000, dtype=np.float32), 'stream': 'mic', 't0': 1.0, 't1': 2.0,
                                'speech_s': 1.0, 'meeting_id': svc.state()['id']})
        svc.end()
        self.assertGreater(svc.transcriber.queued_seconds(), 0)


if __name__ == '__main__':
    unittest.main()
