# live_session_flow.py
# Runs "words while you speak" for one hotkey dictation: picks the engine
# (live_words.py), keeps the bubble's session (dictation_session.py) filled
# with words, pauses and resumes it while you fix something, and at the end
# hands its text to the state manager's pipeline to be written into the app.
# Kept apart from state_manager.py so the dictation pipeline stays readable;
# the state manager calls in at a handful of points (start, phrase pause,
# stop, silence, cancel) and the bubble page calls in through the bridge.

import logging
import threading
import time
from typing import Optional

from . import live_words_usage
from .dictation_session import DictationSession
from .live_words import choose_engine, resolve_language, usage_key, EngineChoice
from .platform import foreground
from .ui.events import publish

logger = logging.getLogger(__name__)

PROMPT_CONTEXT_CHARS = 200     # how much of the bubble's text primes the next phrase
PHRASE_WAIT_S = 10.0           # the most a finish waits for phrases still being transcribed


class LiveWordsFlow:
    def __init__(self, sm):
        self.sm = sm
        self.session: Optional[DictationSession] = None
        self.choice: EngineChoice = EngineChoice('off')
        self._next_id = 0
        self._notices_said = set()
        self._base_prompt = None        # Whisper's prompt for this recording, before phrase context
        self._seconds = 0.0             # audio this session listened to
        self._started_at = None
        self._tokens_in = self._tokens_out = 0
        self._in_flight = 0
        self._idle = threading.Condition()
        self._phrase_lock = threading.Lock()   # phrases are transcribed in the order they were spoken

    # ── Starting and resuming ────────────────────────────────────────────────

    # Called just before the microphone opens for a recording. Returns the
    # phrase pause the recorder should use (None for no phrases). A session
    # you opened and paused is continued; otherwise the engine is chosen anew.
    def prepare(self, mode: str) -> Optional[float]:
        session = self.session
        if session is not None and session.opened:
            return self._phrase_pause()
        self.session = None
        cfg = self.sm.config_manager.config.get('live_words') or {}
        try:
            rule = self.sm.app_rules.match_for_foreground() or {}
        except Exception:
            rule = {}
        whisper_cfg = self.sm.config_manager.get_whisper_config() or {}
        if (rule.get('task') or whisper_cfg.get('task')) == 'translate' and mode == 'dictate':
            mode = 'translate'
        language = resolve_language(whisper_cfg, rule)
        self.choice = choose_engine(cfg, rule, mode=mode, language=language,
                                    has_key=self._has_key, local_ready=self._local_ready())
        if self.choice.engine == 'off':
            return None
        self._language = language
        self._next_id += 1
        self.session = DictationSession(self.choice.engine, self._next_id, foreground.get_foreground_app() or {})
        self._seconds, self._tokens_in, self._tokens_out = 0.0, 0, 0
        self._base_prompt = None
        if self.choice.notice and self.choice.notice not in self._notices_said:
            self._notices_said.add(self.choice.notice)
            self._notice = self.choice.notice
        else:
            self._notice = ''
        logger.info(f"Words while you speak: {self.choice.engine}"
                    + (f" ({self.choice.model})" if self.choice.model else '')
                    + (f" - {self.choice.notice}" if self.choice.notice else ''))
        return self._phrase_pause()

    # The microphone is open: the session listens.
    def started(self) -> None:
        session = self.session
        if session is None:
            return
        self._base_prompt = getattr(self.sm.whisper_engine, 'initial_prompt', None)
        self._started_at = time.monotonic()
        session.set_listening(True)
        self.publish()

    def _phrase_pause(self) -> Optional[float]:
        if self.choice.engine in ('phrases', 'gemini', 'openai'):
            return self.sm._auto_phrase_pause() or 1.0
        return None

    def _has_key(self, provider: str) -> bool:
        try:
            from .llm import credentials
            return bool(credentials.load(provider).get('key'))
        except Exception:
            return False

    def _local_ready(self) -> bool:
        recorder = self.sm.audio_recorder
        return bool(getattr(recorder, 'continuous_streaming', None))

    def active(self) -> bool:
        return self.session is not None

    # ── Words coming in ──────────────────────────────────────────────────────

    # A phrase pause: transcribe what was said since the last one into the
    # bubble. `session` is the one the audio belongs to (read when the pause
    # happened): if it has ended since, the words have nowhere to go.
    def on_phrase(self, audio, session: Optional[DictationSession] = None) -> None:
        session = session or self.session
        if session is None or audio is None:
            return
        self._begin_work()
        try:
            with self._phrase_lock:
                text = self._transcribe_phrase(audio, session)
                if text and session is self.session:
                    session.add_final(self.sm._postprocess_piece(text))
                    self.publish()
                elif text:
                    logger.info("Words while you speak: a phrase finished after its session ended - dropped")
        except Exception as e:
            logger.error(f"Words while you speak: a phrase failed: {e}", exc_info=True)
        finally:
            self._end_work()

    # Whisper for one phrase, primed with the words already in the bubble so
    # names and spellings stay the same.
    def _transcribe_phrase(self, audio, session: DictationSession) -> str:
        engine = self.sm.whisper_engine
        context = session.text()[-PROMPT_CONTEXT_CHARS:]
        prompt = ' '.join(p for p in (self._base_prompt, context) if p).strip() or None
        with self.sm._pipeline_lock:
            try:
                engine.initial_prompt = prompt
            except Exception:
                pass
            try:
                text = engine.transcribe_audio(audio) or ''
            finally:
                try:
                    engine.initial_prompt = self._base_prompt
                except Exception:
                    pass
        from .dictation_cleanup import is_prompt_echo
        if text and prompt and is_prompt_echo(text, prompt):
            logger.info("Words while you speak: Whisper repeated its prompt - not speech")
            return ''
        return text.strip()

    def _begin_work(self) -> None:
        with self._idle:
            self._in_flight += 1

    def _end_work(self) -> None:
        with self._idle:
            self._in_flight = max(0, self._in_flight - 1)
            self._idle.notify_all()

    def _wait_for_phrases(self) -> None:
        with self._idle:
            self._idle.wait_for(lambda: self._in_flight == 0, timeout=PHRASE_WAIT_S)

    # The microphone closed (stop or pause): the words since the last pause
    # go into the bubble, if voice detection heard any (Whisper fills silence
    # with "Thank you.").
    def _take_tail(self, audio, session: DictationSession) -> None:
        if self._started_at is not None:
            self._seconds += time.monotonic() - self._started_at
            self._started_at = None
        if audio is not None and self.sm._detector_heard_speech():
            self.on_phrase(audio, session=session)

    # ── Pause, finish, cancel ────────────────────────────────────────────────

    # You let go of the keys with the bubble focused, or went quiet while
    # fixing something: listening stops, the text stays in the bubble.
    def pause(self) -> None:
        session = self.session
        if session is None:
            return
        audio = self.sm.audio_recorder.stop_recording() if self.sm.audio_recorder.get_recording_status() else None
        self._take_tail(audio, session)
        session.set_listening(False)
        self.sm.audio_feedback.play_stop_sound()
        self.sm._update_ui_state('idle')
        self.publish()
        logger.info("Words while you speak: paused - the text waits in the bubble")

    # Write the bubble's text into the app (and press Enter when `send`).
    def finish(self, send: bool = False) -> None:
        session = self.session
        if session is None:
            return
        recording = self.sm.audio_recorder.get_recording_status()
        audio = self.sm.audio_recorder.stop_recording() if recording else None
        self._take_tail(audio, session)
        self._wait_for_phrases()
        session.commit_interim()
        self.session = None
        self.publish()
        text = session.text().strip()
        self.sm.audio_feedback.play_stop_sound()
        if not text:
            self.sm._emit_pipeline('empty', mode='dictate', why='No speech detected')
            if self.sm.level_overlay:
                self.sm.level_overlay.flash_failure('No speech detected')
            self.sm._update_ui_state('idle')
            self._record_usage(session)
            return
        if session.opened and not self._give_focus_back(session):
            self.sm.keep_in_fallback(text, "The window you dictated into is gone - your text is safe here.")
        else:
            self.sm._transcription_pipeline(None, send, written={
                'text': text, 'edited': session.edited, 'engine': session.engine,
                'original': session.original(), 'duration': self._seconds})
        self._record_usage(session)

    def _give_focus_back(self, session: DictationSession) -> bool:
        hook = getattr(self.sm, 'bubble_focus', None)
        if hook is None:
            return True
        try:
            return bool(hook.give_back(session.target_window))
        except Exception as e:
            logger.warning(f"Words while you speak: couldn't return to the app: {e}")
            return False

    # Esc, Cancel, or a cancelled recording: the text is dropped (History
    # keeps it as cancelled).
    def cancel(self) -> None:
        session = self.session
        if session is None:
            return
        self.session = None
        if self.sm.audio_recorder.get_recording_status():
            self.sm.audio_recorder.cancel_recording()
        if session.opened:
            self._give_focus_back(session)
        text = session.text().strip()
        if text:
            self.sm.keep_cancelled(text, session.target_window)
        self.sm._update_ui_state('idle')
        self.publish()
        logger.info("Words while you speak: cancelled")

    # The recording was cancelled elsewhere (a tap, Esc on the recorder):
    # drop the session without touching the recorder again.
    def drop(self) -> None:
        if self.session is not None:
            self.session = None
            self.publish()

    def _record_usage(self, session: DictationSession) -> None:
        try:
            live_words_usage.record(usage_key(self.choice), self._seconds, session.words(), session.edited,
                                    self._tokens_in, self._tokens_out)
        except Exception as e:
            logger.debug(f"Usage not recorded: {e}")

    # ── The bubble page ──────────────────────────────────────────────────────

    def open(self) -> bool:
        session = self.session
        if session is None or not session.open():
            return False
        hook = getattr(self.sm, 'bubble_focus', None)
        if hook is not None:
            try:
                hook.take()
            except Exception as e:
                logger.debug(f"Bubble focus not taken: {e}")
        self.publish()
        return True

    def edit(self, text: str, start: int, end: int) -> None:
        session = self.session
        if session is not None and session.opened:
            session.edit(text, start, end)

    def publish(self) -> None:
        session = self.session
        if session is None:
            publish('live_words', {'id': None})
            return
        snap = session.snapshot()
        snap['notice'] = getattr(self, '_notice', '')
        snap['cloud'] = self.choice.engine in ('openai', 'gemini')
        snap['label'] = {'local': 'Local words', 'phrases': 'Phrases', 'openai': 'OpenAI',
                         'gemini': 'Gemini'}.get(self.choice.engine, '')
        publish('live_words', snap)
