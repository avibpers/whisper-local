# services/meetings.py
# Meeting mode, assembled: the detector feeds the session manager, a live
# meeting opens the loopback stream and the microphone tap, their chunks go
# to the transcriber, and turns come back into the meeting. This is the one
# object the bridge, the tray and the hotkey talk to. Nothing here touches
# the windows: the panel listens to the 'meeting' and 'meeting_turn' events.

import logging
import threading
from typing import Callable, Optional

from ..meeting.capture import Chunker, LoopbackStream, MicTap, speech_prob_function
from ..meeting.detector import MeetingDetector
from ..meeting.session import SessionManager
from ..meeting.speakers import SpeakerTracker, make_embedder
from ..meeting.store import MeetingStore
from ..meeting.transcriber import MeetingTranscriber
from ..ui.events import publish as publish_event, report_problem
from ..ui.tasks import get_task_manager

logger = logging.getLogger(__name__)

LOOPBACK_HINT = ("Meetings need the loopback package to hear the call. Install it from the Meetings screen, "
                 "or: pip install koli[loopback]")


class MeetingService:
    def __init__(self, ctx, store: Optional[MeetingStore] = None, publish: Callable = None,
                 loopback_factory: Callable = None, embedder_factory: Callable = None):
        self.ctx = ctx
        self.store = store or MeetingStore()
        self._publish = publish or publish_event
        self._loopback_factory = loopback_factory or (lambda on_block, on_error: LoopbackStream(on_block, on_error))
        self._embedder_factory = embedder_factory or self._load_embedder
        self._lock = threading.Lock()
        self.detector: Optional[MeetingDetector] = None
        self._loopback = None
        self._mic_tap = None
        self._loop_chunker = None
        self._mic_chunker = None
        self._speech_prob = None
        self.tracker = SpeakerTracker(embed=None)
        self._embedder_task = None
        self.session = SessionManager(self.store, start_capture=self._start_capture, stop_capture=self._stop_capture,
                                      publish=self._publish, settings=self._setting)
        self.transcriber = MeetingTranscriber(
            engine_provider=lambda: self.ctx.state_manager.whisper_engine,
            is_busy=lambda: not self.ctx.state_manager.is_idle(), tracker=self.tracker,
            language_provider=lambda: str(self._setting('meeting.language', 'auto') or 'auto'),
            on_turn=self.session.add_turn, on_speaker=self.session.ensure_speaker,
            on_pending=self._on_pending, on_behind=self.session.set_behind)
        self.transcriber.start()
        if self._setting('meeting.enabled', True):
            self.start_detector()

    # ── Settings ─────────────────────────────────────────────────────────────

    def _setting(self, path: str, default=None):
        value = self.ctx.config_manager.get_path(path, default)
        return default if value is None else value

    # Only the switch needs a hand; everything else is read when it is used.
    def apply_setting(self, path: str, value):
        if path == 'meeting.enabled':
            if value:
                self.start_detector()
            else:
                self.stop_detector()

    # ── Detector ─────────────────────────────────────────────────────────────

    def start_detector(self):
        with self._lock:
            if self.detector is not None:
                return
            self.detector = MeetingDetector(
                on_sources=self.session.on_sources,
                rules_provider=lambda: list(getattr(self.ctx.state_manager.app_rules, 'rules', []) or []),
                mode_provider=lambda: str(self._setting('meeting.detect', 'auto') or 'auto'))
            self.detector.start()
        logger.info("Meeting detection on")

    def stop_detector(self):
        with self._lock:
            if self.detector is None:
                return
            self.detector.stop()
            self.detector = None
        self.session.on_sources([])
        logger.info("Meeting detection off")

    # ── Capture ──────────────────────────────────────────────────────────────

    # The microphone counts when it is wanted, you aren't dictating, and you
    # haven't excluded yourself.
    def _mic_allowed(self) -> bool:
        sm = self.ctx.state_manager
        return bool(self._setting('meeting.include_me', True)) and not self.session.excluded \
            and not sm.is_transcription_recording()

    def _start_capture(self, meeting: dict):
        self._ensure_embedder()
        self.tracker.load(meeting.get('speakers', []))
        if self._speech_prob is None:
            self._speech_prob = speech_prob_function()
        self._loop_chunker = Chunker('loopback', self._speech_prob, self.transcriber.submit,
                                     on_speech=self.session.on_speech)
        self._mic_chunker = Chunker('mic', self._speech_prob, self.transcriber.submit)
        self._loopback = self._loopback_factory(self._loop_chunker.feed, self._loopback_failed)
        self._loopback.start()
        self._mic_tap = MicTap(lambda: self.ctx.state_manager.audio_recorder, self._mic_chunker.feed, self._mic_allowed)
        self._mic_tap.start()

    def _stop_capture(self):
        if self._loopback is not None:
            self._loopback.stop()
            self._loopback = None
        if self._mic_tap is not None:
            self._mic_tap.stop()
            self._mic_tap = None
        for chunker in (self._loop_chunker, self._mic_chunker):
            if chunker is not None:
                chunker.flush()
        self._loop_chunker = self._mic_chunker = None

    def _loopback_failed(self, message: str):
        lowered = message.lower()
        if 'soundcard' in lowered or 'no module' in lowered:
            report_problem(LOOPBACK_HINT)
        else:
            report_problem(message)

    # The chunk being transcribed, so the panel can show a placeholder.
    def _on_pending(self, stream):
        m = self.session.meeting
        if m:
            self._publish('meeting_turn', {'id': m['id'], 'pending': {'stream': stream} if stream else None})

    # ── Speaker model ────────────────────────────────────────────────────────

    def _load_embedder(self):
        registry = self.ctx.model_registry
        key = str(self._setting('meeting.speaker_model', 'eres2net') or 'eres2net')
        if registry is None:
            return None
        path = registry.get_speaker_model_path(key)
        return make_embedder(path) if path else None

    # Loads the voiceprint model in the background the first time; turns
    # transcribed before it is ready are "Speaker ?".
    def _ensure_embedder(self):
        if self.tracker.embed is not None or self._embedder_task is not None:
            return

        def job(reporter):
            reporter.report(None, 'Downloading the voiceprint model')
            embed = self._embedder_factory()
            if embed is None:
                raise RuntimeError("The voiceprint model could not be loaded; speakers stay unnamed")
            self.tracker.embed = embed
            return 'ready'

        def done(task):
            self._embedder_task = None
            if task.status == 'failed':
                logger.warning(f"Voiceprint model: {task.error}")
        try:
            self._embedder_task = get_task_manager().run('Preparing speaker recognition', job, kind='meeting',
                                                         on_done=done)
        except Exception as e:
            logger.debug(f"Speaker model task not started: {e}")
            self._embedder_task = None

    # ── Commands (bridge, tray, hotkey) ──────────────────────────────────────

    def state(self) -> dict:
        return self.session.state_payload()

    def turns(self, meeting_id: str) -> list:
        m = self.session.meeting
        if m and m['id'] == meeting_id:
            return list(m['turns'])
        return self.store.load(meeting_id)['turns']

    def start(self):
        self.session.start()

    def end(self):
        self.session.end()
        self.transcriber.clear()

    def new(self):
        self.session.new()

    def toggle(self):
        if self.session.state in ('live', 'paused'):
            self.end()
        else:
            self.start()

    def pause(self):
        self.session.pause()

    def resume(self):
        self.session.resume()

    def set_excluded(self, on: bool):
        self.session.set_excluded(bool(on))

    def offer(self, accept: bool):
        if accept:
            self.session.offer_accept()
        else:
            self.session.offer_decline()

    def rename_speaker(self, speaker_id: str, name: str):
        if self.session.meeting:
            self.session.rename_speaker(speaker_id, name)

    def rename_saved_speaker(self, meeting_id: str, speaker_id: str, name: str):
        m = self.store.load(meeting_id)
        for s in m['speakers']:
            if s['id'] == speaker_id:
                s['name'] = str(name).strip()[:40] or s['name']
        self.store.save(m)

    def continue_previous(self):
        self.session.continue_previous()

    def split_resumed(self):
        self.session.split_resumed()

    def list(self) -> list:
        return self.store.list()

    def get(self, meeting_id: str) -> dict:
        m = self.session.meeting
        if m and m['id'] == meeting_id:
            return dict(m)
        return self.store.load(meeting_id)

    def delete(self, meeting_id: str) -> bool:
        m = self.session.meeting
        if m and m['id'] == meeting_id:
            raise ValueError("End the meeting before deleting it")
        return self.store.delete(meeting_id)

    def export(self, meeting_id: str, fmt: str) -> str:
        return self.store.export(meeting_id, fmt)

    def shutdown(self):
        self.stop_detector()
        if self.session.state in ('live', 'paused'):
            self.session.end()
        self.transcriber.stop()
