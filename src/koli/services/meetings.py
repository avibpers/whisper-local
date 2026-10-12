# services/meetings.py
# Meeting mode, assembled: the detector feeds the session manager, an idle
# listener hears the call before a meeting starts, a live meeting opens the
# loopback stream and the microphone tap, their chunks go to the transcriber,
# and turns come back into the meeting. This is the one object the bridge,
# the tray and the hotkey talk to. Nothing here touches the windows: the panel
# listens to the 'meeting' and 'meeting_turn' events.

import importlib.util
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from ..meeting.capture import Chunker, LoopbackStream, MicStream, MicTap, speech_prob_function
from ..meeting.detector import MeetingDetector
from ..meeting.diarize import make_splitter
from ..meeting.session import SessionManager, merge_speaker_into, same_name_as
from ..meeting.speakers import SAME_VOICE, SpeakerTracker, make_embedder
from ..meeting.store import MeetingStore
from ..meeting.transcriber import YOU_FIRM, MeetingTranscriber
from ..meeting.voices import FILE as VOICES_FILE, VoiceBook
from ..ui.events import publish as publish_event, report_problem
from ..ui.tasks import get_task_manager

logger = logging.getLogger(__name__)

MIN_KEEP_CHUNKS = 3   # chunks a remote voice needs before it is saved when the meeting ends
MY_VOICE_MIN = 3      # hotkey dictations on a microphone before auto-dictation checks your voice there

LOOPBACK_HINT = ("Meetings need the loopback package to hear the call. Install it from the Meetings screen, "
                 "or: pip install koli[loopback]")


def _soundcard_installed() -> bool:
    return importlib.util.find_spec('soundcard') is not None


def _active_sessions() -> list:
    from ..platform import audio_sessions
    return audio_sessions.active()


