# tests/test_meet_extension.py
# The Koli Chrome extension for Google Meet, Koli's side: the feed of page
# events (joined, people, captions, left), the local HTTP door, speakers
# named by Meet, and how the meeting service uses them. No browser needed.
import datetime
import json
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tests.test_meeting import FakeClock, FakeConfig, FakeStateManager, at, meet  # noqa: E402

T0 = 1_800_000_000.0   # an epoch second for the feed's clock


def status(code='abc-defg-hij', joined=True, captions=True, people=None, title=''):
    return {'type': 'status', 'code': code, 'joined': joined, 'captions': captions, 'title': title,
            'people': people if people is not None else [{'id': 'p1', 'name': 'Dana', 'self': False},
                                                         {'id': 'p0', 'name': 'Avi', 'self': True}],
            'at_ms': T0 * 1000}


def caption(block, name, text, t0, t1, final=False, self_=False, code='abc-defg-hij'):
    return {'type': 'caption', 'code': code, 'id': block, 'name': name, 'self': self_, 'text': text,
            'first_ms': t0 * 1000, 'last_ms': t1 * 1000, 'final': final}


class FeedTests(unittest.TestCase):
    def setUp(self):
        from koli.meeting.meet_feed import MeetFeed
        self.now = T0
        self.feed = MeetFeed(now=lambda: self.now)

    def test_join_and_leave_are_reported_once(self):
        out = self.feed.apply([status()])
        self.assertEqual(out['joined'], ['abc-defg-hij'])
        self.assertEqual(self.feed.apply([status()])['joined'], [])
        self.assertEqual(self.feed.joined_calls()[0]['people'][0]['name'], 'Dana')
        out = self.feed.apply([{'type': 'left', 'code': 'abc-defg-hij'}])
        self.assertEqual(out['left'], ['abc-defg-hij'])
        self.assertEqual(self.feed.joined_calls(), [])

    def test_a_silent_tab_is_gone_after_the_heartbeat_timeout(self):
        from koli.meeting.meet_feed import HEARTBEAT_TIMEOUT_S
        self.feed.apply([status()])
        self.now += HEARTBEAT_TIMEOUT_S + 1
        self.assertEqual(self.feed.joined_calls(), [])

    def test_a_caption_block_is_handed_out_once_when_final(self):
        self.feed.apply([status()])
        self.assertEqual(self.feed.apply([caption('b1', 'Dana', 'Hello', T0 + 1, T0 + 2)])['finals'], [])
        out = self.feed.apply([caption('b1', 'Dana', 'Hello there', T0 + 1, T0 + 4, final=True)])
        self.assertEqual([(c['name'], c['text']) for c in out['finals']], [('Dana', 'Hello there')])
        again = self.feed.apply([caption('b1', 'Dana', 'Hello there', T0 + 1, T0 + 4, final=True)])
        self.assertEqual(again['finals'], [])

    def test_leaving_finishes_open_captions(self):
        self.feed.apply([status(), caption('b1', 'Dana', 'Bye', T0, T0 + 1)])
        out = self.feed.apply([{'type': 'left', 'code': 'abc-defg-hij'}])
        self.assertEqual([c['text'] for c in out['finals']], ['Bye'])

    def test_name_at_only_when_one_person_covers_the_chunk(self):
        self.feed.apply([status(), caption('b1', 'Dana', 'one', T0 + 10, T0 + 20),
                         caption('b2', 'Ron', 'two', T0 + 30, T0 + 40)])
        self.assertEqual(self.feed.name_at('abc-defg-hij', T0 + 12, T0 + 18), 'Dana')
        self.assertIsNone(self.feed.name_at('abc-defg-hij', T0 + 18, T0 + 32))   # both talked
        self.assertIsNone(self.feed.name_at('abc-defg-hij', T0 + 20.5, T0 + 28))  # nobody
        self.assertIsNone(self.feed.name_at('other-code-xyz', T0 + 12, T0 + 18))

    def test_your_own_captions_never_name_the_call_audio(self):
        self.feed.apply([status(), caption('b1', 'You', 'mine', T0, T0 + 10, self_=True)])
        self.assertIsNone(self.feed.name_at('abc-defg-hij', T0 + 2, T0 + 8))

    def test_people_change_is_reported(self):
        self.feed.apply([status()])
        out = self.feed.apply([status(people=[{'id': 'p1', 'name': 'Dana'}, {'id': 'p2', 'name': 'Ron'}])])
        self.assertEqual(out['changed'], ['abc-defg-hij'])


