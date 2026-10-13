# state_manager.py
# Central coordinator that owns the dictation pipeline state and routes events
# between components. Roughly:
#
#   HotkeyListener  ─►  StateManager  ─►  AudioRecorder.stop_recording()
#                            │                       │
#                            ▼                       ▼
#                     WhisperEngine.transcribe ──► ClipboardManager.deliver
#                                                     │
#                                                     ▼
#                                            stats + transcripts + audit
#
# Also owns the LevelOverlay + FallbackWindow lifecycle and the cross-cutting
# concerns: profile activation, app-rule application, AI rephrase, command mode,
# and the continuous-dictation restart loop.
#
# Thread safety: most state lives behind self._state_lock. The audio capture
# thread writes recording-status atomically; everything else mutates state
# only from the main thread or via the lock.

import collections
import logging
import time
from pathlib import Path
import threading
import platform
from typing import Optional

import sounddevice as sd

from .audio_recorder import AudioRecorder
from .whisper_engine import WhisperEngine
from .clipboard_manager import ClipboardManager
from .delayed_send import count_words, wait_seconds, window_identity
from .system_tray import SystemTray
from .config_manager import ConfigManager
from .audio_feedback import AudioFeedback
from .utils import OptionalComponent, get_user_app_data_path
from .voice_activity_detection import VadEvent, VadManager
from .voice_commands import VoiceCommandManager
from .profiles import ProfileManager
from .app_rules import AppRules, formatting_overrides as app_rules_formatting_overrides, effective_postprocess_config
from .streaming_delivery import StreamingDelivery, decide_stream_delivery
from .transforms import TransformsManager
from .text_postprocess import postprocess
from .dictation_cleanup import confident_speech, is_prompt_echo
from .voice_filter import (LEARN_VOICEPRINT_S, VOICEPRINT_MATCH, VoiceLevels, keep_my_voice, my_voice_pieces,
                           speech_level_db)
from .stats import record_transcription
from .audit_log import record as audit_record
from .terminal_title import TerminalTitle
from .transcript_log import record_transcript
from .platform import foreground
from .level_overlay import LevelOverlay
from .fallback_window import FallbackWindow
from .ui.events import publish


# Wraps the tray so every notification also reaches the event bus (the floating
# bar and the app window show them). Everything else passes through, and a
# missing tray still publishes, so a tray-less setup keeps its notifications.
class _NotifyFanout:
    def __init__(self, tray):
        self._tray = tray

    def notify(self, message: str, title: str = "Koli"):
        publish('notify', {'kind': 'info', 'message': message})
        if self._tray is not None:
            self._tray.notify(message, title)

    def __getattr__(self, name):
        if self._tray is None:
            raise AttributeError(name)
        return getattr(self._tray, name)


# A short name for an app rule in the Live trace: its id, else its match list.
def _rule_label(rule) -> Optional[str]:
    if not rule:
        return None
    if rule.get('id'):
        return str(rule['id'])
    match = rule.get('match')
    if isinstance(match, (list, tuple)):
        return ', '.join(str(m) for m in match)
    return str(match) if match else None


