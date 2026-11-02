# audio_recorder.py
# Continuous-stream audio capture with a pre-roll ring buffer. Key design choices:
#
#   • A single sd.InputStream runs continuously from app startup. We do NOT
#     open/close streams per recording — that was the source of "first word
#     clipped" bugs and added 100-200ms of warmup latency on every press.
#
#   • The buffer is a deque trimmed to ~500ms when idle. When the user presses
#     record, the buffer already contains the recent past, so anything they
#     started saying before the OS finished routing the hotkey is captured.
#
#   • WASAPI on Windows runs at native rate (typically 48 kHz); we resample to
#     Whisper's 16 kHz only at stop time. This avoids resampling per chunk during
#     a recording, which would burn CPU.
#
#   • The microphone is remembered by name (device numbers move when devices
#     come and go). A stream that fails or goes quiet is reopened, never given
#     up on: on your microphone when it is there, else on the default input,
#     and back on yours once it is plugged in again. A recording in progress
#     keeps what it had. Silent-mic (peak amplitude near zero) is reported.

import collections
import logging
import threading
import time
from typing import Optional, Callable

import numpy as np
import sounddevice as sd
import soxr

from .platform import IS_WINDOWS, app, session as desktop_session


def app_is_windows() -> bool:
    return IS_WINDOWS
from .voice_activity_detection import VadEvent, VAD_CHUNK_SIZE


# A problem you must see (bubble and app). The UI is optional: the recorder
# also runs headless (--selftest, tests).
def _report_problem(message: str) -> None:
    try:
        from .ui.events import report_problem
        report_problem(message)
    except Exception:
        pass


# ── Microphones by name ──────────────────────────────────────────────────────

# Whether two device names are the same microphone. The MME driver cuts
# names to 31 characters, so a long name also matches its cut-off start.
def same_mic(a: str, b: str) -> bool:
    a, b = (a or '').strip().lower(), (b or '').strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 25 and long_.startswith(short)


# The input device number for a microphone name: on `host` (the chosen audio
# driver; Windows lists each microphone once per driver) when it is there,
# else on any driver. None when it isn't plugged in. Automatic (no host) is
# WASAPI on Windows, as everywhere else in Koli; MME comes first in the list.
AUTOMATIC_HOST_WINDOWS = 'Windows WASAPI'


def find_input_device(name: str, host: Optional[str] = None) -> Optional[int]:
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
    except Exception:
        return None
    if not host and app_is_windows():
        host = AUTOMATIC_HOST_WINDOWS
    matches = [i for i, d in enumerate(devices)
               if d.get('max_input_channels', 0) > 0 and same_mic(d.get('name'), name)]
    if host:
        for i in matches:
            if hostapis[devices[i]['hostapi']]['name'].lower() == host.lower():
                return i
    return matches[0] if matches else None


# A microphone setting as a device number for sounddevice: None for the
# default input ("default", "" or None), a number as it is, a name looked up.
# A name that isn't plugged in raises LookupError.
def resolve_input(setting, host: Optional[str] = None) -> Optional[int]:
    if setting in (None, '', 'default'):
        return None
    if isinstance(setting, int):
        return setting
    text = str(setting).strip()
    if text.isdigit():
        return int(text)
    found = find_input_device(text, host)
    if found is None:
        raise LookupError(f"{text} isn't plugged in")
    return found


# Has PortAudio reread its device list, which it reads once at start (a
# microphone plugged in later is invisible until then). Only safe with no
# stream of ours open; a meeting's own microphone stream, if one is open,
# fails and falls back to the dictation microphone (meeting/capture.py).
def _refresh_device_list():
    try:
        sd._terminate()
        sd._initialize()
    except Exception as e:
        logging.getLogger(__name__).warning(f"Could not reread the audio devices: {e}")


