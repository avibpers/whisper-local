# tests/test_meet_extension.py
# The Koli Chrome extension for Google Meet, Koli's side: the feed of page
# events (joined, people, captions, left), the local HTTP door, speakers
# named by Meet, and how the meeting service uses them. No browser needed.
import datetime
import json
import sys
import tempfile
import time
import shutil
import subprocess
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

    # Seen live 2026-11-02: the meeting was started by hand before the
    # extension spoke, so Meet's people never reached it.
    def test_a_meeting_started_by_hand_takes_the_meet_call(self):
        svc = self.make()
        svc.start()
        self.assertEqual(svc.session.state, 'live')
        self.assertEqual(svc.session.meeting['source']['code'], '')
        svc._meet_events([status(title='Weekly sync')])
        m = svc.session.meeting
        self.assertEqual(m['source']['code'], 'abc-defg-hij')
        self.assertEqual(m['title'], 'Weekly sync')
        self.assertEqual([p['name'] for p in m['call']['people']], ['Dana', 'Avi'])

    def test_only_a_meeting_without_a_call_takes_one(self):
        svc = self.make()
        svc.start()
        m = svc.session.meeting
        m['source'] = {**m['source'], 'kind': 'native', 'app': 'discord.exe'}
        self.assertFalse(svc.session.adopt_meet_code(m['id'], 'abc-defg-hij'))
        m['source'] = {**m['source'], 'kind': 'manual', 'code': 'xyz-abcd-efg'}
        self.assertFalse(svc.session.adopt_meet_code(m['id'], 'abc-defg-hij'))
        self.assertEqual(m['source']['code'], 'xyz-abcd-efg')

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
        self.assertFalse(svc._captions_are_transcript('abc-defg-hij', now))

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

    # The chunk that teaches a voice costs little: no splitter, and only the
    # middle of the chunk goes to the voiceprint model.
    def test_voice_only_chunk_skips_the_splitter_and_teaches_a_short_clip(self):
        svc = self.make()
        now = self.join(svc)
        heard = []
        svc.tracker.embed = lambda audio: heard.append(len(audio)) or np.array([0.6, 0.8, 0.0])

        def splitter_must_not_run(audio):
            raise AssertionError('the splitter ran on a voice-only chunk')
        svc.transcriber.split = splitter_must_not_run
        svc._meet_events([caption('b1', 'Dana', 'A long point', now, now + 16)])
        svc.transcriber._transcribe({'audio': np.zeros(16000 * 15, dtype=np.float32), 'stream': 'loopback',
                                     't0': now + 1, 't1': now + 16, 'speech_s': 12.0,
                                     'meeting_id': svc.session.meeting['id'], 'voice_only': True})
        self.assertEqual(heard, [16000 * 6])

    # Captions read off the page stop while the tab is hidden: Whisper covers
    # that stretch, up to and including the chunk under way when they came back.
    def test_a_hidden_tab_hands_the_audio_to_whisper_until_it_is_back(self):
        svc = self.make()
        now = self.join(svc)
        code = 'abc-defg-hij'
        self.assertTrue(svc._captions_are_transcript(code, now))
        svc._meet_events([{**status(), 'captions_live': False, 'visible': False, 'source': 'page',
                           'at_ms': (now + 100) * 1000}])
        self.assertFalse(svc._captions_are_transcript(code, now + 110))
        svc._meet_events([{**status(), 'captions_live': True, 'visible': True, 'source': 'page',
                           'at_ms': (now + 200) * 1000}])
        self.assertFalse(svc._captions_are_transcript(code, now + 195))
        self.assertTrue(svc._captions_are_transcript(code, now + 201))

    # Meet's caption channel keeps going while the tab is hidden: no Whisper.
    def test_captions_from_the_channel_cover_a_hidden_tab(self):
        svc = self.make()
        now = self.join(svc)
        svc._meet_events([{**status(), 'captions_live': True, 'visible': False, 'source': 'channel'}])
        self.assertTrue(svc._captions_are_transcript('abc-defg-hij', now + 5))

    # Captions turned on for the first time are no "return": the chunk under
    # way stays with them, so the start isn't transcribed twice.
    def test_captions_turned_on_are_not_a_return_from_hidden(self):
        from koli.meeting.meet_feed import MeetFeed
        feed = MeetFeed()
        feed.apply([{**status(captions=False), 'at_ms': 1000 * 1000}])
        feed.apply([{**status(), 'captions_live': True, 'at_ms': 2000 * 1000}])
        self.assertTrue(feed.captions_cover('abc-defg-hij', 1500.0))

    def test_tab_and_caption_changes_are_noted_for_the_log(self):
        from koli.meeting.meet_feed import MeetFeed
        feed = MeetFeed()
        first = feed.apply([{**status(), 'captions_live': True, 'visible': True, 'source': 'channel',
                             'channel': {'captions': 3}}])
        self.assertEqual(len(first['notes']), 1)
        self.assertIn('channel', first['notes'][0])
        self.assertEqual(feed.apply([{**status(), 'captions_live': True, 'visible': True, 'source': 'channel'}])['notes'], [])
        hidden = feed.apply([{**status(), 'captions_live': True, 'visible': False, 'source': 'channel'}])
        self.assertIn('hidden', hidden['notes'][0])

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