class StateManager:
    # ── Recordings nobody started with a key ─────────────────────────────────
    # Auto-dictation sessions are numbered, and each phrase is tied to the
    # session it was spoken in. Phrases are prepared on their own thread, so
    # one can finish after its session ended: `_live_session` is the session
    # whose phrases may still be pasted (None once it is cancelled), and
    # `_phrases_in_flight` counts the ones being prepared. `_live_window` is the
    # window that session listened in: once a session has ended, no cancel
    # reaches it, so a late phrase checks the window itself. `_hands_free` marks
    # a continuous-mode restart. Class defaults keep bare instances valid.
    _auto_session = 0
    _live_session = None
    _live_window = None
    _phrases_in_flight = 0
    _finish_after_phrases = False
    _hands_free = False

    def __init__(self,
                 audio_recorder: AudioRecorder,
                 whisper_engine: WhisperEngine,
                 clipboard_manager: ClipboardManager,
                 config_manager: ConfigManager,
                 vad_manager: VadManager,
                 system_tray: Optional[SystemTray] = None,
                 audio_feedback: Optional[AudioFeedback] = None,
                 voice_command_manager: Optional[VoiceCommandManager] = None,
                 terminal_title: Optional[TerminalTitle] = None):

        self.audio_recorder = audio_recorder
        self.whisper_engine = whisper_engine
        self.clipboard_manager = clipboard_manager
        self.system_tray = OptionalComponent(_NotifyFanout(system_tray))
        self.terminal_title = OptionalComponent(terminal_title)
        self.config_manager = config_manager
        self.audio_feedback = OptionalComponent(audio_feedback)
        self.vad_manager = vad_manager
        self.voice_command_manager = voice_command_manager

        self.is_processing = False
        self.is_model_loading = False
        self.is_paused = False
        self.last_transcription = None
        self.recent_transcriptions = collections.deque(maxlen=10)
        self._rephrase_mode = False
        self._rephrase_selection = ''
        self._rephrase_original_clipboard = ''
        self._continuous_aborted = False
        # True while the running recording was started by auto-dictation (a text
        # field got focus) rather than a hotkey; see auto_dictation.py.
        self._auto_started = False
        # Something was pasted in the running auto-dictation session, so its
        # end may send it (see _finish_auto_session). The words pasted so far
        # size an "Auto" wait before that send.
        self._auto_pasted = False
        self._auto_pasted_words = 0
        self._auto_dictation = None
        self._wake_listener = None   # wake_word.WakeListener, set by main
        # MeetingService, set by main when the app window runs (meeting mode).
        self.meetings = None
        # How loud you speak per microphone, learned from hotkey dictations
        # ("Only my voice"; see voice_filter.py).
        self.voice_levels = VoiceLevels(Path(get_user_app_data_path()) / 'voice_levels.json')
        self._warned_no_vad_for_auto = False
        from .media_control import MediaControl
        self.media_control = MediaControl()
        self._hotkey_listener_ref = None
        self._pending_model_change = None
        self._pending_device_change = None
        self._command_mode = False
        self._state_lock = threading.Lock()
        # One transcription at a time: a phrase still being pasted and the final
        # stop of the same recording must not run the pipeline together.
        self._pipeline_lock = threading.Lock()
        self._streaming_display_active = False
        # Commit-on-endpoint streaming delivery (opt-in). Active only for the
        # current recording when conditions are met; default off → zero impact.
        self._streaming_delivery_active = False
        self.streaming_delivery = None

        self.logger = logging.getLogger(__name__)
        self._current_audio_host = None
        self._initialize_audio_host()
        self.profile_manager = ProfileManager(config_manager)
        self.app_rules = AppRules()
        self.transforms_manager = TransformsManager()
        self.level_overlay = None
        self.fallback_window = FallbackWindow()
        # Set by the UI host: builds the floating bar instead of the Tk overlay.
        self.overlay_factory = None
        # Work that must wait until no recording/transcription is running
        # (engine and recorder swaps from instant-apply settings).
        self._idle_actions = []

    def attach_components(self,
                          audio_recorder: AudioRecorder,
                          system_tray: Optional[SystemTray]):
        self.audio_recorder = audio_recorder
        self.system_tray = OptionalComponent(_NotifyFanout(system_tray))
        self._ensure_audio_device_for_host(self._current_audio_host)

        try:
            self.transforms_manager.system_tray = system_tray
        except Exception:
            pass

        overlay_cfg = self.config_manager.config.get('overlay', {}) or {}
        if overlay_cfg.get('enabled', True) or self.overlay_factory:
            try:
                factory = self.overlay_factory or LevelOverlay
                self.level_overlay = factory(
                    level_provider=lambda: self.audio_recorder.get_current_level(),
                    click_through=overlay_cfg.get('click_through', True),
                    position=overlay_cfg.get('position', 'bottom-center'),
                )
                self.level_overlay.start()
            except Exception as e:
                self.logger.warning(f"Level overlay disabled: {e}")
                self.level_overlay = None
    
    def handle_max_recording_duration_reached(self, audio_data):
        self.logger.info("Max recording duration reached - starting transcription")
        self._transcription_pipeline(audio_data, use_auto_enter=False)

    def handle_vad_event(self, event: VadEvent):
        if event == VadEvent.SPEECH_START:
            # You started talking: now the music can pause (media_control.py).
            self.media_control.on_speech()
            return
        if event == VadEvent.PHRASE_PAUSE:
            # A short pause while auto-dictation listens: paste the words so
            # far and keep the microphone open for the next phrase. The phrase
            # carries its session, read now: the session may end while the
            # audio is still being prepared (each VAD event has its own thread).
            with self._state_lock:
                session = self._auto_session if self._auto_started else None
            if session is None or not self.audio_recorder.get_recording_status():
                return
            self._phrase_started()
            try:
                audio_data = self.audio_recorder.take_recorded_audio()
                self.logger.info("Auto-dictation: phrase pause - delivering what was said so far")
                self._transcription_pipeline(audio_data, use_auto_enter=False, phrase=True, phrase_session=session)
            finally:
                self._phrase_finished()
            return
        if event == VadEvent.SILENCE_TIMEOUT:
            # An auto-started recording in which nobody spoke is not a dictation:
            # drop it without a sound, so a field you merely clicked stays quiet.
            if self._auto_started and not self._detector_heard_speech():
                # A phrase still being prepared is speech too: the session ends
                # after it, as one whose words were all delivered.
                with self._state_lock:
                    waiting = self._phrases_in_flight > 0
                    if waiting:
                        self._finish_after_phrases = True
                if self._auto_pasted or waiting:
                    # Every word was pasted, or is about to be: the session is done.
                    self.logger.info("Auto-dictation: quiet after the last phrase - session done"
                                     + (" (that phrase is still being prepared)" if waiting else ''))
                    print(f"   ✓ Done listening: quiet for {self._active_silence_timeout():g}s")
                    self.cancel_active_recording(quiet=True, keep_phrases=True)
                    if not waiting:
                        self._finish_auto_session()
                    return
                self.logger.info("Auto-dictation: no speech heard - cancelling quietly")
                print(f"   ✗ Listening cancelled: nothing heard for {self._active_silence_timeout():g}s")
                self.cancel_active_recording(quiet=True)
                return
            self.logger.info("VAD silence timeout detected - stopping recording")
            timeout_seconds = int(self._active_silence_timeout())
            self._clear_streaming_display()
            print(f"⏰ Stopping recording after {timeout_seconds} seconds of silence...")
            audio_data = self.audio_recorder.stop_recording()
            self._transcription_pipeline(audio_data, use_auto_enter=False)

    # Called on the audio thread for each streaming result. Updates the overlay
    # preview always; when commit-on-endpoint delivery is active, hands FINALIZED
    # phrases to the delivery worker (never the revising partials).
    def handle_streaming_result(self, text: str, is_final: bool):
        if is_final:
            if self._streaming_display_active:
                print(f"\r   {text:<70}")
                self._streaming_display_active = False
            if self._streaming_delivery_active and self.streaming_delivery:
                self.streaming_delivery.submit_final(text)
        else:
            display_text = text if len(text) < 67 else "..." + text[-64:]
            print(f"\r   {display_text:<70}", end="", flush=True)
            self._streaming_display_active = True
        if text:
            publish('streaming_text', {'text': text, 'final': bool(is_final)})
        if self.level_overlay and text:
            self.level_overlay.set_streaming_text(text)

    def _clear_streaming_display(self):
        if self._streaming_display_active:
            print("\r" + " " * 75 + "\r", end="", flush=True)
            self._streaming_display_active = False
    
    def stop_recording(self, use_auto_enter: bool = False) -> bool:
        currently_recording = self.audio_recorder.get_recording_status()

        if currently_recording:
            self._clear_streaming_display()
            audio_data = self.audio_recorder.stop_recording()
            self._transcription_pipeline(audio_data, use_auto_enter)
            return True
        else:
            return False
    
    # `quiet` skips the cancel sound: auto-dictation cancels often (you typed,
    # or never spoke) and a chime each time would be noise.
    # `keep_phrases`: the session ended normally (quiet after its last phrase),
    # so a phrase still being prepared may be pasted. Any other cancel (typing,
    # leaving the field, Esc, nothing heard) drops it: it has nowhere safe to go.
    def cancel_active_recording(self, quiet: bool = False, keep_phrases: bool = False):
        self._clear_streaming_display()
        # Tear down the streaming-delivery worker (already-typed text stays — you
        # can't un-type live output, same as Wispr).
        if self.streaming_delivery:
            try:
                self.streaming_delivery.stop()
            except Exception:
                pass
            self.streaming_delivery = None
        self._streaming_delivery_active = False
        with self._state_lock:
            self._command_mode = False
            self._rephrase_mode = False
            self._rephrase_selection = ''
        self._continuous_aborted = True
        self._auto_started = False
        self._hands_free = False
        if not keep_phrases:
            with self._state_lock:
                self._live_session = None
                self._live_window = None
        self.audio_recorder.cancel_recording()
        if not quiet:
            self.audio_feedback.play_cancel_sound()
        self._update_ui_state("idle")
        if self.level_overlay:
            self.level_overlay.hide()
    
    # Esc: cancels the recording, or, with none running, a send that is
    # waiting to press Enter.
    def cancel_recording_hotkey_pressed(self) -> bool:
        current_state = self.get_current_state()

        if current_state == "recording":
            print("🎤 Recording cancelled!")
            self.cancel_active_recording()
            return True
        return self._cancel_pending_send('Esc')

    # Whether this dictation ends with Enter, whether that Enter may wait, and
    # for how long: the rule's own auto_send_delay, or None for the main
    # clipboard.auto_send_delay. Only the automatic kind waits: an app rule's
    # auto-send. The send key is a deliberate "send it", so it sends at once,
    # and a rule saying auto_send: false wins over everything. Auto-dictation
    # never sends: it hears whatever is around, and each pause would be a
    # message of its own.
    @staticmethod
    def _auto_send_plan(use_auto_enter: bool, rule, auto_dictation: bool = False) -> tuple:
        if auto_dictation:
            return False, False, None
        auto_enter, may_wait, wait = bool(use_auto_enter), False, None
        if rule:
            if rule.get('auto_send') is True:
                may_wait = not use_auto_enter
                auto_enter = True
            elif rule.get('auto_send') is False:
                auto_enter = False
            if may_wait and rule.get('auto_send_delay') is not None:
                wait = rule['auto_send_delay']
        return auto_enter, may_wait, wait

    # A send waiting to press Enter is stopped by anything that starts a new
    # message, so two messages never get mixed. Hands-free restarts don't call
    # this (they go straight to _begin_recording).
    def _cancel_pending_send(self, reason: str) -> bool:
        cb = getattr(self, 'clipboard_manager', None)
        ds = getattr(cb, 'delayed_send', None) if cb is not None else None
        if ds is None:
            return False
        try:
            return bool(ds.cancel(reason))
        except Exception as e:
            self.logger.debug(f"Pending send not cancelled: {e}")
            return False

    def start_recording(self):
        self._cancel_pending_send('you started a new dictation')
        if not self.can_start_recording():
            current_state = self.get_current_state()
            if self.is_processing:
                print("⏳ Still processing previous recording...")
            elif self.is_model_loading:
                print("⏳ Still loading model...")
            else:
                print(f"⏳ Cannot record while {current_state}...")
            return

        self._begin_recording()

    def set_paused(self, paused: bool):
        self.is_paused = paused
        if paused and self.audio_recorder.get_recording_status():
            self.cancel_active_recording()
        self.system_tray.notify("Hotkeys paused" if paused else "Hotkeys resumed")
        self.system_tray.refresh_menu()  # the tray's Pause/Resume label follows
        self._publish_state(self.get_current_state())

    def start_rephrase_recording(self):
        self._cancel_pending_send('you started a new dictation')
        if not self.can_start_recording():
            return
        import time
        import pyperclip
        from .platform import keyboard as kb

        try:
            self._rephrase_original_clipboard = pyperclip.paste()
        except Exception:
            self._rephrase_original_clipboard = ''

        kb.send_hotkey('ctrl', 'c')
        time.sleep(0.12)
        try:
            selection = pyperclip.paste()
        except Exception:
            selection = ''
        try:
            pyperclip.copy(self._rephrase_original_clipboard)
        except Exception:
            pass

        if not selection or selection == self._rephrase_original_clipboard:
            print("\n   ✗ Nothing selected — rephrase needs selected text. Recording dictation instead.")
            self._rephrase_selection = ''
        else:
            self._rephrase_selection = selection
            print(f"\n🎙  Rephrase mode: selection captured ({len(selection)} chars)")
            print("   Speak instructions, then release to apply.")

        self._rephrase_mode = True
        self._streaming_delivery_active = False  # rephrase is never live-typed
        self.streaming_delivery = None
        if self.audio_recorder.start_recording():
            self.audio_feedback.play_start_sound()
            self._update_ui_state("recording")
            if self.level_overlay:
                self.level_overlay.show_recording()

    def start_command_recording(self):
        self._cancel_pending_send('you started a new dictation')
        if not self.can_start_recording():
            return

        with self._state_lock:
            self._command_mode = True
        self._streaming_delivery_active = False  # command mode is never live-typed
        self.streaming_delivery = None

        self.logger.info("Starting command mode recording")
        success = self.audio_recorder.start_recording()
        if success:
            print("\n🎤 Command mode activated! Speak a command...")
            self.config_manager.print_command_stop_instructions()
            self.audio_feedback.play_start_sound()
            self._update_ui_state("recording")
            if self.level_overlay:
                self.level_overlay.show_recording()

    # `hands_free`: a continuous-mode restart, not a key you pressed (see
    # _hands_free above).
    def _begin_recording(self, auto_started: bool = False, hands_free: bool = False):
        self._apply_recording_context()
        self._maybe_pause_media()
        silence_timeout = self._auto_silence_timeout() if auto_started else None
        phrase_pause = self._auto_phrase_pause() if auto_started else None
        success = self.audio_recorder.start_recording(silence_timeout=silence_timeout, phrase_pause=phrase_pause)

        if success:
            self._auto_started = auto_started
            self._hands_free = hands_free and not auto_started
            self._auto_pasted = False
            self._auto_pasted_words = 0
            if auto_started:
                window = window_identity(foreground.get_foreground_app() or {})
                with self._state_lock:
                    self._auto_session += 1
                    self._live_session = self._auto_session
                    self._live_window = window
                    self._finish_after_phrases = False
            # Only spin up the streaming-delivery worker once recording is
            # confirmed — otherwise a failed start would leak the worker thread.
            self._setup_streaming_delivery()
            if auto_started:
                # Same console story as a hotkey start, only the chime is skipped.
                self.logger.info(f"Auto-dictation: listening (stops after {silence_timeout:g}s of silence"
                                 + (f", pastes after each {phrase_pause:g}s pause)" if phrase_pause else ")"))
                print("\n🎤 Listening — a text field got focus. Speak now...")
                if phrase_pause:
                    print(f"   Each {phrase_pause:g}s pause pastes what you said and keeps listening. "
                          f"Quiet for {silence_timeout:g}s, typing or leaving the field: done.")
                else:
                    print(f"   Quiet for {silence_timeout:g}s: cancelled. Pause after speaking: transcribed. Typing or leaving the field: cancelled.")
                if not getattr(self.audio_recorder, 'continuous_vad', None) and not self._warned_no_vad_for_auto:
                    self._warned_no_vad_for_auto = True
                    self.logger.warning("Auto-dictation: 'Stop on silence' is off, so only typing or a hotkey ends the recording")
                    print("   ⚠ 'Stop on silence' is off, so only typing or a hotkey ends this recording.")
            else:
                print("\n🎤 Recording started! Speak now...")
                self.config_manager.print_stop_instructions_based_on_config()
                self.audio_feedback.play_start_sound()  # auto starts stay silent
            self._update_ui_state("recording")
            if self.level_overlay:
                self.level_overlay.show_recording()

    # ── Auto-dictation (a focused text field starts the recording) ───────────
    # The coordinator in auto_dictation.py decides WHEN; these are the only
    # entry points it uses, so the policy stays out of the pipeline.
    def set_auto_dictation(self, coordinator):
        self._auto_dictation = coordinator

    def begin_auto_recording(self) -> bool:
        if not self.can_start_recording() or self.is_paused:
            return False
        # Focus moved to a field: a waiting Enter would land there instead.
        self._cancel_pending_send('a text field got focus')
        self._begin_recording(auto_started=True)
        return self._auto_started

    def is_auto_recording(self) -> bool:
        return bool(self._auto_started) and self.audio_recorder.get_recording_status()

    # Typing means "I'll write it myself": the audio is dropped, quietly.
    def cancel_auto_recording(self, reason: str = '') -> bool:
        if not self.is_auto_recording():
            return False
        self.logger.info(f"Auto-dictation: cancelled ({reason or 'cancelled'})")
        print(f"   ✗ Listening cancelled: {reason or 'cancelled'}")
        self._auto_pasted = False   # typing or leaving the field: nothing is sent
        self._auto_pasted_words = 0
        self.cancel_active_recording(quiet=True)
        return True

    # ── Wake word ("Hey Koli, next song") ────────────────────────────────────
    # wake_word.py decides WHEN a command was asked for; this runs it through
    # the same path as the command hotkey. Returns wake_word's RAN, MISSED,
    # EMPTY (only the wake word was said) or BUSY.
    def set_wake_listener(self, listener):
        self._wake_listener = listener

    # The language a wake check or command is heard in: the one in Settings,
    # not the last dictation's app rule ("auto" detects within your list).
    def wake_language(self) -> str:
        try:
            return (self.config_manager.get_whisper_config() or {}).get('language') or 'auto'
        except Exception:
            return 'auto'

    # `audio` is transcribed (or `text` used as heard), the wake word at its
    # start dropped, and the rest matched. `only_media`: a follow-up after
    # "Hey Koli", where anything audible could be a command. `report_miss`:
    # a first command says "Didn't catch that"; a follow-up misses quietly.
    # `can_retry`: Koli listens for one more try after this miss, so the
    # bubble asks you to say it again.
    def run_wake_command(self, audio=None, text=None, only_media: bool = False, report_miss: bool = True,
                         can_retry: bool = False) -> str:
        from .voice_commands import is_media_command
        from .wake_word import BUSY, EMPTY, MISSED, RAN, match_wake
        from .utils import normalize_spoken
        # Checked and claimed together, so a hotkey pressed in between can't
        # start a dictation alongside the command.
        with self._state_lock:
            if self.is_processing or self.is_model_loading or self.is_paused \
                    or self.audio_recorder.get_recording_status():
                return BUSY
            self.is_processing = True
        try:
            if text is None:
                if report_miss:
                    self._publish_state('processing')
                text = self.whisper_engine.transcribe_audio(
                    audio, language=self.wake_language(), use_prompt=False, task='transcribe',
                    quiet=not report_miss) or ''
            listener = self._wake_listener
            if listener is not None:
                rest = match_wake(text, listener.spellings(), listener.match_ratio())
                if rest is not None:
                    text = rest
            if not normalize_spoken(text):
                return EMPTY
            # "Hey Koli" is anyone's (a TV, a video): a command that runs a
            # program asks first. Your own word was checked against your voice.
            personal = bool(listener is not None and listener.personal())
            matched = self._handle_command_transcription(
                text, only=is_media_command if only_media else None, announce_miss=report_miss,
                confirm_runs=not personal)
            if matched or report_miss:
                self._emit_pipeline('command', mode='wake', raw=text, matched=bool(matched))
            if not matched and report_miss and can_retry:
                print("   ↻ Say the command again (no wake word needed)")
            if self.level_overlay:
                if matched:
                    self.level_overlay.flash_success()
                elif report_miss:
                    self.level_overlay.flash_failure("Didn't catch that - say it again" if can_retry
                                                     else "Didn't catch that")
            return RAN if matched else MISSED
        finally:
            with self._state_lock:
                self.is_processing = False
            if report_miss:
                self._publish_state(self.get_current_state())
            self._run_idle_actions()

    # ── Only my voice ────────────────────────────────────────────────────────

    def _voice_filter_config(self) -> dict:
        audio = (getattr(self.config_manager, 'config', None) or {}).get('audio') or {}
        cfg = audio.get('voice_filter')
        return cfg if isinstance(cfg, dict) else {}

    # The learned level belongs to a microphone: another mic sits elsewhere.
    def _mic_key(self) -> str:
        try:
            name = self.audio_recorder.describe().get('name')
        except Exception:
            name = None
        return name if isinstance(name, str) and name else 'default'

    # How much quieter than you a voice may be and still count as yours (dB).
    def _voice_margin_db(self) -> float:
        try:
            return float(self._voice_filter_config().get('margin_db', 12))
        except (TypeError, ValueError):
            return 12.0

    # The loudness (dBFS) your speech reaches on this microphone, less the
    # margin: quieter speech is a TV or someone else, not you starting to talk.
    # None until Koli has learned your level from 3 hotkey dictations.
    def _my_voice_floor_db(self):
        levels = getattr(self, 'voice_levels', None)
        mine = levels.my_level(self._mic_key()) if levels is not None else None
        return None if mine is None else mine - self._voice_margin_db()

    # Learned on the raw microphone, like the meter under How much quieter and
    # the detector's live level: noise reduction doesn't move your level.
    def _learn_my_level(self, audio) -> None:
        levels = getattr(self, 'voice_levels', None)
        if levels is None:
            return
        level = speech_level_db(self._raw_audio(audio))
        if level is not None:
            levels.learn(self._mic_key(), level)

    # ── Your voiceprint per microphone (Only my voice, step 2) ───────────────
    # Kept by the meeting service's voice store, one per microphone name.

    def _voiceprint_on(self) -> bool:
        return getattr(self, 'meetings', None) is not None \
            and self._voice_filter_config().get('voiceprint', True) is not False

    # The microphone as it was for `audio`: noise reduction and the Whisper-mode
    # boost make any voice (a TV) score like yours, so voiceprints use this.
    def _raw_audio(self, audio):
        try:
            raw = self.audio_recorder.raw_for(audio)
        except Exception:
            return audio
        return raw if raw is not None else audio

    def _learn_my_voiceprint(self, audio) -> None:
        if not self._voiceprint_on() or audio is None or len(audio) / 16000 < LEARN_VOICEPRINT_S:
            return
        try:
            self.meetings.learn_my_voice(self._raw_audio(audio), self._mic_key())   # one voiceprint per microphone
        except Exception as e:
            self.logger.debug(f"Voiceprint not learned: {e}")

    # True unless your voice on this microphone is known and this recording
    # clearly doesn't sound like it (another person, the TV, noise). Every
    # result is logged: a check that silently passed is how a TV got through.
    def _sounds_like_me(self, audio) -> bool:
        if not self._voiceprint_on():
            return True
        try:
            similarity = self.meetings.my_voice_similarity(self._raw_audio(audio), self._mic_key())
        except Exception as e:
            self.logger.info(f"Auto-dictation: voiceprint not checked ({e})")
            return True
        if similarity is None:
            self.logger.info("Auto-dictation: voiceprint not checked (your voice on this microphone isn't learned "
                             "yet - 3 hotkey dictations - or the voice model is still loading)")
            return True
        if similarity >= VOICEPRINT_MATCH:
            self.logger.info(f"Auto-dictation: sounds like you ({similarity:.2f}, needs {VOICEPRINT_MATCH})")
            return True
        self.logger.info(f"Auto-dictation: doesn't sound like you on this microphone ({similarity:.2f}, "
                         f"needs {VOICEPRINT_MATCH})")
        return False

    # ── Settings: "Does Koli recognize you?" ─────────────────────────────────
    # `seconds` from the open microphone (nothing is recorded or typed), judged
    # the way auto-dictation judges a phrase: is it speech, is it about as loud
    # as you, does it sound like you. The verdict is 'type', 'leave_out' or
    # 'no_speech', with a sentence saying why.
    def check_my_voice(self, seconds: float = 5) -> dict:
        if self.audio_recorder.get_recording_status():
            raise ValueError("A recording is running: finish it, then check again")
        raw = self.audio_recorder.capture_raw(seconds)
        if raw is None or not len(raw):
            raise ValueError("The microphone sent nothing. Is it muted?")
        mic = self._mic_key()
        speech = bool(self.vad_manager.check_audio_for_speech(raw))
        loudness = self._loudness_check(raw, mic)
        voiceprint = self._voiceprint_check(raw, mic)
        if not speech:
            verdict, reason = 'no_speech', "No speech heard: auto-dictation would stay quiet"
        elif voiceprint['status'] == 'no_match':
            verdict, reason = 'leave_out', "It doesn't sound like you on this microphone: auto-dictation leaves it out"
        elif loudness['status'] == 'quieter' and voiceprint['status'] == 'match':
            verdict, reason = 'type', "Quieter than you usually are, but it sounds like you: auto-dictation would type this"
        elif loudness['status'] == 'quieter':
            verdict, reason = 'leave_out', "It's much quieter than you: auto-dictation leaves it out"
        else:
            verdict, reason = 'type', "Auto-dictation would type this"
            if voiceprint['status'] in ('not_learned', 'loading'):
                reason += ", without checking your voice yet"
        self.logger.info(f"Voice check on {mic}: {verdict} (voiceprint {voiceprint['status']} "
                         f"{voiceprint.get('score')}, level {loudness.get('level_db')} dB vs yours "
                         f"{loudness.get('mine_db')} dB)")
        return {'verdict': verdict, 'reason': reason, 'speech': speech, 'mic': mic,
                'loudness': loudness, 'voiceprint': voiceprint}

    # Your learned speaking level on the microphone in use (None until 3
    # hotkey dictations), for the "Set it by ear" meter in Settings.
    def my_voice_level(self) -> dict:
        mic = self._mic_key()
        levels = getattr(self, 'voice_levels', None)
        mine = levels.my_level(mic) if levels is not None else None
        return {'mic': mic, 'mine_db': None if mine is None else round(mine, 1)}

    # Is `audio` about as loud as you on `mic`: 'mine', 'quieter', or why it
    # can't be told ('off', 'not_learned', 'silent').
    def _loudness_check(self, audio, mic: str) -> dict:
        levels = getattr(self, 'voice_levels', None)
        mine = levels.my_level(mic) if levels is not None else None
        margin = self._voice_margin_db()
        level = speech_level_db(audio)
        if not self._voice_filter_config().get('quieter_than_me', True):
            status = 'off'
        elif mine is None:
            status = 'not_learned'
        elif level is None:
            status = 'silent'
        else:
            status = 'mine' if level >= mine - margin else 'quieter'
        return {'status': status, 'margin_db': margin,
                'level_db': None if level is None else round(level, 1),
                'mine_db': None if mine is None else round(mine, 1)}

    # Does `audio` sound like you on `mic`: 'match', 'no_match', or why it
    # can't be told ('off', 'not_learned', 'loading').
    def _voiceprint_check(self, audio, mic: str) -> dict:
        if not self._voiceprint_on():
            return {'status': 'off', 'needs': VOICEPRINT_MATCH}
        status = self.meetings.my_voice_status(audio, mic)
        if not status.get('ready'):
            kind = 'loading'
        elif status.get('learned', 0) < status.get('needed', 0):
            kind = 'not_learned'
        elif status.get('score') is None:
            kind = 'loading'
        else:
            kind = 'match' if status['score'] >= VOICEPRINT_MATCH else 'no_match'
        return {**status, 'status': kind, 'needs': VOICEPRINT_MATCH}

    # Drops the pieces of a recording much quieter than your learned level.
    # Returns the text left and the engine info with only the kept pieces, so
    # later checks don't bring a dropped piece back.
    def _keep_my_voice(self, text: str, audio, info: dict, auto_session: bool) -> tuple:
        cfg = self._voice_filter_config()
        levels = getattr(self, 'voice_levels', None)
        if levels is None or not cfg.get('quieter_than_me', True):
            return text, info
        if cfg.get('apply_to', 'auto') != 'all' and not auto_session:
            return text, info
        mine = levels.my_level(self._mic_key())
        if mine is None:
            return text, info   # not enough hotkey dictations yet to know your level
        margin = self._voice_margin_db()
        # The raw clip is cut where this one is, so Whisper's piece times fit
        # it; it has no Whisper-mode boost to take back out.
        raw = self._raw_audio(audio)
        heard_text, heard_info = text, info
        pieces = info.get('segments')
        if pieces:
            pieces, why = my_voice_pieces(raw, pieces, mine, margin, 1.0)
            if why:
                text = ''.join(p.get('text', '') for p in pieces).strip()
                info = {**info, 'segments': pieces}
        else:
            text, why = keep_my_voice(text, raw, None, mine, margin, 1.0)
        # Loudness would drop the whole phrase: your voiceprint, which tells
        # you from music or a TV, gets the last word. Only for the whole
        # phrase - next to you, a quiet part could still be the TV.
        if why and not text and self._voiceprint_says_me(audio):
            self.logger.info(f"Only my voice: {why} (your level {mine:.0f} dB), "
                             f"but it sounds like you - kept")
            print("   ✓ Kept: quieter than you usually are, but it sounds like you")
            return heard_text, heard_info
        if why:
            self.logger.info(f"Only my voice: {why} (your level {mine:.0f} dB)")
            print(f"   ✗ Not your voice: {why}" if not text else f"   ✓ Kept your voice ({why})")
        return text, info

    # True only when your voiceprint on this microphone is known and `audio`
    # matches it: unknown or unchecked is no proof, unlike _sounds_like_me.
    def _voiceprint_says_me(self, audio) -> bool:
        if not self._voiceprint_on():
            return False
        try:
            similarity = self.meetings.my_voice_similarity(self._raw_audio(audio), self._mic_key())
        except Exception as e:
            self.logger.info(f"Only my voice: voiceprint not checked ({e})")
            return False
        if similarity is None:
            self.logger.info("Only my voice: voiceprint not checked (not learned on this microphone yet)")
            return False
        says = similarity >= VOICEPRINT_MATCH
        verdict = 'sounds like you' if says else "doesn't sound like you"
        self.logger.info(f"Only my voice: {verdict} ({similarity:.2f}, needs {VOICEPRINT_MATCH})")
        return says

    def _phrase_started(self) -> None:
        with self._state_lock:
            self._phrases_in_flight += 1

    # A phrase is done (pasted or dropped). If its session already ended
    # quietly, waiting for it, the session as a whole may now be sent.
    def _phrase_finished(self) -> None:
        with self._state_lock:
            self._phrases_in_flight = max(0, self._phrases_in_flight - 1)
            finish = self._finish_after_phrases and self._phrases_in_flight == 0
            if finish:
                self._finish_after_phrases = False
        if finish:
            self._finish_auto_session()

    # Why a phrase may not be pasted any more, or '' when it may: its session
    # was cancelled while it was being prepared, or it ended and you have
    # since moved to another window.
    def _phrase_drop_reason(self, phrase_session) -> str:
        with self._state_lock:
            live, window = self._live_session, self._live_window
        if phrase_session is None or phrase_session != live:
            return 'its session was cancelled before it was ready'
        if window is not None:
            now = window_identity(foreground.get_foreground_app() or {})
            if now is not None and now != window:
                return 'you moved to another window before it was ready'
        return ''

    # A finished auto-dictation session that pasted something is sent once,
    # by the app rule's auto-send, after its wait (or the main one) so a stray
    # message can still be stopped. An "Auto" wait counts every word the
    # session pasted. No wait set: it is left unsent.
    def _finish_auto_session(self) -> None:
        pasted, self._auto_pasted = self._auto_pasted, False
        words, self._auto_pasted_words = getattr(self, '_auto_pasted_words', 0), 0
        if not pasted:
            return
        try:
            rule = self.app_rules.match_for_foreground() or {}
        except Exception:
            rule = {}
        if rule.get('auto_send') is not True:
            return
        wait = rule.get('auto_send_delay')
        if wait is None:
            wait = getattr(self.clipboard_manager, 'auto_send_delay', 0)
        wait = wait_seconds(wait, words)
        delayed = getattr(self.clipboard_manager, 'delayed_send', None)
        if wait <= 0 or delayed is None:
            self.logger.info("Auto-dictation: not sending - auto-send needs a wait so it can be stopped")
            print("   ↵ Not sent: set a wait before sending for this app to send automatically")
            return
        self.logger.info(f"Auto-dictation: session done - sending in {wait:g}s")
        print(f"   ↵ Sending in {wait:g}s (Esc, typing or Cancel stops it)")
        delayed.schedule(wait)

    def _auto_silence_timeout(self) -> float:
        cfg = self.config_manager.config.get('auto_dictation') or {}
        try:
            return max(1.0, float(cfg.get('silence_timeout_seconds', 5)))
        except (TypeError, ValueError):
            return 5.0

    # Seconds of silence after speech that paste a phrase mid-recording; 0 = off.
    def _auto_phrase_pause(self) -> float:
        cfg = self.config_manager.config.get('auto_dictation') or {}
        try:
            return max(0.0, float(cfg.get('phrase_pause_seconds', 0) or 0))
        except (TypeError, ValueError):
            return 0.0

    def _active_silence_timeout(self) -> float:
        detector = getattr(self.audio_recorder, 'continuous_vad', None)
        if detector is not None:
            return float(getattr(detector, 'silence_timeout_sec', self.vad_manager.vad_silence_timeout_seconds))
        return float(self.vad_manager.vad_silence_timeout_seconds)

    # Is there speech that has not been delivered yet? With phrase delivery the
    # pasted phrases no longer count, so a session that ends after its last
    # phrase ends quietly instead of transcribing silence.
    def _detector_heard_speech(self) -> bool:
        detector = getattr(self.audio_recorder, 'continuous_vad', None)
        if detector is None:
            return True
        if getattr(detector, 'phrase_pause_sec', 0):
            return bool(getattr(detector, 'speech_since_phrase', True))
        return bool(getattr(detector, 'had_speech', True))

    # Decide whether this (plain dictation) recording should stream finalized
    # phrases to the cursor live, and if so spin up the delivery worker. Only the
    # plain-dictation path calls this; command/rephrase never stream-deliver.
    def _setup_streaming_delivery(self):
        self._streaming_delivery_active = False
        self.streaming_delivery = None
        streaming_available = bool(getattr(self.audio_recorder, 'continuous_streaming', None))
        active = decide_stream_delivery(
            self.config_manager.get_streaming_config(),
            streaming_available,
            bool(self.clipboard_manager.auto_paste),
            self._foreground_is_textable(),
            self.app_rules.match_for_foreground(),
        )
        if not active:
            return
        self.streaming_delivery = StreamingDelivery(
            deliver_fn=lambda seg: self.clipboard_manager.deliver_transcription(seg, use_auto_enter=False)
        )
        self.streaming_delivery.start()
        self._streaming_delivery_active = True
        self.logger.info("Streaming commit-on-endpoint delivery active for this recording")

    # Flush the trailing in-progress phrase, wait for the worker to finish typing
    # everything, then record + (optionally) press Enter. Returns the delivered
    # text, or '' if nothing was finalized (caller then falls back to Whisper).
    def _finalize_streaming_delivery(self, use_auto_enter: bool, duration: float) -> str:
        sd = self.streaming_delivery
        self._streaming_delivery_active = False
        self.streaming_delivery = None
        if sd is None:
            return ''

        # Deliver the un-endpointed remainder, if any.
        try:
            cont = getattr(self.audio_recorder, 'continuous_streaming', None)
            if cont:
                trailing = cont.finalize()
                if trailing:
                    sd.submit_final(trailing)
        except Exception as e:
            self.logger.debug(f"Streaming flush failed: {e}")

        text = sd.stop()  # drains + joins the worker: all text is on screen now
        if not text:
            return ''

        # If a segment failed to type, some words may be missing. We can't re-run
        # Whisper (it would duplicate what's already typed), so warn instead.
        if sd.had_failure:
            self.logger.warning("Streaming delivery had a failed segment; some words may be missing")
            self.system_tray.notify("Some dictated words may not have typed — please review.")

        if use_auto_enter:   # the send key: always at once
            self.clipboard_manager.send_enter_key()

        self.last_transcription = text
        self.recent_transcriptions.appendleft(text)
        self.system_tray.refresh_menu()
        self.audio_feedback.play_transcription_complete_sound()
        if self.level_overlay:
            self.level_overlay.flash_success()

        fg = foreground.get_foreground_app() or {}
        record_transcription(char_count=len(text), duration_seconds=duration, app=fg.get('exe', ''))
        record_transcript(text, window=fg, duration_s=duration)
        audit_enabled = (self.config_manager.config.get('audit') or {}).get('enabled', False)
        audit_record('delivered', text, fg.get('exe', ''), audit_enabled)

        self.logger.info(f"Streaming delivery complete: {len(text)} chars")
        self._maybe_restart_continuous()
        return text

    def _maybe_restart_continuous(self):
        audio_cfg = self.config_manager.config.get('audio', {})
        if not audio_cfg.get('continuous_mode', False) or self.is_paused:
            return
        self._continuous_aborted = False
        import threading
        def restart():
            import time
            time.sleep(0.6)
            if self.is_paused or self._continuous_aborted:
                self.logger.info("Continuous mode: restart aborted")
                return
            if not self.audio_recorder.get_recording_status():
                self.logger.info("Continuous mode: auto-restarting recording")
                self._begin_recording(hands_free=True)
        threading.Thread(target=restart, daemon=True, name='continuous-restart').start()

    # Pause the music and/or lower the volume, as the audio settings say
    # (media_control.py); both come back when the recording ends. With voice
    # detection running they wait until you actually speak, at your own level,
    # so a field that starts auto-dictation or a noise in the room doesn't
    # quiet the computer for nothing.
    def _maybe_pause_media(self):
        audio_cfg = self.config_manager.config.get('audio', {})
        detector = getattr(self.audio_recorder, 'continuous_vad', None)
        try:
            if detector is not None:
                # Your level less the margin: quieter speech is silence to the
                # detector. "Ignore voices quieter than mine" off: any speech.
                gate = self._my_voice_floor_db() if self._voice_filter_config().get('quieter_than_me', True) else None
                detector.set_voice_start_level(gate)
            self.media_control.on_recording_start(audio_cfg, hears_voice=detector is not None)
        except Exception as e:
            self.logger.warning(f"Media control skipped: {e}")

    def _apply_recording_context(self):
        whisper_cfg = self.config_manager.get_whisper_config()
        prompt_parts = []
        base_prompt = whisper_cfg.get('initial_prompt') or ''
        if base_prompt:
            prompt_parts.append(base_prompt)
        language = whisper_cfg.get('language') or 'auto'
        task = whisper_cfg.get('task') or 'transcribe'

        try:
            rule = self.app_rules.match_for_foreground() or {}
        except Exception:
            rule = {}
        if rule.get('initial_prompt'):
            prompt_parts.append(str(rule['initial_prompt']))
            self.logger.debug(f"App rule {rule.get('match')} → initial_prompt applied")
        if rule.get('language'):
            language = rule['language']
        if rule.get('task'):
            task = rule['task']

        if whisper_cfg.get('prompt_from_selection', False):
            sel = self._grab_selection_text()
            if sel:
                prompt_parts.append(sel)
                self.logger.info(f"Selection seed applied ({len(sel)} chars)")

        combined_prompt = ' '.join(prompt_parts).strip()
        try:
            self.whisper_engine.initial_prompt = combined_prompt or None
            self.whisper_engine.language = None if language == 'auto' else language
            self.whisper_engine.task = task if task in ('transcribe', 'translate') else 'transcribe'
        except Exception as e:
            self.logger.debug(f"Engine context update failed: {e}")

    def _grab_selection_text(self) -> str:
        try:
            import time
            import pyperclip
            from .platform import keyboard as kb
            original = pyperclip.paste()
            kb.send_hotkey('ctrl', 'c')
            time.sleep(0.08)
            selection = pyperclip.paste()
            try: pyperclip.copy(original)
            except Exception: pass
            if selection and selection != original:
                return selection[-200:].replace('\n', ' ')
        except Exception as e:
            self.logger.debug(f"Selection grab failed: {e}")
        return ''
    
    # Tell the user, once per app per session, that a per-app rule made this
    # delivery copy-only. Without it the app looks broken in exactly the place
    # the rule is most useful — a code editor, where the shipped default is
    # copy-only so dictation never types into source by surprise.
    def _announce_copy_only(self, rule):
        try:
            seen = self._copy_only_announced
        except AttributeError:
            seen = self._copy_only_announced = set()
        # Identify by the rule's own match pattern: it is always present,
        # unlike the foreground exe, which isn't looked up until later in
        # the pipeline.
        match = rule.get('match')
        key = str(match)
        if key in seen:
            return
        seen.add(key)
        where = ', '.join(match) if isinstance(match, (list, tuple)) else (match or 'this app')
        self.system_tray.notify(
            f"Copied, not pasted — your app rule for {where} is copy-only. "
            "Press Ctrl+V, or edit app_rules.yaml to change it.")
        self.logger.info(f"Copy-only delivery for {where} (app rule)")

    # The heart of the app: everything between "user released the hotkey" and
    # "text is in their editor". Runs on a worker thread so the hotkey listener
    # never blocks. Order matters — transcribe, then branch by mode (command /
    # rephrase / dictation), then apply app rules + post-processing, then choose a
    # delivery route (suppressed, fallback window, or paste at cursor), and only
    # record stats/history once delivery actually succeeded. Every early return
    # is a legitimate "nothing to deliver" case and must still clear state via
    # the finally block.
    # `phrase` is a mid-recording run (auto-dictation phrase delivery): the
    # chunk is transcribed and pasted with a trailing space, silently, and the
    # recording state, sounds and idle hand-off are left alone. `phrase_session`
    # is the auto-dictation session it was spoken in.
    def _transcription_pipeline(self, audio_data, use_auto_enter: bool = False, phrase: bool = False,
                                phrase_session=None):
        with self._pipeline_lock:
            with self._state_lock:
                ends_auto_session = not phrase and bool(self._auto_started)
            self._transcription_pipeline_locked(audio_data, use_auto_enter, phrase, phrase_session)
            # The last words of an auto-dictation session (or none, after an
            # echo was dropped): the session as a whole may now be sent.
            if ends_auto_session:
                self._finish_auto_session()

    # Says so and returns True when a phrase may no longer be pasted (see
    # _phrase_drop_reason): it would land somewhere you didn't dictate into.
    def _drop_stale_phrase(self, phrase: bool, phrase_session, mode: str) -> bool:
        why = self._phrase_drop_reason(phrase_session) if phrase else ''
        if not why:
            return False
        self.logger.info(f"Auto-dictation: a phrase was dropped - {why}")
        print(f"   ✗ Phrase not typed: {why}")
        self._emit_pipeline('empty', mode=mode)
        return True

    def _transcription_pipeline_locked(self, audio_data, use_auto_enter: bool = False, phrase: bool = False,
                                       phrase_session=None):
        mode = 'dictate'
        try:
            with self._state_lock:
                self.is_processing = True
                # A phrase always comes from an auto-dictation session, even when
                # that session ended while the phrase was being prepared.
                auto_session = phrase or bool(self._auto_started)   # read before the final run clears it
                hands_free = False
                if not phrase:
                    hands_free = self._hands_free
                    self._auto_started = False
                    self._hands_free = False
                command_mode = self._command_mode
                self._command_mode = False
                rephrase_mode = self._rephrase_mode
                rephrase_selection = self._rephrase_selection
                self._rephrase_mode = False
                self._rephrase_selection = ''
            mode = 'command' if command_mode else ('rephrase' if rephrase_mode and rephrase_selection else 'dictate')

            if not phrase:
                self.audio_feedback.play_stop_sound()

            if audio_data is None:
                if phrase:
                    return  # nothing in this pause worth a word
                self._emit_pipeline('no_audio', mode=mode)
                self.system_tray.notify("No audio captured — was the mic muted?")
                # Flash the overlay too — tray balloons are often suppressed by Windows.
                if self.level_overlay:
                    self.level_overlay.flash_failure("No audio — mic muted?")
                return
            if self._drop_stale_phrase(phrase, phrase_session, mode):
                return

            duration = self.audio_recorder.get_audio_duration(audio_data)
            self._emit_pipeline('captured', mode=mode, duration=round(duration, 2))

            # Commit-on-endpoint streaming: if finalized phrases were already typed
            # live during this recording, flush the trailing phrase, record it, and
            # skip the (now redundant) Whisper pass. Falls through to Whisper if
            # nothing was actually finalized (e.g. a very short utterance).
            if self._streaming_delivery_active and self.streaming_delivery:
                streamed = self._finalize_streaming_delivery(use_auto_enter, duration)
                if streamed:
                    self._emit_pipeline('delivered', mode=mode, outcome='streamed', text=streamed,
                                        duration=round(duration, 2))
                    return

            print(f"   ✓ Recorded {duration:.1f} seconds, transcribing...")

            if not phrase:
                self._update_ui_state("processing")
                if self.level_overlay:
                    self.level_overlay.show_processing()

            self._emit_pipeline('transcribing', mode=mode)
            transcribed_text = self.whisper_engine.transcribe_audio(audio_data)
            info = dict(getattr(self.whisper_engine, 'last_info', None) or {})

            if not transcribed_text:
                self._emit_pipeline('empty', mode=mode, info=info)
                if phrase:
                    return  # a pause with nothing in it; keep listening
                self.system_tray.notify("Transcription was empty (silence or noise only).")
                if self.level_overlay:
                    self.level_overlay.flash_failure("No speech detected")
                return

            self._emit_pipeline('transcribed', mode=mode, raw=transcribed_text, info=info)

            # Noise that Whisper filled with its own prompt is not speech.
            if is_prompt_echo(transcribed_text, getattr(self.whisper_engine, 'initial_prompt', None)):
                self.logger.info(f"Dropped an echo of the prompt, not speech: {transcribed_text!r}")
                print("   ✗ Heard only noise (Whisper repeated its prompt) - nothing typed")
                self._emit_pipeline('empty', mode=mode, info=info)
                return

            # Nobody pressed a key for this one: auto-dictation, or a
            # continuous-mode restart. It hears whatever is around.
            unprompted = auto_session or hands_free

            # A hotkey dictation is you: it teaches Koli how loud you are and
            # how you sound on this microphone.
            if not unprompted:
                self._learn_my_level(audio_data)
                self._learn_my_voiceprint(audio_data)

            # Only my voice: leave out speech much quieter than you (a TV across
            # the room, someone talking in your pauses).
            transcribed_text, info = self._keep_my_voice(transcribed_text, audio_data, info, unprompted)
            if not transcribed_text:
                self._emit_pipeline('empty', mode=mode, info=info)
                return

            # A recording nobody started on purpose: keep only what Whisper is
            # sure is speech. A hotkey recording is not second-guessed.
            if unprompted:
                forced = bool(getattr(self.whisper_engine, 'language', None))
                kept, why = confident_speech(transcribed_text, info, language_forced=forced)
                if why:
                    self.logger.info(f"{'Auto-dictation' if auto_session else 'Continuous dictation'}: {why}")
                if not kept:
                    print(f"   ✗ Not sure that was speech ({why}) - nothing typed")
                    self._emit_pipeline('empty', mode=mode, info=info)
                    return
                # Only my voice, step 2: it must sound like you on this microphone.
                if not self._sounds_like_me(audio_data):
                    print("   ✗ Doesn't sound like you on this microphone - nothing typed")
                    self._emit_pipeline('empty', mode=mode, info=info)
                    return
                if why:
                    print(f"   ✓ Kept what was clearly speech ({why})")
                transcribed_text = kept

            if command_mode:
                matched = self._handle_command_transcription(transcribed_text, use_auto_enter)
                self._emit_pipeline('command', mode=mode, raw=transcribed_text, matched=bool(matched))
                if self.level_overlay:
                    if matched:
                        self.level_overlay.flash_success()
                    else:
                        self.level_overlay.flash_failure()
                return

            if rephrase_mode and rephrase_selection:
                self._handle_rephrase(rephrase_selection, transcribed_text)
                self._emit_pipeline('rephrased', mode=mode, raw=transcribed_text)
                return

            # Transcribing took a moment: the session may have been cancelled since.
            if self._drop_stale_phrase(phrase, phrase_session, mode):
                return

            # Match the foreground app once, before post-processing, so a rule can
            # override formatting (e.g. code editors: verbatim, no auto-caps/periods)
            # in addition to the delivery behaviour handled further down.
            rule = self.app_rules.match_for_foreground()
            raw_postprocess_cfg = self.config_manager.get_postprocess_config()
            fmt_overrides = app_rules_formatting_overrides(rule, raw_postprocess_cfg)
            if fmt_overrides:
                self.logger.info(f"App rule {rule.get('match')} → formatting overrides {fmt_overrides}")
            postprocess_cfg = effective_postprocess_config(raw_postprocess_cfg, rule)
            trace = []
            heard_text = transcribed_text
            polish = self._ai_polish if self.config_manager.get_path('ai.polish.enabled') is True else None
            transcribed_text = postprocess(transcribed_text, postprocess_cfg, trace=trace, polish=polish)
            rule_id = _rule_label(rule)
            fg_now = foreground.get_foreground_app() or {}
            self._emit_pipeline('postprocessed', mode=mode, raw=heard_text, text=transcribed_text,
                                steps=trace, rule=rule_id, app=fg_now.get('exe', ''),
                                title=fg_now.get('title', ''))

            # Post-processing can legitimately empty the text — e.g. "scratch that"
            # erasing the whole utterance. Don't deliver an empty string (which
            # with auto-enter would just press Enter into the focused field).
            if not transcribed_text or not transcribed_text.strip():
                self.logger.info("Post-processing produced empty text; nothing to deliver")
                self._emit_pipeline('delivered', mode=mode, outcome='empty', raw=heard_text, text='')
                if self.level_overlay:
                    self.level_overlay.flash_failure("Nothing to type")
                return

            if rule and rule.get('suppress'):
                self._emit_pipeline('delivered', mode=mode, outcome='suppressed', raw=heard_text,
                                    text=transcribed_text, rule=rule_id, app=fg_now.get('exe', ''))
                self.logger.info(f"Delivery suppressed by app rule: {rule.get('match')}")
                # Clipboard-free setups get the recovery window instead of a
                # silent copy, so the transcript is still retrievable without
                # touching the clipboard behind the user's back (issue #12).
                may_copy = self.clipboard_manager.silent_copy_allowed
                if may_copy:
                    self.clipboard_manager.copy_text(transcribed_text)
                else:
                    self.fallback_window.show(
                        transcribed_text,
                        reason="Delivery is suppressed for this app, and clipboard "
                               "copying is off — your dictation is safe here.",
                        allow_clipboard=False,
                    )
                self.last_transcription = transcribed_text
                self.recent_transcriptions.appendleft(transcribed_text)
                self.system_tray.refresh_menu()
                self.system_tray.notify(
                    "Delivery suppressed for this app — text on clipboard." if may_copy
                    else "Delivery suppressed for this app — see the popup.")
                if self.level_overlay:
                    self.level_overlay.flash_success()
                return

            if not self._foreground_is_textable():
                self._emit_pipeline('delivered', mode=mode, outcome='fallback', raw=heard_text,
                                    text=transcribed_text, app=fg_now.get('exe', ''))
                self.logger.info("No textable foreground window; opening fallback window")
                may_copy = self.clipboard_manager.silent_copy_allowed
                if may_copy:
                    self.clipboard_manager.copy_text(transcribed_text)
                self.fallback_window.show(
                    transcribed_text,
                    reason=("No text field was focused — your dictation is safe here. "
                            "Already on your clipboard.") if may_copy else
                           ("No text field was focused — your dictation is safe here. "
                            "Clipboard copying is off; use Copy if you want it."),
                    allow_clipboard=may_copy,
                )
                self.last_transcription = transcribed_text
                self.recent_transcriptions.appendleft(transcribed_text)
                self.system_tray.refresh_menu()
                self.system_tray.notify("Captured your dictation — see the popup.")
                if self.level_overlay:
                    self.level_overlay.flash_success()
                return

            effective_auto_enter, send_may_wait, send_wait = self._auto_send_plan(
                use_auto_enter, rule, auto_dictation=auto_session)
            effective_auto_paste = None
            if rule:
                if 'auto_paste' in rule:
                    effective_auto_paste = bool(rule['auto_paste'])

            previous_auto_paste = None
            if effective_auto_paste is not None:
                previous_auto_paste = self.clipboard_manager.auto_paste
                self.clipboard_manager.update_auto_paste(effective_auto_paste)
                # An app rule silently turning paste off reads as "the app is
                # broken" — it was reported as exactly that (issue #11). Say it
                # once per app per session: often enough to explain, rare enough
                # not to nag.
                if previous_auto_paste and not effective_auto_paste:
                    self._announce_copy_only(rule)

            try:
                # A phrase gets a trailing space so the next one does not glue to it.
                success = self.clipboard_manager.deliver_transcription(
                    transcribed_text + ' ' if phrase else transcribed_text, effective_auto_enter,
                    allow_delay=send_may_wait, send_delay=send_wait,
                )
            finally:
                if previous_auto_paste is not None:
                    self.clipboard_manager.update_auto_paste(previous_auto_paste)

            delivered_paste = self.clipboard_manager.auto_paste
            if success and auto_session and delivered_paste:
                self._auto_pasted = True   # the session's end may send it
                self._auto_pasted_words = getattr(self, '_auto_pasted_words', 0) + count_words(transcribed_text)
            self._emit_pipeline(
                'delivered', mode=mode,
                outcome=('pasted' if delivered_paste else 'copied') if success else 'failed',
                raw=heard_text, text=transcribed_text, rule=rule_id, app=fg_now.get('exe', ''),
                method=self.clipboard_manager.delivery_method,
                # A send still counting down hasn't pressed Enter yet; the
                # bubble follows it through the 'auto_send' events.
                auto_enter=bool(effective_auto_enter and success and not self.clipboard_manager.send_is_pending()),
                send_pending=bool(effective_auto_enter and success and self.clipboard_manager.send_is_pending()),
                duration=round(duration, 2))
            self.logger.info(f"Delivered{' phrase' if phrase else ''}: {'pasted' if delivered_paste else 'copied'}" if success else "Delivery failed")
            if success:
                self.last_transcription = transcribed_text
                self.recent_transcriptions.appendleft(transcribed_text)
                self.system_tray.refresh_menu()
                if not phrase:  # phrases are pasted silently; the bar shows the text landing
                    self.audio_feedback.play_transcription_complete_sound()
                    if self.level_overlay:
                        self.level_overlay.flash_success()
                fg = foreground.get_foreground_app() or {}
                record_transcription(
                    char_count=len(transcribed_text),
                    duration_seconds=duration,
                    app=fg.get('exe', ''),
                )
                record_transcript(transcribed_text, window=fg, duration_s=duration,
                                  language=(info or {}).get('language') or '')
                audit_enabled = (self.config_manager.config.get('audit') or {}).get('enabled', False)
                audit_record('delivered', transcribed_text, fg.get('exe', ''), audit_enabled)
                self._maybe_restart_continuous()
            elif self.level_overlay:
                self.level_overlay.flash_failure()
            
        except Exception as e:
            self.logger.error(f"Error in processing workflow: {e}")
            print(f"❌ Error processing recording: {e}")
            self._emit_pipeline('error', mode=mode, error=str(e))
            if self.level_overlay:
                self.level_overlay.flash_failure()

        finally:
            # Safety net: if a streaming-delivery worker is still around (e.g. the
            # audio came back None and we returned before _finalize_streaming_delivery,
            # or an exception fired), stop+join it here so no path can leak the
            # worker thread or leave _streaming_delivery_active stuck True.
            if self.streaming_delivery is not None:
                try:
                    self.streaming_delivery.stop()
                except Exception:
                    pass
                self.streaming_delivery = None
            self._streaming_delivery_active = False

            with self._state_lock:
                self.is_processing = False
                if phrase:
                    return  # still recording: no idle hand-off, no engine swaps yet
                pending_model = self._pending_model_change
                pending_device = self._pending_device_change

            if pending_device:
                device_id, device_name = pending_device
                self.logger.info(f"Executing pending device change to: {device_name}")
                self._execute_audio_device_change(device_id, device_name)
                self._pending_device_change = None

            if pending_model:
                self.logger.info(f"Executing pending model change to: {pending_model}")
                print(f"🔄 Processing complete, now switching to [{pending_model}] model...")
                self._execute_model_change(pending_model)
                self._pending_model_change = None

            if not (pending_device or pending_model):
                self._update_ui_state("idle")
            self._run_idle_actions()
            self.logger.info("Ready for the next dictation")

    # `only` limits which commands may run (wake-word follow-ups: media only);
    # `announce_miss=False` keeps a miss out of the console (lyrics, talk);
    # `confirm_runs`: anyone could have said it, so run steps ask first.
    def _handle_command_transcription(self, text: str, use_auto_enter: bool = False, only=None,
                                      announce_miss: bool = True, confirm_runs: bool = False) -> bool:
        log_config = self.config_manager.get_logging_config()
        if log_config.get('log_transcriptions', False) and announce_miss:
            self.logger.info(f"Command mode transcription: '{text}'")
        else:
            self.logger.info("Command mode transcription received")

        if not self.voice_command_manager.enabled:
            self.logger.warning("Voice commands disabled")
            return False

        matched = self.voice_command_manager.match_command(text, only=only)
        if matched:
            if confirm_runs:
                self.voice_command_manager.execute_command(matched, use_auto_enter, confirm_runs=True)
            else:
                self.voice_command_manager.execute_command(matched, use_auto_enter)
            return True
        if announce_miss:
            print(f"   ✗ No matching command for: \"{text[:60]}\"")
        return False

    def get_application_state(self) -> dict:
        status = {
            "recording": self.audio_recorder.get_recording_status(),
            "processing": self.is_processing,
            "model_loading": self.is_model_loading,
        }
        
        return status
    
    def manual_transcribe_test(self, duration_seconds: int = 5):
        try:
            print(f"🎤 Recording for {duration_seconds} seconds...")
            print("Speak now!")
            
            self.audio_recorder.start_recording()
            
            time.sleep(duration_seconds)
            
            audio_data = self.audio_recorder.stop_recording()
            self._transcription_pipeline(audio_data)
            
        except Exception as e:
            self.logger.error(f"Manual test failed: {e}")
            print(f"❌ Test failed: {e}")

    # Single place that fans a state change out to every status surface, so
    # the tray icon and the terminal tab title can never drift apart. Both are
    # OptionalComponent-wrapped: a disabled tray or a non-TTY terminal is a
    # silent no-op rather than a branch at each call site.
    def _update_ui_state(self, state: str):
        if state != 'recording':
            self.media_control.on_recording_end()
        self.system_tray.update_state(state)
        self.terminal_title.update_state(state)
        self._publish_state(state)

    # The UI's view of the state: the pipeline state plus which kind of
    # recording it is and whether hotkeys are paused.
    def _publish_state(self, state: str):
        mode = 'command' if self._command_mode else ('rephrase' if self._rephrase_mode else 'dictate')
        if state == 'recording' and self._auto_started:
            mode = 'auto'  # the floating bar shows "Listening" instead of the hotkey hints
        payload = {'state': state, 'mode': mode, 'paused': bool(self.is_paused),
                   'model_loading': bool(self.is_model_loading)}
        if state != 'recording':
            payload['hands_free'] = False  # a lock only lasts one recording
        publish('state', payload)

    # Recording continues without holding the key (double-tap lock).
    def set_hands_free(self, on: bool):
        publish('state', {'hands_free': bool(on)})

    # One stage of a dictation, for the Live trace and the floating bar. Text is
    # only ever kept in memory for the UI; the log still follows log_transcriptions.
    # The meeting hotkey and the tray: start a meeting or end the running one.
    def toggle_meeting(self) -> bool:
        if self.meetings is None:
            self.system_tray.notify("Meetings need the app window (not --legacy-ui)")
            return False
        self.meetings.toggle()
        return True

    # The mute hotkey: your microphone muted (or unmuted) for every app, and
    # left out of the meeting while muted.
    def toggle_meeting_mute(self) -> bool:
        if self.meetings is None:
            self.system_tray.notify("Meetings need the app window (not --legacy-ui)")
            return False
        muted = self.meetings.toggle_mic_mute()
        if muted is None:
            self.system_tray.notify("Couldn't mute the meeting microphone")
            return False
        self.system_tray.notify("Microphone muted for every app" if muted else "Microphone on")
        return True

    def _emit_pipeline(self, stage: str, **data):
        data['stage'] = stage
        data['t'] = time.time()
        publish('pipeline', data)

    # ── Hot-swapping components (instant-apply settings) ──────────────────────

    def is_idle(self) -> bool:
        return self.get_current_state() == 'idle'

    # Run `fn` now if nothing is recording or transcribing, otherwise as soon as
    # the current dictation finishes. Used by background settings so a model or
    # microphone swap never interrupts a dictation in progress.
    def run_when_idle(self, fn):
        if self.is_idle():
            fn()
            return True
        with self._state_lock:
            self._idle_actions.append(fn)
        return False

    def _run_idle_actions(self):
        with self._state_lock:
            actions, self._idle_actions = self._idle_actions, []
        for fn in actions:
            try:
                fn()
            except Exception as e:
                self.logger.error(f"Deferred action failed: {e}")

    # Replace the Whisper engine with one already built and warmed up off-thread.
    def replace_whisper_engine(self, new_engine):
        with self._state_lock:
            old = self.whisper_engine
            self.whisper_engine = new_engine
        self.logger.info(f"Whisper engine swapped: {getattr(old, 'model_key', '?')} -> "
                         f"{getattr(new_engine, 'model_key', '?')} on {getattr(new_engine, 'device', '?')}")
        # Free the old engine's graphics memory now, not whenever Python gets
        # to it: otherwise a few switches leave no room for a second model.
        if old is not None and old is not new_engine and hasattr(old, 'unload'):
            try:
                old.unload()
            except Exception as e:
                self.logger.warning(f"Could not unload the old engine: {e}")

    # Build a new recorder with the same callbacks and any changed parameters,
    # swap it in, then stop the old one's capture thread.
    def rebuild_audio_recorder(self, **overrides):
        old = self.audio_recorder
        audio_cfg = self.config_manager.config.get('audio') or {}
        params = dict(
            on_vad_event=self.handle_vad_event,
            channels=old.channels,
            dtype=old.dtype,
            max_duration=old.max_duration,
            on_max_duration_reached=old.on_max_duration_reached,
            vad_manager=old.vad_manager,
            streaming_manager=old.streaming_manager,
            on_streaming_result=old.on_streaming_result,
            device=old.device,
            noise_suppression_config=audio_cfg.get('noise_suppression') or {},
            whisper_mode_config=audio_cfg.get('whisper_mode') or {},
        )
        params.update(overrides)
        new_recorder = AudioRecorder(**params)
        # A meeting's microphone tap must follow the microphone.
        new_recorder._taps = list(getattr(old, '_taps', []))
        with self._state_lock:
            self.audio_recorder = new_recorder
        try:
            old.shutdown()
        except Exception as e:
            self.logger.debug(f"Old recorder shutdown failed: {e}")
        return new_recorder

    
    def shutdown(self):        
        self.terminal_title.stop()
        print("Koli is shutting down... goodbye!")

        if self.audio_recorder.get_recording_status():
            self.audio_recorder.stop_recording()
        self.audio_recorder.shutdown()

        if self.level_overlay:
            self.level_overlay.shutdown()

        self.system_tray.stop()
    
    def set_model_loading(self, loading: bool):
        with self._state_lock:
            old_state = self.is_model_loading
            self.is_model_loading = loading
            
            if old_state != loading:
                if loading:
                    self._update_ui_state("processing")
                else:
                    self._update_ui_state("idle")
    
    def is_transcription_recording(self) -> bool:
        return self.audio_recorder.get_recording_status() and not self._command_mode

    def can_start_recording(self) -> bool:
        with self._state_lock:
            return not (self.is_processing or self.is_model_loading or self.audio_recorder.get_recording_status())
    
    def get_current_state(self) -> str:
        with self._state_lock:
            if self.is_model_loading:
                return "model_loading"
            elif self.is_processing:
                return "processing"
            elif self.audio_recorder.get_recording_status():
                return "recording"
            else:
                return "idle"
    
    def request_model_change(self, new_model_key: str) -> bool:
        current_state = self.get_current_state()
        
        if new_model_key == self.whisper_engine.model_key:
            return True
        
        if current_state == "model_loading":
            print("⏳ Model already loading, please wait...")
            return False
        
        if current_state == "recording":
            print(f"🎤 Cancelling recording to switch to [{new_model_key}] model...")
            self.cancel_active_recording()
            self._execute_model_change(new_model_key)
            return True
        
        if current_state == "processing":
            print(f"⏳ Queueing model change to [{new_model_key}] until transcription completes...")
            self._pending_model_change = new_model_key
            return True
        
        if current_state == "idle":
            self._execute_model_change(new_model_key)
            return True
        
        self.logger.warning(f"Unexpected state for model change: {current_state}")
        return False
    
    def update_transcription_mode(self, value):
        self.config_manager.update_user_setting('clipboard', 'auto_paste', value)
        self.clipboard_manager.update_auto_paste(value)

    def _execute_model_change(self, new_model_key: str):
        def progress_callback(message: str):
            if "ready" in message.lower() or "already loaded" in message.lower():
                print(f"✅ Successfully switched to [{new_model_key}] model")
                self.set_model_loading(False)
            elif "failed" in message.lower():
                print(f"❌ Failed to change model: {message}")
                self.set_model_loading(False)
            else:
                print(f"🔄 {message}")
                self.set_model_loading(True)
        
        try:
            self.set_model_loading(True)
            print(f"🔄 Switching to [{new_model_key}] model...")
            
            self.whisper_engine.change_model(new_model_key, progress_callback)
            
        except Exception as e:
            self.logger.error(f"Failed to initiate model change: {e}")
            print(f"❌ Failed to change model: {e}")
            self.set_model_loading(False)

    # The last post-processing step when "Polish every dictation" is on. An
    # empty answer (model down, too slow) keeps the dictation as it was.
    def _ai_polish(self, text: str) -> str:
        from . import llm
        from .llm import prompts
        result = llm.complete('polish', prompts.polish(self.config_manager.get_path('ai.polish.prompt'), text))
        if result.error:
            self.logger.warning(f"AI polish skipped: {result.error}")
        return result.text

    def _handle_rephrase(self, selection: str, instruction: str):
        import time
        import pyperclip
        from . import llm
        from .llm import prompts
        from .platform import keyboard as kb

        print(f"\n   🤖 Rephrasing ({len(selection)} chars) with instruction: '{instruction[:60]}'")

        result = llm.complete('rewrite', prompts.rewrite(instruction, selection))
        polished = result.text
        if not polished:
            print(f"   ✗ Rephrase failed: {result.error}")
            self.system_tray.notify(f"Rephrase failed: {result.error}")
            if self.level_overlay:
                self.level_overlay.flash_failure()
            return

        try:
            pyperclip.copy(polished)
            time.sleep(0.05)
            kb.send_hotkey('ctrl', 'v')
            time.sleep(0.2)
        finally:
            try:
                if self._rephrase_original_clipboard:
                    pyperclip.copy(self._rephrase_original_clipboard)
            except Exception:
                pass

        self.last_transcription = polished
        self.recent_transcriptions.appendleft(polished)
        self.system_tray.refresh_menu()
        self.system_tray.notify("Rephrased and pasted")
        if self.level_overlay:
            self.level_overlay.flash_success()
        self.logger.info(f"Rephrase complete: {len(selection)} -> {len(polished)} chars")
        print("   ✓ Rephrased and pasted")

    NON_TEXT_EXES = {'progman.exe', 'workerw.exe', 'dwm.exe', 'searchhost.exe',
                     'shellexperiencehost.exe', 'startmenuexperiencehost.exe',
                     'lockapp.exe', 'sihost.exe'}

    def _foreground_is_textable(self) -> bool:
        try:
            info = foreground.get_foreground_app() or {}
        except Exception:
            return True
        exe = (info.get('exe') or '').lower()
        if not exe:
            return False
        return exe not in self.NON_TEXT_EXES


    def set_overlay_position(self, name: str) -> bool:
        if not self.level_overlay:
            return False
        self.level_overlay.set_position(name)
        self.config_manager.update_user_setting('overlay', 'position', name)
        self.logger.info(f"Overlay position set to {name}")
        return True

    def get_overlay_position(self) -> str:
        return self.config_manager.config.get('overlay', {}).get('position', 'bottom-center')

    def get_current_language(self) -> str:
        return self.config_manager.config.get('whisper', {}).get('language', 'auto')

    def set_language(self, code: str) -> bool:
        current = self.get_current_language()
        if current == code:
            return False
        self.config_manager.update_user_setting('whisper', 'language', code)
        try:
            self.whisper_engine.language = None if code == 'auto' else code
        except Exception:
            pass
        self.logger.info(f"Language switched: {current} -> {code}")
        print(f"   ✓ Language set to: {code}")
        self.system_tray.notify(f"Language: {code}")
        self.system_tray.refresh_menu()
        return True

    def apply_transform(self, name: str) -> bool:
        success = self.transforms_manager.apply(name)
        if self.level_overlay:
            if success:
                self.level_overlay.flash_success()
            else:
                self.level_overlay.flash_failure()
        return success

    def list_transforms(self) -> list:
        return self.transforms_manager.list_transforms()

    def reload_transforms(self) -> bool:
        old = [(t.get('name'), t.get('hotkey')) for t in self.transforms_manager.list_transforms()]
        self.transforms_manager.reload_if_changed()
        new = [(t.get('name'), t.get('hotkey')) for t in self.transforms_manager.list_transforms()]
        changed = old != new
        if changed:
            self.system_tray.notify(f"Transforms reloaded ({len(new)} active)")
            self.system_tray.refresh_menu()
            if self._hotkey_listener_ref:
                self._hotkey_listener_ref.refresh_transforms()
        return changed

    def set_hotkey_listener(self, listener):
        self._hotkey_listener_ref = listener

    def list_profiles(self) -> list:
        return self.profile_manager.list_profiles()

    def get_active_profile(self) -> Optional[str]:
        return self.profile_manager.get_active()

    def activate_profile(self, name: str) -> bool:
        old_whisper = dict(self.config_manager.get_whisper_config())

        if not self.profile_manager.apply(name):
            return False

        new_whisper = self.config_manager.get_whisper_config()

        if new_whisper.get('language') != old_whisper.get('language'):
            lang = new_whisper.get('language', 'auto')
            try:
                self.whisper_engine.language = None if lang == 'auto' else lang
            except Exception:
                pass

        if new_whisper.get('task') != old_whisper.get('task'):
            task = new_whisper.get('task') or 'transcribe'
            try:
                self.whisper_engine.task = task if task in ('transcribe', 'translate') else 'transcribe'
            except Exception:
                pass

        new_prompt = new_whisper.get('initial_prompt') or ''
        if new_prompt != (old_whisper.get('initial_prompt') or ''):
            try:
                self.whisper_engine.initial_prompt = new_prompt or None
            except Exception:
                pass

        if new_whisper.get('model') and new_whisper.get('model') != old_whisper.get('model'):
            try:
                self._execute_model_change(new_whisper['model'])
            except Exception as e:
                self.logger.error(f"Profile model change failed: {e}")

        # A profile may switch auto-dictation on or off; the tray path bypasses
        # the settings appliers, so apply it here.
        if self._auto_dictation is not None:
            try:
                self._auto_dictation.apply_settings()
            except Exception as e:
                self.logger.warning(f"Auto-dictation not re-applied after profile: {e}")

        self.logger.info(f"Activated profile: {name}")
        print(f"   ✓ Profile activated: {name}")
        self.system_tray.notify(f"Profile: {name}")
        self.system_tray.refresh_menu()
        return True

    def get_recent_transcriptions(self) -> list:
        return list(self.recent_transcriptions)

    # Types the last dictation again at the cursor (paste-last hotkey), e.g.
    # after it went to the wrong window. Never auto-sends, and the foreground
    # app's rule still applies: nothing goes into a suppressed app (password
    # managers), and copy-only apps (terminals, where a pasted newline runs a
    # command) get the clipboard instead.
    def paste_last_transcription(self) -> bool:
        text = self.last_transcription
        if not text:
            self.system_tray.notify("Nothing to paste yet — dictate something first.")
            return False
        if self.get_current_state() != "idle":
            self.logger.info("Paste-last ignored: busy recording or transcribing")
            return False
        self._wait_for_modifiers_released()
        rule = None
        try:
            rule = self.app_rules.match_for_foreground()
        except Exception as e:
            self.logger.debug(f"Paste-last rule lookup failed: {e}")
        try:
            if rule and rule.get('suppress'):
                self.logger.info(f"Paste-last suppressed by app rule: {rule.get('match')}")
                self.system_tray.notify("Not pasted: this app is excluded in app_rules.yaml.")
                return False
            if rule and rule.get('auto_paste') is False:
                self.clipboard_manager.copy_text(text)
                self.system_tray.notify("Last dictation copied — this app is copy-only.")
                return True
            self.clipboard_manager.deliver_transcription(text, use_auto_enter=False)
        except Exception as e:
            self.logger.error(f"Paste-last failed: {e}")
            self.system_tray.notify("Couldn't paste the last dictation.")
            return False
        return True

    # A hotkey's release event can arrive while its other modifiers are still
    # down, and those would merge into the paste. Wait briefly for the user to
    # let go; after the timeout, deliver anyway rather than drop the request.
    def _wait_for_modifiers_released(self, timeout: float = 1.5):
        import time
        try:
            from .platform import keyboard as kb
            modifiers_held = kb.modifiers_held
        except (ImportError, AttributeError):
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if not modifiers_held():
                    return
            except Exception:
                return
            time.sleep(0.02)

    def recopy_recent_transcription(self, index: int):
        if 0 <= index < len(self.recent_transcriptions):
            text = self.recent_transcriptions[index]
            self.clipboard_manager.copy_text(text)
            print(f"   ✓ Re-copied: {text[:60]}{'...' if len(text) > 60 else ''}")

    def get_available_audio_devices(self, host_filter: Optional[str] = None):
        host_name = host_filter if host_filter is not None else self._current_audio_host
        return AudioRecorder.get_available_audio_devices(host_name)

    def get_current_audio_device_id(self):
        return self.audio_recorder.get_device_id()

    def get_available_audio_hosts(self):
        try:
            hostapis = sd.query_hostapis()
            devices = sd.query_devices()
        except Exception as e:
            self.logger.error(f"Failed to query audio hosts: {e}")
            return []

        hosts_with_input = {}
        for index, host in enumerate(hostapis):
            hosts_with_input[index] = {
                'name': host['name'],
                'index': index,
                'has_input': False
            }

        for device in devices:
            if device.get('max_input_channels', 0) > 0:
                host_index = device['hostapi']
                if host_index in hosts_with_input:
                    hosts_with_input[host_index]['has_input'] = True

        return [
            {'name': host['name'], 'index': host['index']}
            for host in hosts_with_input.values()
            if host['has_input']
        ]

    def get_current_audio_host(self) -> Optional[str]:
        return self._current_audio_host

    def set_audio_host(self, host_name: str) -> bool:
        if not host_name:
            return False

        available_hosts = self.get_available_audio_hosts()
        normalized_lookup = {host['name'].lower(): host for host in available_hosts}
        host_entry = normalized_lookup.get(host_name.lower())

        if not host_entry:
            self.logger.warning(f"Requested audio host '{host_name}' is not available")
            return False

        canonical_name = host_entry['name']
        if canonical_name == self._current_audio_host:
            return True

        self._current_audio_host = canonical_name
        self.config_manager.update_audio_host(canonical_name)
        self.logger.info(f"Audio host changed to {canonical_name}")

        self._ensure_audio_device_for_host(canonical_name)
        self.system_tray.refresh_menu()
        return True

    def request_audio_device_change(self, device_id: int, device_name: str):
        current_state = self.get_current_state()

        if device_id == self.audio_recorder.device:
            return True

        if current_state == "recording":
            print("🎤 Cancelling recording to switch audio device...")
            self.cancel_active_recording()
            self._execute_audio_device_change(device_id, device_name)
            return True

        if current_state == "processing":
            print("⏳ Queueing audio device change until transcription completes...")
            self._pending_device_change = (device_id, device_name)
            return True

        if current_state == "idle":
            self._execute_audio_device_change(device_id, device_name)
            return True

        self.logger.warning(f"Unexpected state for device change: {current_state}")
        return False

    def _execute_audio_device_change(self, device_id: int, device_name: str):
        try:
            print(f"🎤 Switching to: {device_name}")

            channels = self.audio_recorder.channels
            dtype = self.audio_recorder.dtype
            max_duration = self.audio_recorder.max_duration
            on_max_duration = self.audio_recorder.on_max_duration_reached
            vad_manager = self.audio_recorder.vad_manager
            streaming_manager = self.audio_recorder.streaming_manager
            on_streaming_result = self.audio_recorder.on_streaming_result

            audio_cfg = self.config_manager.config.get('audio') or {}
            noise_cfg = audio_cfg.get('noise_suppression') or {}
            new_recorder = AudioRecorder(
                on_vad_event=self.handle_vad_event,
                channels=channels,
                dtype=dtype,
                max_duration=max_duration,
                on_max_duration_reached=on_max_duration,
                vad_manager=vad_manager,
                streaming_manager=streaming_manager,
                on_streaming_result=on_streaming_result,
                device=device_id if device_id != -1 else None,
                noise_suppression_config=noise_cfg,
                whisper_mode_config=audio_cfg.get('whisper_mode') or {},
            )

            old_recorder = self.audio_recorder
            self.audio_recorder = new_recorder
            # The old recorder's capture thread kept its stream open forever;
            # stop it now that the new one is live.
            try:
                old_recorder.shutdown()
            except Exception:
                pass

            print(f"✅ Successfully switched audio device to: {device_name}")

        except Exception as e:
            self.logger.error(f"Failed to change audio device: {e}")
            print(f"❌ Failed to switch audio device: {e}")

    def _initialize_audio_host(self):
        try:
            configured_host = self.config_manager.get_setting('audio', 'host')
        except KeyError:
            configured_host = None

        available_hosts = self.get_available_audio_hosts()
        resolved_host = self._resolve_audio_host(configured_host, available_hosts)

        self._current_audio_host = resolved_host

        if resolved_host != configured_host:
            self.config_manager.update_audio_host(resolved_host)

    def _resolve_audio_host(self, configured_host: Optional[str], available_hosts):
        if not available_hosts:
            return None

        if configured_host:
            match = self._match_host(configured_host, available_hosts)
            if match:
                return match

        preferred_host = self._preferred_platform_host()
        if preferred_host:
            preferred_match = self._match_host(preferred_host, available_hosts)
            if preferred_match:
                return preferred_match

        return available_hosts[0]['name']

    def _match_host(self, requested: str, available_hosts) -> Optional[str]:
        requested_lower = requested.lower().strip()
        for host in available_hosts:
            if host['name'].lower() == requested_lower:
                return host['name']
        for host in available_hosts:
            if requested_lower in host['name'].lower():
                return host['name']
        return None

    def _preferred_platform_host(self) -> Optional[str]:
        system_name = platform.system().lower()
        if system_name == 'windows':
            return 'WASAPI'
        return None

    def _ensure_audio_device_for_host(self, host_name: Optional[str]):
        if not host_name or not self.audio_recorder:
            return

        try:
            current_device_id = self.audio_recorder.get_device_id()
        except Exception as e:
            self.logger.error(f"Unable to read current audio device: {e}")
            return

        if self._device_matches_host(current_device_id, host_name):
            return

        fallback_device_id = self._get_default_device_for_host(host_name)
        if fallback_device_id is None:
            self.logger.warning(f"No input devices available for host {host_name}")
            return

        device_name = self._get_device_name(fallback_device_id)
        success = self.request_audio_device_change(fallback_device_id, device_name)

        if not success:
            self.logger.warning(f"Failed to switch to fallback device {fallback_device_id} for host {host_name}")

    def _device_matches_host(self, device_id: int, host_name: str) -> bool:
        try:
            device_info = sd.query_devices(device_id)
            host_info = sd.query_hostapis(device_info['hostapi'])
            return host_info['name'].lower() == host_name.lower()
        except Exception:
            return False

    def _get_default_device_for_host(self, host_name: str) -> Optional[int]:
        try:
            target_index = None
            target_host = None
            hostapis = sd.query_hostapis()
            for idx, host in enumerate(hostapis):
                if host['name'].lower() == host_name.lower():
                    target_index = idx
                    target_host = host
                    break
            else:
                return None

            default_input = target_host.get('default_input_device', -1)
            if default_input is not None and default_input >= 0:
                device_info = sd.query_devices(default_input)
                if device_info.get('max_input_channels', 0) > 0:
                    return default_input

            all_devices = sd.query_devices()
            for idx, device in enumerate(all_devices):
                if device['hostapi'] == target_index and device.get('max_input_channels', 0) > 0:
                    return idx
        except Exception as e:
            self.logger.error(f"Failed to determine default device for host {host_name}: {e}")

        return None

    def _get_device_name(self, device_id: int) -> str:
        try:
            device_info = sd.query_devices(device_id)
            return device_info.get('name', f"Device {device_id}")
        except Exception:
            return f"Device {device_id}"