class AudioRecorder:
    WHISPER_SAMPLE_RATE = 16000
    # No audio for this long means the stream is gone (an unplugged device on
    # WASAPI often just stops calling back).
    STALL_SECONDS = 2.0
    # The longest wait between two attempts to reopen a failed stream.
    RETRY_MAX_WAIT_S = 5.0
    # How often Windows' live list is checked for your microphone coming back.
    MIC_BACK_CHECK_S = 3.0

    # The chosen audio driver, where a microphone name is looked up first
    # (set per recorder; None = any driver).
    preferred_host = None

    # Whether the device list may be reread on a reopen; the state manager
    # says no while a meeting holds its own streams.
    @staticmethod
    def may_refresh_devices() -> bool:
        return True
    THREAD_JOIN_TIMEOUT = 2.0
    LOOP_SLEEP_MS = 100
    STREAM_DTYPE = np.float32
    PREROLL_SECONDS = 0.5
    TRAILING_SILENCE_TRIM_SECONDS = 0.4
    SILENCE_RMS_THRESHOLD = 0.005
    LONG_PAUSE_SECONDS = 2.0
    LONG_PAUSE_REPLACEMENT_SECONDS = 0.4

    def __init__(self,
                 on_vad_event: Callable[[VadEvent], None],
                 channels: int = 1,
                 dtype: str = "float32",
                 max_duration: int = 30,
                 on_max_duration_reached: callable = None,
                 vad_manager=None,
                 streaming_manager=None,
                 on_streaming_result: Callable[[str, bool], None] = None,
                 device=None,
                 noise_suppression_config: Optional[dict] = None,
                 whisper_mode_config: Optional[dict] = None,
                 host: Optional[str] = None):

        self.sample_rate = self.WHISPER_SAMPLE_RATE
        # The chosen audio driver (Settings › Audio driver): where a microphone
        # name is looked up first.
        self.preferred_host = host
        self.channels = channels
        self.dtype = dtype
        self.max_duration = max_duration
        self.on_max_duration_reached = on_max_duration_reached
        self.is_recording = False
        self.recording_start_time = None
        self.level_preview = False
        self.logger = logging.getLogger(__name__)
        self._set_noise_suppression(noise_suppression_config or {})
        self._whisper_mode_gain = self._parse_whisper_mode(whisper_mode_config)
        # (audio array, raw array) for the last few handed out; see raw_for.
        self._recent_clips = collections.deque(maxlen=4)

        self.vad_manager = vad_manager
        self.on_vad_event = on_vad_event
        self.continuous_vad = self._setup_continuous_vad_monitoring()

        self.streaming_manager = streaming_manager
        self.on_streaming_result = on_streaming_result

        self.resolve_device(device)
        self._test_audio_source()

        self.continuous_streaming = self._setup_continuous_streaming()
        self.stream_partials = False

        self._configure_for_device_rate()
        self._buffer = collections.deque()
        # Guards the ring buffer. The PortAudio callback appends/trims on its own
        # thread while stop/cancel/max-duration snapshot+clear from other threads;
        # list(deque) raises RuntimeError if the deque mutates mid-iteration, so
        # every buffer access is serialized through this lock. Held only for fast
        # ops (append, snapshot, clear), so it won't stall the audio callback.
        self._buffer_lock = threading.Lock()

        self._capture_running = False
        self._capture_thread = None
        self._stream_error = None
        self._last_audio_at = time.monotonic()
        self._current_level = 0.0
        # Meeting mode listens to the microphone through taps: fn(mono 16 kHz
        # float32, wall clock), called on the audio thread for every chunk.
        self._taps = []
        self._start_capture()

    def _setup_continuous_vad_monitoring(self):
        if self.vad_manager.is_available():
            return self.vad_manager.create_continuous_detector(event_callback=self._handle_vad_event)
        return None

    def _setup_continuous_streaming(self):
        if self.streaming_manager and self.streaming_manager.is_available():
            recognizer = self.streaming_manager.create_continuous_recognizer(
                result_callback=self._handle_streaming_result
            )
            recognizer.set_recording_rate(self._get_recording_sample_rate())
            return recognizer
        return None

    def _handle_streaming_result(self, text: str, is_final: bool):
        if self.on_streaming_result:
            self.on_streaming_result(text, is_final)

    # `chosen_device` is the microphone from Settings (a name, "default", or a
    # device number from older callers); `chosen_name` its name, which is what
    # finds it again after devices move; `device` is the one opened. Over
    # Remote Desktop that is always the default input (Remote Audio), since
    # the PC's own microphones only send silence there.
    def resolve_device(self, device):
        self.chosen_device = device
        self.chosen_name = None
        if device not in (None, '', 'default') and not isinstance(device, int) and not str(device).isdigit():
            self.chosen_name = str(device).strip()
        if device not in (None, '', "default") and desktop_session.is_remote():
            self.logger.info("Remote desktop: recording from the default input (the microphone your remote client passes through)")
            device = None
        try:
            index = resolve_input(device, self.preferred_host)
        except LookupError:
            self.logger.warning(f"{self.chosen_name} isn't plugged in; recording from the default input until it is")
            _report_problem(f"{self.chosen_name} isn't plugged in, so Koli is recording from "
                            f"{self._device_name(None)} until it is.")
            index = None
        self._open_index(index)

    # Takes device number `index` (None = the default input) when it has input
    # channels, else the default input.
    def _open_index(self, index):
        if index is None:
            self.device = None
            self._resolve_hostapi(None)
            return
        try:
            device_info = sd.query_devices(index)
            if device_info.get('max_input_channels', 0) > 0:
                self.device = index
                if self.chosen_name is None and self.chosen_device not in (None, '', 'default'):
                    self.chosen_name = device_info.get('name')
                self._resolve_hostapi(device_info)
                return
            self.logger.warning(f"Selected device {index} has no input channels; using default input instead")
        except Exception as e:
            self.logger.warning(f"Failed to load device {index}: {e}. Falling back to default input")
        self.device = None
        self._resolve_hostapi(None)

    # Everything that depends on the device's rate, in one place: the stream
    # rate, whether chunks are resampled, the VAD block size (one 16 ms VAD
    # frame's worth of device samples) and the pre-roll length in chunks.
    # Called at start-up and again when the stream falls back to the default
    # device, which may run at another rate: a stale block size there fed the
    # silence detector chunks of the wrong length, so it timed out three times
    # too late on one device and heard no speech at all on another.
    def _configure_for_device_rate(self):
        self._recording_rate = self._get_recording_sample_rate()
        self._needs_resampling_cached = self._needs_resampling()
        if self._needs_resampling_cached:
            self._vad_blocksize = int(VAD_CHUNK_SIZE * self._recording_rate / self.WHISPER_SAMPLE_RATE)
        else:
            self._vad_blocksize = VAD_CHUNK_SIZE
        chunk_seconds = self._vad_blocksize / self._recording_rate
        self._preroll_max_chunks = max(1, int(self.PREROLL_SECONDS / chunk_seconds))

    # A device's name for messages; the default input when `device` is None.
    @staticmethod
    def _device_name(device) -> str:
        try:
            info = sd.query_devices(device) if device is not None else sd.query_devices(kind='input')
            return info['name']
        except Exception:
            return 'your microphone' if device is not None else 'the default microphone'

    def _resolve_hostapi(self, device_info):
        try:
            if device_info is None:
                device_info = sd.query_devices(kind='input')
            hostapi_index = device_info['hostapi']
            self.device_hostapi = sd.query_hostapis(hostapi_index)['name']
            self.device_native_rate = int(device_info['default_samplerate'])
        except Exception as e:
            self.logger.debug(f"Could not determine host API: {e}")
            self.device_hostapi = None
            self.device_native_rate = self.WHISPER_SAMPLE_RATE

    def _needs_resampling(self) -> bool:
        return self.device_hostapi and 'wasapi' in self.device_hostapi.lower()

    def _get_recording_sample_rate(self) -> int:
        if self._needs_resampling():
            return self.device_native_rate
        return self.WHISPER_SAMPLE_RATE

    def _resample_audio(self, audio: np.ndarray, orig_rate: int, target_rate: int) -> np.ndarray:
        if orig_rate == target_rate or len(audio) == 0:
            return audio
        return soxr.resample(audio.flatten(), orig_rate, target_rate).astype(np.float32)

    def _handle_vad_event(self, event: VadEvent):
        self.on_vad_event(event)

    # ── Taps ─────────────────────────────────────────────────────────────────

    def add_tap(self, fn):
        if fn not in self._taps:
            self._taps.append(fn)

    def remove_tap(self, fn):
        if fn in self._taps:
            self._taps.remove(fn)

    # `seconds` of the open microphone as it is, through a tap: no recording
    # starts, so nothing is transcribed or pasted. Trimmed like a dictation's
    # raw clip (see raw_for), for the Settings voice check.
    def capture_raw(self, seconds: float) -> Optional[np.ndarray]:
        chunks = []

        def keep(mono, _when):
            chunks.append(mono.copy())
        self.add_tap(keep)
        try:
            time.sleep(seconds)
        finally:
            self.remove_tap(keep)
        if not chunks:
            return None
        return self._trim_trailing_silence(self._trim_long_pauses(np.concatenate(chunks)))

    # Each chunk to every tap, at Whisper's rate. A tap that raises is logged,
    # never allowed to break the audio callback.
    def _notify_taps(self, chunk: np.ndarray):
        if not self._taps:
            return
        mono = chunk.reshape(-1) if chunk.ndim == 1 or chunk.shape[1] == 1 else chunk.mean(axis=1)
        mono = mono.astype(np.float32, copy=False)
        if self._needs_resampling_cached:
            mono = self._resample_audio(mono, self._recording_rate, self.WHISPER_SAMPLE_RATE)
        when = time.time()
        for fn in list(self._taps):
            try:
                fn(mono, when)
            except Exception as e:
                self.logger.debug(f"Audio tap failed: {e}")

    def _test_audio_source(self):
        try:
            if self.device is not None:
                device_info = sd.query_devices(self.device)
                self.logger.info(f"Using device: {device_info['name']}")
            else:
                default_input = sd.query_devices(kind='input')
                self.logger.info(f"Default source: {default_input['name']}")
        except Exception as e:
            self.logger.error(f"Audio source test failed: {e}")
            raise

    def _start_capture(self):
        self._capture_running = True
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True, name="audio-capture")
        self._capture_thread.start()

    # The audio thread's whole life runs inside app.audio_thread(): on Windows
    # it joins COM, without which a WASAPI microphone can never start here.
    def _capture_loop(self):
        with app.audio_thread():
            self._run_capture()

    # ── Keeping the stream alive ────────────────────────────────────────────
    # The stream is reopened whenever it fails or goes quiet, for as long as
    # Koli runs, waiting a little longer each time (up to RETRY_MAX_WAIT_S).
    # Each reopen rereads the device list, so a microphone plugged back in is
    # seen. While on the default input in place of yours, Windows' live list
    # is checked every few seconds and yours is reopened once it is back.
    def _run_capture(self):
        failures = 0
        while self._capture_running:
            try:
                mine_is_back = self._run_stream()
                failures = 0
                if not mine_is_back:
                    return
                self.logger.info(f"{self.chosen_name} is back; switching to it")
                self._reopen(announce_back=True)
            except Exception as e:
                if not self._capture_running:
                    return
                failures += 1
                self._stream_error = e
                self.logger.warning(f"Audio stream lost ({e}); reopening (attempt {failures})")
                if failures == 1:
                    print("⚠ Microphone stream lost — reopening")
                try:
                    self._reopen()
                except Exception as reopen_err:
                    self.logger.error(f"Could not find a microphone to reopen: {reopen_err}")
                time.sleep(min(self.RETRY_MAX_WAIT_S, 0.5 * failures))

    # One open stream, until Koli stops (False) or your microphone is back
    # while recording from the default input (True). Raises when the stream
    # fails, stops, or sends nothing for STALL_SECONDS (an unplugged device
    # on WASAPI often just stops calling back rather than raising).
    def _run_stream(self) -> bool:
        with sd.InputStream(samplerate=self._recording_rate,
                            channels=self.channels,
                            callback=self._audio_callback,
                            dtype=self.STREAM_DTYPE,
                            blocksize=self._vad_blocksize if self.continuous_vad else None,
                            device=self.device,
                            latency='low') as stream:
            self._last_audio_at = time.monotonic()
            if self._stream_error is not None:
                self.logger.info(f"Microphone stream reopened on {self._device_name(self.device)}")
                self._stream_error = None
            next_check = time.monotonic() + self.MIC_BACK_CHECK_S
            while self._capture_running:
                if self.is_recording:
                    self._check_max_duration_exceeded()
                now = time.monotonic()
                if not stream.active:
                    raise RuntimeError('the stream stopped')
                if now - self._last_audio_at > self.STALL_SECONDS:
                    raise RuntimeError(f'no audio for {self.STALL_SECONDS:.0f} s')
                if now >= next_check:
                    next_check = now + self.MIC_BACK_CHECK_S
                    if self._mine_is_back():
                        return True
                sd.sleep(self.LOOP_SLEEP_MS)
        return False

    # Recording from the default input in place of your microphone, idle, and
    # Windows lists yours again.
    def _mine_is_back(self) -> bool:
        if self.device is not None or not self.chosen_name or self.is_recording:
            return False
        if not self.may_refresh_devices():
            return False   # it couldn't be reopened yet anyway
        if desktop_session.is_remote():
            return False
        try:
            from .platform import audio_sessions
            names = audio_sessions.microphones()
        except Exception:
            return False
        return bool(names) and any(same_mic(n, self.chosen_name) for n in names)

    # Reopens on your microphone when it is plugged in, else the default input
    # (said out loud: it can be another microphone altogether). The device
    # list is reread first: PortAudio reads it once at start, so a microphone
    # plugged back in is otherwise never seen. A recording in progress keeps
    # what it had, converted to the new device's rate.
    def _reopen(self, announce_back: bool = False):
        old_rate = self._recording_rate
        if self.may_refresh_devices():
            _refresh_device_list()
        on_default_before = self.device is None
        self.resolve_device_quietly()
        self._configure_for_device_rate()
        self._carry_buffer(old_rate, self._recording_rate)
        self.logger.info(f"Recording from {self._device_name(self.device)} at {self._recording_rate} Hz "
                         f"(VAD block {self._vad_blocksize} samples)")
        if self.chosen_name and self.device is None and not on_default_before:
            _report_problem(f"{self.chosen_name} stopped working, so Koli is recording from "
                            f"{self._device_name(None)} until it is back.")
        elif announce_back and self.device is not None:
            _report_problem(f"{self.chosen_name} is back. Koli records from it again.")

    # resolve_device without the "isn't plugged in" message (the reopen says
    # what happened in its own words).
    def resolve_device_quietly(self):
        chosen, name = self.chosen_device, self.chosen_name
        try:
            index = None if desktop_session.is_remote() else resolve_input(name or chosen, self.preferred_host)
        except LookupError:
            index = None
        self._open_index(index)
        self.chosen_device, self.chosen_name = chosen, name

    # Audio already in the buffer was captured at `old_rate`; a new device may
    # run at another. While recording it is resampled so the dictation keeps
    # it; idle it is only pre-roll and is dropped.
    def _carry_buffer(self, old_rate: int, new_rate: int):
        if old_rate == new_rate:
            return
        with self._buffer_lock:
            if not self._buffer:
                return
            if not self.is_recording:
                self._buffer.clear()
                return
            audio = np.concatenate(list(self._buffer), axis=0).reshape(-1).astype(np.float32)
            carried = soxr.resample(audio, old_rate, new_rate).astype(self.STREAM_DTYPE).reshape(-1, 1)
            self._buffer.clear()
            self._buffer.append(carried)

    def _audio_callback(self, audio_data, frames, _time, status):
        chunk = audio_data.copy()
        # A stereo stream is mixed to one channel here, once: everything after
        # (resampling, VAD, Whisper) treats the samples as mono, and a flattened
        # two-channel array interleaves left and right into garbled audio.
        if chunk.ndim > 1 and chunk.shape[1] > 1:
            chunk = chunk.mean(axis=1, keepdims=True).astype(audio_data.dtype, copy=False)
        self._last_audio_at = time.monotonic()   # the capture loop's "still alive"
        recording = self.is_recording

        # The level is also measured while "Test my microphone" is open, which
        # never starts a recording.
        if recording or self.level_preview:
            self._current_level = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
        else:
            self._current_level = 0.0

        with self._buffer_lock:
            self._buffer.append(chunk)
            # Trim to pre-roll size while idle. (VAD/streaming below operate on the
            # local `chunk` copy, not the buffer, so they stay outside the lock.)
            if not recording:
                while len(self._buffer) > self._preroll_max_chunks:
                    self._buffer.popleft()

        self._notify_taps(chunk)

        if recording:
            if self.continuous_vad and frames == self._vad_blocksize:
                if self._needs_resampling_cached:
                    chunk_16k = self._resample_audio(chunk, self._recording_rate, self.WHISPER_SAMPLE_RATE)
                    self.continuous_vad.process_chunk(chunk_16k.reshape(-1, 1))
                else:
                    self.continuous_vad.process_chunk(chunk)

            # The local words model runs only for a dictation whose bubble
            # uses it (live_session_flow.py sets this per recording).
            if self.continuous_streaming and self.stream_partials:
                self.continuous_streaming.process_chunk(chunk)

        if status:
            self.logger.debug(f"Audio callback status: {status}")

    # `silence_timeout` overrides the configured silence stop for this one
    # recording (auto-dictation listens for a few seconds only); None restores
    # the configured value, so a hotkey recording never inherits the short one.
    def start_recording(self, silence_timeout: Optional[float] = None, phrase_pause: Optional[float] = None):
        if self.is_recording:
            return False

        self.logger.info("Starting audio recording...")
        self.recording_start_time = time.time()

        if self.continuous_vad:
            configured = getattr(self.vad_manager, 'vad_silence_timeout_seconds', None)
            wanted = silence_timeout if silence_timeout else configured
            if wanted and wanted != self.continuous_vad.silence_timeout_sec:
                self.continuous_vad.set_silence_timeout(wanted)
            self.continuous_vad.set_phrase_pause(phrase_pause or 0)
            self.continuous_vad.reset()
        if self.continuous_streaming:
            self.continuous_streaming.reset()

        preroll_chunks = len(self._buffer)
        self.is_recording = True
        self.logger.debug(f"Recording started with {preroll_chunks} preroll chunks (~{preroll_chunks * self._vad_blocksize / self._recording_rate:.2f}s)")
        return True

    # Hands over everything recorded so far and keeps recording: phrase
    # delivery pastes each pause-separated phrase while you go on talking. The
    # length limit restarts with each phrase, so a long dictation is not cut.
    def take_recorded_audio(self) -> Optional[np.ndarray]:
        if not self.is_recording:
            return None
        with self._buffer_lock:
            snapshot = list(self._buffer)
            self._buffer.clear()
        self.recording_start_time = time.time()
        return self._build_audio_array(snapshot)

    def stop_recording(self) -> Optional[np.ndarray]:
        if not self.is_recording:
            return None
        with self._buffer_lock:
            snapshot = list(self._buffer)
            self.is_recording = False
            self._buffer.clear()
        return self._build_audio_array(snapshot)

    # Says which microphone sent nothing and where to look, at most once a
    # minute (phrases of one dictation would otherwise repeat it).
    SILENT_MIC_REPORT_EVERY_S = 60.0
    last_clip_silent = False

    def _report_silent_mic(self):
        now = time.monotonic()
        if now - self.__dict__.get('_silent_reported_at', -1e9) < self.SILENT_MIC_REPORT_EVERY_S:
            return
        self._silent_reported_at = now
        _report_problem(f"No sound came from {self._device_name(getattr(self, 'device', None))}. "
                        "Check that it isn't muted, or pick another microphone in Settings. "
                        "Windows can also block microphones: Settings › Privacy & security › Microphone.")

    def _build_audio_array(self, chunks) -> Optional[np.ndarray]:
        if not chunks:
            print("   ✗ No audio data recorded!")
            return None

        audio_array = np.concatenate(chunks, axis=0)
        peak = float(np.max(np.abs(audio_array))) if len(audio_array) else 0.0
        # Digital silence: even a quiet room leaves a noise floor, so this is
        # a muted or blocked microphone, not "no speech" (after Handy).
        self.last_clip_silent = peak < 1e-5
        if self.last_clip_silent:
            self.logger.warning(f"Recorded audio is silent (peak={peak:.2e}) — mic may be muted or permission denied")
            print("   ⚠ Recording captured pure silence — mic muted, unplugged, or OS permission denied?")
            self._report_silent_mic()

        if self._needs_resampling_cached:
            self.logger.info(f"Resampling from {self._recording_rate} Hz to {self.WHISPER_SAMPLE_RATE} Hz")
            audio_array = self._resample_audio(audio_array, self._recording_rate, self.WHISPER_SAMPLE_RATE)
        # The microphone as it was, for the voiceprint (see raw_for).
        raw_array = audio_array

        if self._noise_suppression_config.get('enabled'):
            from .noise_suppression import apply_noise_reduction
            strength = float(self._noise_suppression_config.get('strength', 0.75))
            audio_array = apply_noise_reduction(audio_array, self.WHISPER_SAMPLE_RATE, strength)


        # The cuts are decided on the noise-reduced clip (the room doesn't read
        # as sound there) and made at the same samples in the raw clip, so
        # Whisper's piece times fit both. The raw clip losing its pauses also
        # keeps a TV in them out of your voiceprint.
        kept = self._trim_long_pauses(np.arange(len(audio_array)), decide_on=audio_array)
        kept = self._trim_trailing_silence(kept, decide_on=audio_array[kept])
        audio_array, raw_array = audio_array[kept], raw_array[kept]

        # Whisper mode boosts last: the trims above judge silence against an
        # absolute level, and a boosted noise floor would read as speech,
        # switching off the anti-hallucination trimming.
        gain = 1.0
        if self._whisper_mode_gain:
            from .audio_gain import boost_gain
            gain = boost_gain(audio_array, self._whisper_mode_gain)
            if gain > 1.0:
                audio_array = np.clip(audio_array * gain, -1.0, 1.0).astype(audio_array.dtype, copy=False)
        self._recent_clips.append((audio_array, raw_array))

        duration = self.get_audio_duration(audio_array)
        self.logger.info(f"Recorded {duration:.2f}s (incl. preroll, mid-pauses + trailing silence trimmed)")
        return audio_array

    # The same clip before noise reduction and the Whisper-mode boost, cut at
    # the same places (the array itself if this recorder didn't hand it out).
    # "Only my voice" judges this: on a processed clip a TV scored 0.45 against
    # your voiceprint, over the 0.43 "that's you" line, and 0.11 on the raw
    # microphone; and noise reduction would move your measured level.
    def raw_for(self, audio: np.ndarray) -> np.ndarray:
        for array, raw in reversed(self._recent_clips):
            if array is audio:
                return raw
        self.logger.debug("Raw clip not kept (more than 4 clips since); the processed one is judged")
        return audio

    # The whisper-mode max gain, or None when it's off. Validated once here: a
    # hand-edited `whisper_mode: true` or `max_gain: 8x` must not break every
    # recording, so bad values fall back to the default boost.
    @staticmethod
    def _parse_whisper_mode(config) -> Optional[float]:
        if not isinstance(config, dict) or config.get('enabled') is not True:
            return None
        try:
            gain = float(config.get('max_gain', 8.0))
        except (TypeError, ValueError):
            gain = 8.0
        return gain if gain > 1.0 else None

    # `decide_on`: judge the pauses on this array and cut `audio` (the same
    # length, e.g. its sample indices) at the same places.
    def _trim_long_pauses(self, audio: np.ndarray, decide_on: Optional[np.ndarray] = None) -> np.ndarray:
        judged = audio if decide_on is None else decide_on
        mono = judged.mean(axis=1) if judged.ndim > 1 else judged
        win = int(0.05 * self.WHISPER_SAMPLE_RATE)
        if win < 1:
            return audio
        long_silence = int(self.LONG_PAUSE_SECONDS / 0.05)
        keep_windows = max(1, int(self.LONG_PAUSE_REPLACEMENT_SECONDS / 0.05))
        n = len(mono) // win
        if n < long_silence * 2:
            return audio

        voiced = np.empty(n, dtype=bool)
        for i in range(n):
            block = mono[i * win:(i + 1) * win]
            voiced[i] = float(np.sqrt(np.mean(block ** 2))) > self.SILENCE_RMS_THRESHOLD

        pieces = []
        i = 0
        modified = False
        while i < n:
            if voiced[i]:
                j = i
                while j < n and voiced[j]:
                    j += 1
                pieces.append(audio[i * win:j * win])
                i = j
            else:
                j = i
                while j < n and not voiced[j]:
                    j += 1
                run_windows = j - i
                if run_windows > long_silence:
                    pieces.append(audio[i * win:(i + keep_windows) * win])
                    modified = True
                else:
                    pieces.append(audio[i * win:j * win])
                i = j

        if not modified:
            return audio
        if (len(mono) % win) > 0:
            pieces.append(audio[n * win:])
        return np.concatenate(pieces, axis=0) if pieces else audio

    def _trim_trailing_silence(self, audio: np.ndarray, decide_on: Optional[np.ndarray] = None) -> np.ndarray:
        judged = audio if decide_on is None else decide_on
        mono = judged.mean(axis=1) if judged.ndim > 1 else judged
        window = int(0.02 * self.WHISPER_SAMPLE_RATE)
        if window < 1 or len(mono) < window * 4:
            return audio
        trim_samples = int(self.TRAILING_SILENCE_TRIM_SECONDS * self.WHISPER_SAMPLE_RATE)
        last_voice = len(mono)
        i = len(mono) - window
        while i > 0:
            block = mono[i:i + window]
            rms = float(np.sqrt(np.mean(block ** 2)))
            if rms > self.SILENCE_RMS_THRESHOLD:
                last_voice = i + window
                break
            i -= window
        keep_until = min(len(mono), last_voice + trim_samples)
        if keep_until >= len(mono) - window:
            return audio
        return audio[:keep_until]

    def get_current_level(self) -> float:
        return self._current_level

    # Settings that apply from the next recording without reopening the stream.
    def apply_live_config(self, max_duration=None, noise_suppression=None, whisper_mode=None):
        if max_duration is not None:
            self.max_duration = max_duration
        if noise_suppression is not None:
            self._set_noise_suppression(dict(noise_suppression))
        if whisper_mode is not None:
            self._whisper_mode_gain = self._parse_whisper_mode(whisper_mode)

    # Turned on, it is loaded now in the background: its first run is seconds
    # slow, and a phrase prepared that slowly can outlive its session.
    def _set_noise_suppression(self, config: dict):
        self._noise_suppression_config = config
        if config.get('enabled'):
            from . import noise_suppression
            noise_suppression.warm_up_in_background(self.WHISPER_SAMPLE_RATE)

    # Rebuild the realtime voice detector after its thresholds change.
    def refresh_vad(self):
        self.continuous_vad = self._setup_continuous_vad_monitoring()

    # Device details for the Live screen's chips.
    def describe(self) -> dict:
        name = None
        try:
            info = sd.query_devices(self.device) if self.device is not None else sd.query_devices(kind='input')
            name = info.get('name')
        except Exception:
            pass
        return {
            'device_id': self.device, 'name': name, 'host': self.device_hostapi,
            'native_rate': self.device_native_rate, 'recording_rate': self._recording_rate,
            'channels': self.channels, 'dtype': self.dtype,
            'preroll_s': self.PREROLL_SECONDS, 'max_duration': self.max_duration,
            'noise_suppression': bool(self._noise_suppression_config.get('enabled')),
            'whisper_mode': bool(self._whisper_mode_gain),
        }

    def cancel_recording(self):
        if not self.is_recording:
            return
        with self._buffer_lock:
            self.is_recording = False
            self._buffer.clear()
        self.recording_start_time = None

    def shutdown(self):
        self._capture_running = False
        if self._capture_thread:
            self._capture_thread.join(timeout=self.THREAD_JOIN_TIMEOUT)

    def _check_max_duration_exceeded(self) -> bool:
        if self.max_duration <= 0 or not self.recording_start_time:
            return False
        if time.time() - self.recording_start_time < self.max_duration:
            return False

        self.logger.info(f"Maximum recording duration of {self.max_duration}s reached")
        print(f"⏰ Maximum recording duration of {self.max_duration}s reached - stopping recording")

        with self._buffer_lock:
            snapshot = list(self._buffer)
            self.is_recording = False
            self._buffer.clear()
        audio_data = self._build_audio_array(snapshot)
        if self.on_max_duration_reached:
            self.on_max_duration_reached(audio_data)
        return True

    def get_recording_status(self) -> bool:
        return self.is_recording

    def get_audio_duration(self, audio_data: np.ndarray) -> float:
        if audio_data is None or len(audio_data) == 0:
            return 0.0
        return len(audio_data) / self.sample_rate

    def get_device_id(self) -> Optional[int]:
        if self.device is not None:
            return self.device
        return sd.query_devices(kind='input')['index']

    @staticmethod
    def get_available_audio_devices(host_filter: Optional[str] = None):
        try:
            all_devices = sd.query_devices()
            hostapis = sd.query_hostapis()
        except Exception as e:
            logging.getLogger(__name__).error(f"Failed to enumerate audio devices: {e}")
            return []

        devices = []
        host_filter_lower = host_filter.lower() if host_filter else None

        for idx, device in enumerate(all_devices):
            if device.get('max_input_channels', 0) <= 0:
                continue

            hostapi_index = device['hostapi']
            hostapi_info = hostapis[hostapi_index]
            hostapi_name = hostapi_info['name']

            if host_filter_lower and hostapi_name.lower() != host_filter_lower:
                continue

            devices.append({
                'id': idx,
                'name': device['name'],
                'input_channels': device['max_input_channels'],
                'sample_rate': device['default_samplerate'],
                'hostapi': hostapi_name
            })

        return devices