class LinkTests(unittest.TestCase):
    def setUp(self):
        from koli.meeting.meet_link import MeetLink
        self.got = []
        self.link = MeetLink(lambda events: self.got.extend(events) or {'state': 'live'},
                             lambda: {'state': 'idle'}, port=0)
        # Port 0: the OS picks a free one; read it back from the server.
        self.assertTrue(self.link.start())
        self.addCleanup(self.link.stop)
        self.port = self.link._httpd.server_address[1]

    def post(self, body: bytes, headers: dict):
        req = urllib.request.Request(f'http://127.0.0.1:{self.port}/meet/events', data=body, method='POST',
                                     headers={'Content-Type': 'application/json', **headers})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            return e.code, None

    def test_the_extension_posts_hebrew_captions(self):
        body = json.dumps({'events': [{'type': 'caption', 'text': 'שלום לכולם'}]}, ensure_ascii=False).encode('utf-8')
        code, reply = self.post(body, {'X-Koli-Extension': '1', 'Origin': 'chrome-extension://abc'})
        self.assertEqual(code, 200)
        self.assertEqual(reply['state'], 'live')
        self.assertEqual(self.got[0]['text'], 'שלום לכולם')

    def test_a_web_page_is_refused(self):
        body = b'{"events": []}'
        self.assertEqual(self.post(body, {'X-Koli-Extension': '1', 'Origin': 'https://evil.example'})[0], 404)
        self.assertEqual(self.post(body, {'Origin': 'chrome-extension://abc'})[0], 404)
        self.assertEqual(self.got, [])


class NamedSpeakerTests(unittest.TestCase):
    def tracker(self):
        from koli.meeting.speakers import SpeakerTracker
        vectors = {1.0: np.array([1.0, 0.0, 0.0]), 2.0: np.array([0.0, 1.0, 0.0])}
        return SpeakerTracker(embed=lambda audio: vectors.get(float(audio[0])))

    def test_named_speaker_has_no_voiceprint_until_taught(self):
        t = self.tracker()
        sid, entry = t.named('Dana')
        self.assertEqual(entry['name'], 'Dana')
        self.assertNotIn('centroid', entry)
        self.assertEqual(t.named('Dana'), (sid, None))
        self.assertIsNone(t.voiceprint(sid))
        taught = t.teach(sid, np.full(16000, 1.0, dtype=np.float32))
        self.assertEqual(taught['count'], 1)
        self.assertEqual(t.voiceprint(sid)[1], 1)

    def test_a_named_speaker_survives_a_resume_and_clustering(self):
        t = self.tracker()
        sid, entry = t.named('Dana')
        t.load([entry])
        self.assertEqual(t.named('Dana'), (sid, None))
        # Clustering a new voice never compares against a voiceprint that isn't there.
        other, _ = t.label(np.full(48000, 2.0, dtype=np.float32), speech_s=3.0)
        self.assertNotEqual(other, sid)


class SessionJoinTests(unittest.TestCase):
    def setUp(self):
        from koli.meeting.session import SessionManager
        from koli.meeting.store import MeetingStore
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.clock = FakeClock(at(30))
        self.settings = {'meeting.countdown_seconds': 10, 'meeting.start_speech_seconds': 3,
                         'meeting.gap_minutes': 10, 'meeting.language': 'auto'}
        self.sm = SessionManager(MeetingStore(Path(tmp.name)), start_capture=lambda m: None, stop_capture=lambda: None,
                                 publish=lambda t, p: None, settings=lambda p, d=None: self.settings.get(p, d),
                                 now=self.clock.now, schedule=self.clock.schedule)

    def test_joined_starts_at_once_without_speech_or_countdown(self):
        self.sm.on_sources([{**meet(), 'joined': True}])
        self.assertEqual(self.sm.state, 'live')

    def test_joined_in_ask_mode_offers(self):
        self.sm.on_sources([{**meet(mode='ask'), 'joined': True}])
        self.assertEqual(self.sm.state, 'offered')

    def test_leaving_ends_the_meeting(self):
        self.sm.on_sources([{**meet(), 'joined': True}])
        self.sm.call_left('abc-defg-hij')
        self.assertEqual(self.sm.state, 'idle')
        self.assertIsNone(self.sm.current())

    def test_a_stopped_meet_does_not_restart_while_still_joined(self):
        self.sm.on_sources([{**meet(), 'joined': True}])
        self.sm.end()
        self.sm.on_sources([{**meet(), 'joined': True}])
        self.assertEqual(self.sm.state, 'idle')