# ── Meet's caption channel (chrome-extension/meet_channel.js, run by node) ───

NODE_CHECK = r"""
const { parseCaption, findUsers } = require(process.argv[1]);
const varint = (n) => { const out = []; while (n > 127) { out.push((n & 127) | 128); n = Math.floor(n / 128); } out.push(n); return out; };
const num = (no, n) => [...varint(no * 8), ...varint(n)];
const bytes = (no, b) => [...varint(no * 8 + 2), ...varint(b.length), ...b];
const str = (no, s) => bytes(no, [...Buffer.from(s, 'utf8')]);
const device = 'spaces/abc/devices/42';
const v1 = bytes(1, [...str(1, device), ...num(2, 7), ...num(3, 2), ...str(6, 'שלום')]);
const v2 = [...bytes(1, [...num(1, 7), ...num(2, 3), ...bytes(3, [...num(2, 1), ...str(3, 'hello'), ...str(4, 'en-US'), ...str(6, device)])]), ...bytes(6, num(1, 99))];
const people = bytes(1, bytes(2, bytes(2, bytes(2, [...str(1, device), ...str(2, 'Dana Levi'), ...str(7, 'me'), ...str(29, 'Dana')]))));
console.log(JSON.stringify({
  v1: parseCaption('captions', Uint8Array.from(v1)),
  v2: parseCaption('captions_v2', Uint8Array.from(v2)),
  new_label: parseCaption('captions_v9', Uint8Array.from(v1)),
  junk: parseCaption('captions', Uint8Array.from([1, 2, 3, 200, 200])),
  text_only: parseCaption('captions', Uint8Array.from(str(1, 'hello there'))),
  users: findUsers(Uint8Array.from(people)),
  no_users: findUsers(Uint8Array.from(str(1, 'not a person'))),
}));
"""


@unittest.skipUnless(shutil.which('node'), 'needs node')
class CaptionChannelDecoderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = Path(__file__).resolve().parents[1] / 'chrome-extension' / 'meet_channel.js'
        out = subprocess.run(['node', '-e', NODE_CHECK, str(script)], capture_output=True, encoding='utf-8', check=True)
        cls.result = json.loads(out.stdout)

    def test_the_first_layout(self):
        self.assertEqual(self.result['v1'], {'device': 'spaces/abc/devices/42', 'id': '7', 'version': '2',
                                             'final': False, 'text': 'שלום'})

    def test_the_second_layout_with_its_final_mark(self):
        self.assertEqual(self.result['v2'], {'device': 'spaces/abc/devices/42', 'id': '7', 'version': '3',
                                             'final': True, 'text': 'hello'})

    def test_a_new_channel_label_may_carry_either_layout(self):
        self.assertEqual(self.result['new_label']['text'], 'שלום')

    # Anything else is no caption, which leaves meet.js reading the page.
    def test_other_bytes_are_no_caption(self):
        self.assertIsNone(self.result['junk'])
        self.assertIsNone(self.result['text_only'])

    def test_people_are_found_under_any_wrappers(self):
        self.assertEqual(self.result['users'], [{'device': 'spaces/abc/devices/42', 'name': 'Dana', 'self': True}])
        self.assertEqual(self.result['no_users'], [])


if __name__ == '__main__':
    unittest.main()