class MeetingService:
    def __init__(self, ctx, store: Optional[MeetingStore] = None, publish: Callable = None,
                 loopback_factory: Callable = None, embedder_factory: Callable = None,
                 speech_prob_factory: Callable = None, loopback_available: Callable[[], bool] = None,
                 report: Callable[[str], None] = None, sessions_fn: Callable[[], list] = None,
                 autostart: bool = True, mic_stream_factory: Callable = None,
                 splitter_factory: Callable = None, voices: Optional[VoiceBook] = None):
        self.ctx = ctx
        self.store = store or MeetingStore()
        # Saved voices live next to the meetings folder (beside it, so the
        # meeting list never reads them as a meeting).
        self.voices = voices if voices is not None else VoiceBook(Path(self.store.folder).parent / VOICES_FILE)
        self._splitter_factory = splitter_factory or self._load_splitter
        self._publish = publish or publish_event
        self._loopback_factory = loopback_factory or (
            lambda on_block, on_error, device=None: LoopbackStream(on_block, on_error, device=device))
        self._embedder_factory = embedder_factory or self._load_embedder
        self._speech_prob_factory = speech_prob_factory or speech_prob_function
        self._loopback_available = loopback_available or _soundcard_installed
        self._report = report or report_problem
        self._sessions_fn = sessions_fn or _active_sessions
        self._mic_stream_factory = mic_stream_factory or (
            lambda device, on_block, allow, on_error: MicStream(device, on_block, allow, on_error))
        self._reported_missing = False
        self._closed = False              # shutdown began: nothing opens any more
        self._lock = threading.Lock()
        self.detector: Optional[MeetingDetector] = None
        self._idle_stream = None          # hears the call while a source waits for speech
        self._loopbacks = []              # one stream per output device the meeting listens to
        self._loop_chunkers = []
        self._mic_source = None           # the tap on the dictation microphone, or a stream of its own
        self._mic_chunker = None
        self.tracker = SpeakerTracker(embed=None, voices=self.voices)
        self._meeting_mic = ''            # the meeting microphone's name, for your voice on it
        self._embedder_task = None
        self.session = SessionManager(self.store, start_capture=self._start_capture, stop_capture=self._stop_capture,
                                      publish=self._publish, settings=self._setting, on_speakers=self.tracker.load,
                                      on_ended=self._keep_voices)
        self.transcriber = MeetingTranscriber(
            engine_provider=lambda: self.ctx.state_manager.whisper_engine,
            is_busy=lambda: not self.ctx.state_manager.is_idle(), tracker=self.tracker,
            language_provider=lambda: str(self._setting('meeting.language', 'auto') or 'auto'),
            on_turn=self.session.add_turn, on_speaker=self.session.ensure_speaker,
            on_pending=self._on_pending, on_behind=self.session.set_behind, keep_audio=self._keep_audio)
        self.transcriber.start()
        if autostart and self._setting('meeting.enabled', True):
            self.start_detector()

    # The app window is up: detection may start. Probing Chrome and opening
    # a loopback while WebView2 initialises once delayed the main window past
    # pywebview's limit, which then never created the bubble and the panel.
    def ui_ready(self):
        if self._setting('meeting.enabled', True):
            self.start_detector()

    # ── Settings ─────────────────────────────────────────────────────────────

    def _setting(self, path: str, default=None):
        value = self.ctx.config_manager.get_path(path, default)
        return default if value is None else value

    # The switch needs a hand, and a new splitter choice drops the loaded one
    # so the next meeting loads it; everything else is read when it is used.
    def apply_setting(self, path: str, value):
        if path == 'meeting.enabled':
            if value:
                self.start_detector()
            else:
                self.stop_detector()
        elif path == 'meeting.segmentation_model':
            self.transcriber.split = None

    # The loopback package was installed while the app runs: detection can
    # start now, without a restart.
    def package_installed(self):
        self._reported_missing = False
        if self._setting('meeting.enabled', True):
            self.start_detector()

    # Without the loopback package nothing can hear the call: keep meeting
    # mode off until it is installed. Startup only logs it; an explicit start
    # raises the problem (with the Install row on the Meetings screen).
    def _loopback_ok(self, report: bool = True) -> bool:
        if self._loopback_available():
            return True
        if report and not self._reported_missing:
            self._reported_missing = True
            self._report(LOOPBACK_HINT)
        logger.warning(LOOPBACK_HINT)
        return False

    # ── Detector and the idle listener ───────────────────────────────────────

    def start_detector(self):
        if not self._loopback_ok(report=False):
            return
        with self._lock:
            if self.detector is not None:
                return
            self.detector = MeetingDetector(
                on_sources=self._on_sources,
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
        self._on_sources([])
        logger.info("Meeting detection off")

    # The detector's view: the session decides, then the idle listener opens
    # or closes to match whether a source is waiting for speech.
    def _on_sources(self, sources: list):
        self.session.on_sources(sources)
        self._sync_idle_listener()

    def _sync_idle_listener(self):
        pending = self.session.pending_source()
        want = pending is not None and not self._closed
        to_stop = None
        with self._lock:
            if want and self._idle_stream is None:
                chunker = Chunker('loopback', self._speech_prob_factory(), lambda chunk: None,
                                  on_speech=self._on_idle_speech)
                self._idle_stream = self._loopback_factory(chunker.feed, self._loopback_failed,
                                                           device=pending.get('device') or None)
                self._idle_stream.start()
            elif not want and self._idle_stream is not None:
                to_stop, self._idle_stream = self._idle_stream, None
        if to_stop is not None:
            to_stop.stop()

    # Speech from the idle listener's own thread. A start that follows stops
    # the listener from inside its callback; LoopbackStream.stop never joins
    # its own thread.
    def _on_idle_speech(self, seconds: float):
        self.session.on_speech(seconds)
        self._sync_idle_listener()

    # ── Capture ──────────────────────────────────────────────────────────────

    # The microphone counts when it is wanted, you aren't dictating, and you
    # haven't excluded yourself.
    def _mic_allowed(self) -> bool:
        sm = self.ctx.state_manager
        return bool(self._setting('meeting.include_me', True)) and not self.session.excluded \
            and not sm.is_transcription_recording()

    # Called by the session once a meeting is live (outside its lock). Each
    # stream gets its own voice detector; chunks carry the meeting's id so a
    # late one still reaches the right file.
    def _start_capture(self, meeting: dict):
        meeting_id = meeting['id']
        self._ensure_embedder()
        self.tracker.load(meeting.get('speakers', []))
        idle = None
        with self._lock:
            idle, self._idle_stream = self._idle_stream, None
        if idle is not None:
            idle.stop()

        def submit(chunk):
            self.transcriber.submit({**chunk, 'meeting_id': meeting_id})
        loopbacks, loop_chunkers = [], []
        for device in self._output_devices(meeting):
            chunker = Chunker('loopback', self._speech_prob_factory(), submit)
            loop_chunkers.append(chunker)

            # The call's loudness goes to the transcriber too, to spot it leaking into your microphone.
            def on_block(block, when, feed=chunker.feed):
                self.transcriber.note_call(block, when)
                feed(block, when)
            loopbacks.append(self._loopback_factory(on_block, self._loopback_failed, device=device))
        mic_chunker = Chunker('mic', self._speech_prob_factory(), submit)
        mic_source = self._open_mic(mic_chunker)
        with self._lock:
            self._loop_chunkers, self._mic_chunker = loop_chunkers, mic_chunker
            self._loopbacks, self._mic_source = loopbacks, mic_source
        # Your voice on this meeting's microphone (each mic hears you differently);
        # its name is kept for when the meeting ends and the mic is closed.
        self._meeting_mic = self._mic_name() or ''
        mine = self.voices.self_voice(mic=self._meeting_mic)
        self.transcriber.set_you(mine[1] if mine else None)
        for loopback in loopbacks:
            loopback.start()

    # Your side: the dictation microphone's always-open stream (a tap), or a
    # device of your own for meetings. A device that won't open is reported
    # and the tap is used, so a meeting never silently loses your side.
    def _open_mic(self, mic_chunker: Chunker):
        device = self._setting('meeting.input_device', '')
        if device not in ('', None):
            stream = self._mic_stream_factory(device, mic_chunker.feed, self._mic_allowed, self._mic_failed)
            if stream.start():
                return stream
            logger.warning(f"Meeting microphone {device!r} unavailable; tapping the dictation microphone")
        tap = MicTap(lambda: self.ctx.state_manager.audio_recorder, mic_chunker.feed, self._mic_allowed)
        tap.start()
        return tap

    def _mic_failed(self, message: str):
        self._report(f"{message}. Using the dictation microphone for this meeting.")

    # Which outputs to listen to: the one the detected call plays on; for a
    # meeting started by hand, every device with sound right now; and the
    # Windows default when nothing says otherwise (None).
    def _output_devices(self, meeting: dict) -> list:
        device = (meeting.get('source') or {}).get('device')
        if device:
            return [device]
        try:
            with_sound = []
            for s in self._sessions_fn():
                name = s.get('device')
                if int(s.get('state', 0)) == 1 and name and name not in with_sound:
                    with_sound.append(name)
            if with_sound:
                return with_sound
        except Exception as e:
            logger.debug(f"Output devices unknown ({e}); using the default output")
        return [None]

    def _stop_capture(self):
        with self._lock:
            loopbacks, self._loopbacks = self._loopbacks, []
            mic_source, self._mic_source = self._mic_source, None
            chunkers = [c for c in (*self._loop_chunkers, self._mic_chunker) if c is not None]
            self._loop_chunkers, self._mic_chunker = [], None
        for loopback in loopbacks:
            loopback.stop()
        if mic_source is not None:
            mic_source.stop()
        for chunker in chunkers:
            chunker.flush()

    def _loopback_failed(self, message: str):
        lowered = message.lower()
        if 'soundcard' in lowered or 'no module' in lowered:
            self._report(LOOPBACK_HINT)
        else:
            self._report(message)

    # With meeting.keep_audio on, each chunk's audio is saved beside its
    # meeting (for tuning how speakers are told apart); off by default.
    def _keep_audio(self, chunk: dict):
        if chunk.get('meeting_id') and self._setting('meeting.keep_audio', False):
            self.store.save_audio(chunk['meeting_id'], chunk)

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

    # The voice splitter: the segmentation model plus the voiceprint model,
    # or None when it is turned off or can't be had.
    def _load_splitter(self):
        registry = self.ctx.model_registry
        key = str(self._setting('meeting.segmentation_model', '') or '')
        if registry is None or not key:
            return None
        segmentation = registry.get_speaker_model_path(key)
        voiceprint = registry.get_speaker_model_path(
            str(self._setting('meeting.speaker_model', 'eres2net') or 'eres2net'))
        return make_splitter(segmentation, voiceprint) if segmentation and voiceprint else None

    # Loads the voiceprint model (and the voice splitter) in the background
    # the first time; turns transcribed before it is ready are "Speaker ?",
    # and a chunk is one voice until the splitter is ready. A splitter that
    # won't load only costs the splitting.
    def _ensure_embedder(self):
        want_split = bool(self._setting('meeting.segmentation_model', ''))
        if (self.tracker.embed is not None and (self.transcriber.split is not None or not want_split)) \
                or self._embedder_task is not None:
            return

        def job(reporter):
            if self.tracker.embed is None:
                reporter.report(None, 'Downloading the voiceprint model')
                embed = self._embedder_factory()
                if embed is None:
                    raise RuntimeError("The voiceprint model could not be loaded; speakers stay unnamed")
                self.tracker.embed = embed
            if want_split and self.transcriber.split is None:
                reporter.report(None, 'Downloading the voice splitter')
                try:
                    self.transcriber.split = self._splitter_factory()
                except Exception as e:
                    logger.warning(f"Voice splitter unavailable, each chunk stays one voice: {e}")
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
        if self._closed or not self._loopback_ok():
            return
        self.session.start()
        self._sync_idle_listener()

    # Queued chunks are not dropped: they drain into the ended meeting's file.
    def end(self):
        self.session.end()
        self._sync_idle_listener()

    def new(self):
        self.session.new()

    # A meeting is on, recording or paused: the call is still in your ears.
    def is_running(self) -> bool:
        return self.session.state in ('live', 'paused')

    def toggle(self):
        if self.is_running():
            self.end()
        else:
            self.start()

    def pause(self):
        self.session.pause()

    def resume(self):
        self.session.resume()

    def set_excluded(self, on: bool):
        self.session.set_excluded(bool(on))

    # The microphone "You" comes from now: the meeting's own device, or the
    # dictation microphone it taps. Its Windows name, or None.
    def _mic_name(self) -> Optional[str]:
        import sounddevice as sd
        source = self._mic_source
        device = getattr(source, 'device', None) if isinstance(source, MicStream) \
            else getattr(self.ctx.state_manager.audio_recorder, 'device', None)
        try:
            info = sd.query_devices(device) if device is not None else sd.query_devices(kind='input')
            return info['name']
        except Exception as e:
            logger.debug(f"Meeting microphone unknown: {e}")
            return None

    # One key mutes you everywhere: the microphone is muted in Windows, so
    # the meeting app sends silence (like its own mute) and Koli leaves you
    # out ("Exclude me"); again unmutes both. Muting inside the meeting app
    # alone can't be seen from here: the app keeps the microphone open.
    # Returns the new state, or None when the microphone can't be muted.
    def toggle_mic_mute(self) -> Optional[bool]:
        from ..platform import media
        name = self._mic_name()
        muted = media.mic_muted(name) if name else None
        if muted is None:
            return None
        if not media.set_mic_muted(name, not muted):
            return None
        self.session.set_excluded(not muted)
        logger.info(f"{name} {'muted' if not muted else 'unmuted'} for every app")
        return not muted

    def offer(self, accept: bool):
        if accept:
            self.session.offer_accept()
        else:
            self.session.offer_decline()
        self._sync_idle_listener()

    # ── Saved voices ─────────────────────────────────────────────────────────

    # When a meeting ends, every remote voice in it is saved with a permanent
    # id (refining the saved voice it was linked to, or the one it matches,
    # or as a new voice), and the meeting file records each speaker's id.
    # Labels that are clearly one voice are merged first (the splitter cut
    # one person in two). A speaker heard in fewer than MIN_KEEP_CHUNKS
    # chunks is too thin a sample to save.
    def _keep_voices(self, meeting: dict):
        if not self._setting('meeting.remember_voices', True):
            return
        try:
            changed = self._merge_twins(meeting)
            taken = []
            for s in meeting['speakers']:
                if s['id'] == 'you' or not s.get('centroid') or int(s.get('count', 0)) < MIN_KEEP_CHUNKS:
                    continue
                voice_id = self.voices.keep(s['centroid'], s['count'], meeting['id'], voice_id=s.get('voice_id'),
                                            taken=taken)
                taken.append(voice_id)
                if s.get('voice_id') != voice_id:
                    s['voice_id'] = voice_id
                    changed = True
            # Your own voice, learned on the meeting microphone, and the
            # meeting's "You" pointing to it.
            learned = self.transcriber.you_learned()
            if learned and learned[1] >= YOU_FIRM:
                mine = self.voices.keep_self(learned[0], learned[1], meeting['id'],
                                             mic=getattr(self, '_meeting_mic', '') or '')
                you = next((s for s in meeting['speakers'] if s['id'] == 'you'), None)
                if you is not None and you.get('voice_id') != mine:
                    you['voice_id'] = mine
                    changed = True
            if changed:
                self.store.save(meeting)
            logger.info(f"Saved {len(taken)} voice(s) from meeting {meeting['id']}")
        except Exception as e:
            logger.warning(f"Voices of meeting {meeting.get('id')} not saved: {e}")

    # Merge speakers of one meeting whose voiceprints are clearly one voice;
    # the one heard less joins the other. Returns whether anything changed.
    def _merge_twins(self, meeting: dict) -> bool:
        changed = True
        merged_any = False
        while changed:
            changed = False
            remote = [s for s in meeting['speakers'] if s['id'] != 'you' and s.get('centroid')]
            for i, a in enumerate(remote):
                for b in remote[i + 1:]:
                    va, vb = np.asarray(a['centroid']), np.asarray(b['centroid'])
                    sim = float(np.dot(va, vb) / ((np.linalg.norm(va) * np.linalg.norm(vb)) or 1.0))
                    if sim >= SAME_VOICE:
                        keep, drop = (a, b) if a.get('count', 0) >= b.get('count', 0) else (b, a)
                        self._merge_voice_ids(drop.get('voice_id'), keep)
                        merge_speaker_into(meeting, drop['id'], keep['id'])
                        logger.info(f"Merged {drop['name']} into {keep['name']}: one voice ({sim:.2f})")
                        changed = merged_any = True
                        break
                if changed:
                    break
        return merged_any

    # A speaker merged into `keep` brings its saved voice along.
    def _merge_voice_ids(self, from_voice: Optional[str], keep: dict):
        if from_voice and keep.get('voice_id') and from_voice != keep['voice_id']:
            self.voices.merge(from_voice, keep['voice_id'])
        elif from_voice and not keep.get('voice_id'):
            keep['voice_id'] = from_voice

    # A name is a label on the speaker's saved voice, so later meetings use
    # it too; a speaker not saved yet is saved now. The name of another
    # speaker in the same meeting merges the two: one person, cut in two.
    def rename_speaker(self, speaker_id: str, name: str):
        m = self.session.meeting
        if not m:
            return
        twin = same_name_as(m, speaker_id, name)
        if twin:
            source = next((s for s in m['speakers'] if s['id'] == speaker_id), {})
            target = next((s for s in m['speakers'] if s['id'] == twin), {})
            self._merge_voice_ids(source.get('voice_id'), target)
            self.session.merge_speakers(speaker_id, twin)
            self.tracker.merge(speaker_id, twin)
            speaker_id = twin
        else:
            self.session.rename_speaker(speaker_id, name)
            self.tracker.rename(speaker_id, str(name).strip()[:40])
        speaker = next((s for s in m['speakers'] if s['id'] == speaker_id), None)
        voiceprint = self.tracker.voiceprint(speaker_id)
        if speaker is None or not voiceprint:
            return
        voice_id = speaker.get('voice_id') or self.voices.keep(
            voiceprint[0], voiceprint[1], m['id'], taken=[s.get('voice_id') for s in m['speakers'] if s.get('voice_id')])
        self.voices.set_name(voice_id, name)
        if speaker.get('voice_id') != voice_id:
            self.session.ensure_speaker({'id': speaker_id, 'voice_id': voice_id})
            self.tracker.set_voice_id(speaker_id, voice_id)

    def rename_saved_speaker(self, meeting_id: str, speaker_id: str, name: str):
        m = self.store.load(meeting_id)
        twin = same_name_as(m, speaker_id, name)
        if twin:
            source = next((s for s in m['speakers'] if s['id'] == speaker_id), {})
            target = next((s for s in m['speakers'] if s['id'] == twin), {})
            self._merge_voice_ids(source.get('voice_id'), target)
            merge_speaker_into(m, speaker_id, twin)
            speaker_id = twin
        for s in m['speakers']:
            if s['id'] == speaker_id:
                s['name'] = str(name).strip()[:40] or s['name']
                if s.get('centroid'):
                    s['voice_id'] = s.get('voice_id') or self.voices.keep(
                        s['centroid'], s.get('count', 1), meeting_id,
                        taken=[o.get('voice_id') for o in m['speakers'] if o.get('voice_id')])
                    self.voices.set_name(s['voice_id'], s['name'])
        self.store.save(m)

    # ── Your voice in dictation ──────────────────────────────────────────────
    # The same voiceprints, for auto-dictation: a hotkey dictation (you
    # pressed the key, so it is you) teaches your voice on that microphone;
    # an auto-dictation recording that doesn't sound like you there is not
    # typed. Until a microphone has MY_VOICE_MIN samples nothing is blocked.

    def _voiceprint(self, audio) -> Optional[np.ndarray]:
        embed = self.tracker.embed
        if embed is None:
            self._ensure_embedder()       # loads in the background; ready for the next one
            return None
        try:
            vec = embed(audio)
        except Exception as e:
            logger.debug(f"Voiceprint of a dictation failed: {e}")
            return None
        if vec is None:
            return None
        vec = np.asarray(vec, dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm > 0 else None

    def learn_my_voice(self, audio, mic: str):
        vec = self._voiceprint(audio)
        if vec is not None:
            self.voices.keep_self(vec, 1, mic=mic)

    # How much `audio` sounds like you on `mic` (cosine), or None when your
    # voice there isn't known well enough yet (or no voiceprint could be made).
    def my_voice_similarity(self, audio, mic: str) -> Optional[float]:
        mine = self.voices.self_voice(mic=mic)
        if mine is None or mine[2] < MY_VOICE_MIN:
            return None
        vec = self._voiceprint(audio)
        return None if vec is None else float(np.dot(vec, mine[1]))

    # For the Settings voice check: the score, plus why there may be none -
    # how many hotkey dictations taught your voice on `mic` (MY_VOICE_MIN
    # needed) and whether the voice model is loaded. Waits up to `wait_s` for
    # the model, which loads on first use.
    def my_voice_status(self, audio, mic: str, wait_s: float = 15.0) -> dict:
        mine = self.voices.self_voice(mic=mic)
        learned = mine[2] if mine else 0
        deadline = time.monotonic() + wait_s
        if self.tracker.embed is None:
            self._ensure_embedder()
            while self.tracker.embed is None and time.monotonic() < deadline:
                time.sleep(0.2)
        ready = self.tracker.embed is not None
        score = None
        if ready and learned >= MY_VOICE_MIN:
            vec = self._voiceprint(audio)
            score = None if vec is None else round(float(np.dot(vec, mine[1])), 2)
        return {'score': score, 'learned': learned, 'needed': MY_VOICE_MIN, 'ready': ready}

    # ── Your voice for the wake word ─────────────────────────────────────────
    # wake_word.py keeps its own voiceprint of your word; it borrows the model.

    def voiceprint_of(self, audio) -> Optional[np.ndarray]:
        return self._voiceprint(audio)

    # Whether a voiceprint can be made now. If not, the model starts loading
    # (a background task with progress), so ask again in a moment.
    def voiceprint_ready(self) -> bool:
        if self.tracker.embed is not None:
            return True
        self._ensure_embedder()
        return False

    # Saved voices, for the Meetings screen: ids, names and where they were
    # heard; never the voiceprints.
    def voices_list(self) -> list:
        return self.voices.all()

    def forget_voice(self, voice_id: str) -> bool:
        return self.voices.forget(voice_id)

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

    # A detector tick can land while this runs; `_closed` keeps it from
    # opening a listener nothing would close.
    def shutdown(self):
        self._closed = True
        self.stop_detector()
        if self.session.state in ('live', 'paused'):
            self.session.end()
        self._sync_idle_listener()
        self.transcriber.stop()