class ServiceMeetTests(unittest.TestCase):
    def make(self, mode='captions'):
        from koli.services.meetings import MeetingService
        from koli.meeting.store import MeetingStore
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ctx = type('Ctx', (), {})()
        ctx.state_manager = FakeStateManager()
        ctx.config_manager = FakeConfig({'meeting.enabled': False, 'meeting.include_me': True, 'meeting.language': 'auto',
                                         'meeting.detect': 'auto', 'meeting.google_meet': mode,
                                         'meeting.speaker_model': 'eres2net', 'meeting.segmentation_model': '',
                                         'meeting.start_speech_seconds': 3, 'meeting.countdown_seconds': 0})
        ctx.model_registry = None
        fake_loopback = type('L', (), {'start': lambda s: None, 'stop': lambda s: None})
        svc = MeetingService(ctx, store=MeetingStore(Path(tmp.name) / 'meetings'), publish=lambda t, p: None,
                             loopback_factory=lambda on_block, on_error, device=None: fake_loopback(),
                             embedder_factory=lambda: None, loopback_available=lambda: True, sessions_fn=lambda: [],
                             call_people_fn=lambda exe: {})
        self.addCleanup(svc.shutdown)
        return svc

    def join(self, svc, **kw):
        now = time.time()
        svc._meet_events([status(**kw)])
        svc._on_sources([])          # a detector tick with no browser source: the Meet still counts
        self.assertEqual(svc.session.state, 'live')
        return now

    def test_joining_starts_and_names_the_people(self):
        svc = self.make()
        self.join(svc, title='Weekly sync')
        m = svc.session.meeting
        self.assertEqual(m['source']['code'], 'abc-defg-hij')
        self.assertEqual(m['title'], 'Weekly sync')
        self.assertEqual([p['name'] for p in m['call']['people']], ['Dana', 'Avi'])

    def test_a_detector_source_of_the_same_meet_keeps_its_device(self):
        svc = self.make()
        svc._meet_events([status()])
        merged = svc._with_meet([{**meet(), 'device': 'Headset'}])
        self.assertEqual(len(merged), 1)
        self.assertEqual((merged[0]['key'], merged[0]['device'], merged[0]['joined']),
                         ('meet|abc-defg-hij', 'Headset', True))

    def test_captions_become_turns_with_meet_names(self):
        svc = self.make()
        now = self.join(svc)
        svc._meet_events([caption('b1', 'Dana', 'Hi everyone', now, now + 2, final=True),
                          caption('b2', 'You', 'Hi Dana', now + 3, now + 4, final=True, self_=True)])
        m = svc.session.meeting
        names = {s['id']: s['name'] for s in m['speakers']}
        self.assertEqual([(names[t['speaker']], t['text']) for t in m['turns']],
                         [('Dana', 'Hi everyone'), ('You', 'Hi Dana')])
        self.assertEqual(m['turns'][1]['stream'], 'mic')

    def test_names_only_mode_keeps_captions_out_of_the_transcript(self):
        svc = self.make(mode='names')
        now = self.join(svc)
        svc._meet_events([caption('b1', 'Dana', 'Hi', now, now + 2, final=True)])
        self.assertEqual(svc.session.meeting['turns'], [])
        self.assertFalse(svc._captions_are_transcript('abc-defg-hij'))

    def test_voice_only_chunk_teaches_the_named_voice(self):
        svc = self.make()
        now = self.join(svc)
        svc.tracker.embed = lambda audio: np.array([0.6, 0.8, 0.0])
        svc._meet_events([caption('b1', 'Dana', 'A long point', now, now + 6)])
        chunk = {'audio': np.zeros(16000 * 4, dtype=np.float32), 'stream': 'loopback', 't0': now + 1, 't1': now + 5,
                 'speech_s': 3.5, 'meeting_id': svc.session.meeting['id'], 'voice_only': True}
        svc.transcriber._transcribe(chunk)
        dana = next(s for s in svc.session.meeting['speakers'] if s['name'] == 'Dana')
        self.assertEqual(dana['count'], 1)
        self.assertEqual(len(dana['centroid']), 3)
        self.assertEqual(svc.session.meeting['turns'], [])

    def test_leaving_ends_the_meeting(self):
        svc = self.make()
        self.join(svc)
        svc._meet_events([{'type': 'left', 'code': 'abc-defg-hij'}])
        self.assertEqual(svc.session.state, 'idle')

    def test_off_ignores_the_extension(self):
        svc = self.make(mode='off')
        svc._meet_events([status()])
        svc._on_sources([])
        self.assertEqual(svc.session.state, 'idle')


if __name__ == '__main__':
    unittest.main()
