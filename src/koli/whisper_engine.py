# whisper_engine.py
# The default speech-recognition backend, wrapping faster-whisper (CTranslate2).
# Loads the model once at startup and warms it with a dummy transcription so the
# user's first real recording isn't slowed by lazy initialisation. Model switches
# happen on a background thread with progress callbacks so the UI stays live.
# `whisper_engine_cpp.py` mirrors this class's API for the opt-in whisper.cpp
# backend — keep the two signatures in sync.

import logging
import queue
import time
import threading
from typing import Optional, Callable

import numpy as np
from faster_whisper import WhisperModel

# Approximate on-disk download sizes for the standard Whisper models, shown in
# the first-run download message so the wait is expected, not mysterious. Keyed
# by the leading size token (handles ".en" and "large-v3" style keys). Unknown
# models just omit the size hint.
_MODEL_SIZE_HINTS = {
    'tiny': '~75 MB',
    'base': '~141 MB',
    'small': '~465 MB',
    'medium': '~1.5 GB',
    'large': '~3 GB',
    'distil': '~1.5 GB',
}


def _model_size_hint(model_key: str) -> str:
    key = (model_key or '').lower()
    for prefix, size in _MODEL_SIZE_HINTS.items():
        if key.startswith(prefix):
            return size
    return ''


# ── The engine thread ────────────────────────────────────────────────────────
# ctranslate2 keeps per-thread CUDA state and tears it down in a thread-exit
# callback that calls cudaSetDevice. On a machine with two graphics cards that
# call threw a std::runtime_error with nowhere to go, and Windows aborted the
# whole process (crash dumps of 2026-10-08: RtlExitUserThread → TLS callback →
# ctranslate2::set_device_index → throw). Any Python thread that transcribed
# and then ended did it: the silence-stop path ran the transcription on a
# thread spawned per event, while hotkey stops ran on the listener thread,
# which never ends. So every ctranslate2 call goes through this one daemon
# thread, which is never joined: it ends only with the process, and no
# thread-exit callbacks run then.
class _EngineThread:
    def __init__(self, name: str = 'whisper-engine'):
        self._name = name
        self._queue = queue.SimpleQueue()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # Runs fn on the engine thread and waits for it; re-raises what it raised.
    # Already on the engine thread (a nested call): run it inline.
    def run(self, fn, *args, **kwargs):
        if threading.current_thread() is self._thread:
            return fn(*args, **kwargs)
        self._ensure_started()
        done = threading.Event()
        box = {}
        self._queue.put((fn, args, kwargs, done, box))
        done.wait()
        if 'error' in box:
            raise box['error']
        return box['result']

    def is_engine_thread(self) -> bool:
        return threading.current_thread() is self._thread

    def _ensure_started(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name=self._name, daemon=True)
                self._thread.start()

    def _loop(self):
        while True:
            fn, args, kwargs, done, box = self._queue.get()
            try:
                box['result'] = fn(*args, **kwargs)
            except BaseException as e:  # noqa: BLE001 - handed back to the caller
                box['error'] = e
            finally:
                done.set()


ENGINE_THREAD = _EngineThread()


# A problem you must see (bubble and app). The UI's event module is optional
# here: the engine also runs headless (--serve, tests).
def _report_problem(message: str) -> None:
    try:
        from .ui.events import report_problem
        report_problem(message)
    except Exception:
        pass


class WhisperEngine:
    def __init__(self,
                 model_key: str = "tiny",
                 device: str = "cpu",
                 compute_type: str = "int8",
                 language: str = None,
                 beam_size: int = 5,
                 initial_prompt: str = "",
                 hotwords: list = None,
                 task: str = "transcribe",
                 vad_manager = None,
                 model_registry = None,
                 log_transcriptions: bool = False,
                 device_index: int = 0,
                 language_model: Optional[tuple] = None,
                 allowed_languages: Optional[list] = None):

        self.model_key = model_key
        # The languages auto-detect may pick from (["en", "he"]); empty = any.
        # Stops a short clip from coming back as a language you never speak.
        self.allowed_languages = [str(code) for code in (allowed_languages or [])]
        # An optional second model for one language, as (language, model key):
        # ("he", "hebrew-large-v3-turbo") sends Hebrew to ivrit-ai's model.
        self.language_model = None
        self.language_model_lang, self.language_model_key = language_model or (None, None)
        self.device = device
        # Which graphics card, when there is more than one (0 = the first).
        self.device_index = int(device_index or 0)
        self.compute_type = compute_type
        self.language = None if language == 'auto' else language
        self.beam_size = beam_size
        self.initial_prompt = initial_prompt or None
        self.hotwords = ", ".join(hotwords) if hotwords else None
        self.task = task if task in ('transcribe', 'translate') else 'transcribe'
        self.model = None
        self.logger = logging.getLogger(__name__)
        self.registry = model_registry
        self.log_transcriptions = log_transcriptions

        self._loading_thread = None
        self._progress_callback = None
        # Details of the most recent transcription, for the Live trace.
        self.last_info = None

        self.vad_manager = vad_manager

        self._load_model()
    
    def _get_model_source(self, model_key: str) -> str:
        if self.registry:
            return self.registry.get_source(model_key)
        return model_key

    def _is_model_cached(self, model_key: str = None) -> bool:
        if model_key is None:
            model_key = self.model_key
        if self.registry:
            return self.registry.is_model_cached(model_key)
        return False

    def _load_model(self):
        try:
            print(f"🧠 Loading Whisper AI model [{self.model_key}]...")

            was_cached = self._is_model_cached()
            if not was_cached:
                size = _model_size_hint(self.model_key)
                size_note = f" ({size})" if size else ""
                print(f"⬇  Downloading the '{self.model_key}' model{size_note} — "
                      "first run only. This can take a few minutes; a progress bar "
                      "appears below.")

            if self.device == 'cuda':
                self._verify_gpu_runtime()

            model_source = self._get_model_source(self.model_key)
            self.model = ENGINE_THREAD.run(
                WhisperModel,
                model_source,
                device=self.device,
                device_index=self.device_index if self.device == 'cuda' else 0,
                compute_type=self.compute_type
            )

            if not was_cached:
                print("\n")  # Workaround for download status bar misplacement

            print(f"   ✓ Whisper model [{self.model_key}] ready!")
            device_label = "GPU" if self.device == "cuda" else "CPU"
            print(f"   ✓ Running on {device_label} with {self.compute_type} precision")
            ENGINE_THREAD.run(self._warmup)

        except Exception as e:
            self.logger.error(f"Failed to load Whisper model: {e}")
            raise
        self._load_language_model()

    # The second model, loaded next to the main one so switching per dictation
    # costs nothing. If it fails, dictation carries on with the main model.
    def _load_language_model(self):
        if not self.language_model_lang or not self.language_model_key:
            return
        try:
            print(f"🧠 Loading the {self.language_model_lang} model [{self.language_model_key}]...")
            self.language_model = ENGINE_THREAD.run(
                WhisperModel,
                self._get_model_source(self.language_model_key),
                device=self.device,
                device_index=self.device_index if self.device == 'cuda' else 0,
                compute_type=self.compute_type
            )
            print(f"   ✓ {self.language_model_lang} speech goes to [{self.language_model_key}]")
        except Exception as e:
            self.language_model = None
            self.logger.error(f"Second model [{self.language_model_key}] failed to load, using the main model: {e}")
            print(f"   ⚠ The {self.language_model_lang} model could not load; the main model does everything")
            _report_problem(f"The second model ({self.language_model_key}) could not load, so the main model "
                            f"transcribes everything. {e}")

    # Which model transcribes this clip, in which language, with which prompt.
    # A language set in settings, a rule or a profile is used as is; a list of
    # one language works the same way. Otherwise, with a language list or a
    # second model, the main model detects the language (fast, one pass) and
    # passes it on, so Whisper doesn't detect again. Speech in the second
    # model's language goes to that model, without the prompt: an English
    # prompt pushes a Hebrew model off course. Runs on the engine thread,
    # inside the decode.
    # Returns (model, key, language, prompt, detection probability or None).
    # `language` overrides the engine's setting for one call (meeting mode):
    # None keeps the setting, 'auto' detects, anything else forces that one.
    # A detected language goes to the second model only when Whisper is this
    # sure of it: told "Hebrew", that model translates whatever else it hears.
    # On 2026-10-12 a 47 s English dictation scored Hebrew 0.45 and half of it
    # came out in Hebrew; real Hebrew dictations scored 0.8-1.0.
    SECOND_MODEL_MIN_CONFIDENCE = 0.6

    def _route(self, audio: np.ndarray, language: Optional[str] = None) -> tuple:
        self._raw_language_probability = None
        second = getattr(self, 'language_model', None)
        allowed = getattr(self, 'allowed_languages', None) or []
        forced = self.language if language is None else (None if language == 'auto' else language)
        language, probability = forced, None
        if not language and len(allowed) == 1:
            language = allowed[0]
        detected = False
        if not language and (allowed or second is not None):
            language, probability = self._detect_language(audio, allowed)
            detected = True
        if second is not None and language == self.language_model_lang:
            sure = getattr(self, '_raw_language_probability', None)
            if not detected or (sure is not None and sure >= self.SECOND_MODEL_MIN_CONFIDENCE):
                return second, self.language_model_key, language, None, probability
            # Unsure: the main model, not told a language, writes each one as spoken.
            self.logger.info(f"Only {sure or 0:.2f} sure it's {language}: the main model writes it as spoken "
                             f"(the {language} model would translate other speech)")
            return self.model, self.model_key, None, self.initial_prompt, probability
        return self.model, self.model_key, language, self.initial_prompt, probability

    # The clip's language and how sure the main model is. Without a list, an
    # unsure guess (under 50%) comes back as None so Whisper decides while
    # transcribing. With a list, the best-scoring listed language always wins,
    # and the certainty is its share among the listed ones: Icelandic 55%,
    # Hebrew 30%, English 5% with [en, he] gives Hebrew at 86%. The raw score
    # of the language picked (30% there) is kept too: the share is always high
    # with a short list, and auto-dictation's "is this even speech?" guard
    # needs how sure Whisper really was.
    def _detect_language(self, audio: np.ndarray, allowed: list) -> tuple:
        # English-only models (base.en, ...) can't detect; they only do English.
        if not getattr(getattr(self.model, 'model', None), 'is_multilingual', True):
            return 'en', None
        detected, probability, all_probs = self.model.detect_language(audio)
        if not allowed:
            self.logger.info(f"Detected {detected} ({probability:.2f})")
            self._raw_language_probability = probability
            return (detected if probability >= 0.5 else None), probability
        scores = {code: score for code, score in (all_probs or []) if code in allowed}
        total = sum(scores.values())
        if not total:
            self.logger.info(f"Detected {detected} ({probability:.2f}); no score for {', '.join(allowed)}")
            return (detected if detected in allowed else None), None
        best = max(scores, key=scores.get)
        share = scores[best] / total
        self._raw_language_probability = scores[best]
        self.logger.info(f"Detected {detected} ({probability:.2f}); among {', '.join(allowed)}: {best} ({share:.2f})")
        return best, share

    # Fail fast when the CUDA runtime is incomplete. The model itself loads
    # without cuBLAS/cuDNN; their absence only shows on the first inference,
    # as a hang rather than an error (issue #15). Raising RuntimeError here
    # routes through the caller's GPU-failure path, which offers GPU setup or
    # CPU instead of freezing on the user's first dictation.
    def _verify_gpu_runtime(self):
        try:
            from .hardware_detection import missing_gpu_libraries
        except ImportError:
            return  # no platform backend (Linux CI): nothing to probe
        missing = missing_gpu_libraries()
        if missing:
            raise RuntimeError(
                f"CUDA libraries not found: {', '.join(missing)}. "
                "Re-run GPU setup to install them, or switch to CPU."
            )

    def _load_model_async(self,
                          new_model_key: str,
                          progress_callback: Optional[Callable[[str], None]] = None):
        def _background_loader():
            # Capture before any callback so the except handler can always restore
            # it (a progress_callback that raises must not cause UnboundLocalError).
            old_model_key = self.model_key
            try:
                if progress_callback:
                    progress_callback("Checking model cache...")

                was_cached = self._is_model_cached(new_model_key)

                if progress_callback:
                    if was_cached:
                        progress_callback("Loading cached model...")
                    else:
                        progress_callback("Downloading model...")

                self.logger.info(f"Loading Whisper model: {new_model_key} (async)")

                model_source = self._get_model_source(new_model_key)
                new_model = ENGINE_THREAD.run(
                    WhisperModel,
                    model_source,
                    device=self.device,
                    device_index=self.device_index if self.device == 'cuda' else 0,
                    compute_type=self.compute_type
                )
                self.model = new_model

                self.model_key = new_model_key
                self.logger.info(f"Whisper model [{new_model_key}] loaded successfully (async)")

                if progress_callback:
                    progress_callback("Model ready!")

            except Exception as e:
                self.model_key = old_model_key
                self.logger.error(f"Failed to load Whisper model async: {e}")
                if progress_callback:
                    progress_callback(f"Failed to load model: {e}")
                raise
            finally:
                self._loading_thread = None
                self._progress_callback = None

        if self._loading_thread and self._loading_thread.is_alive():
            self.logger.warning("Model loading already in progress, ignoring new request")
            return
        
        self._progress_callback = progress_callback
        self._loading_thread = threading.Thread(target=_background_loader, daemon=True)
        self._loading_thread.start()
    
    def is_loading(self) -> bool:
        return self._loading_thread is not None and self._loading_thread.is_alive()
    

    def _warmup(self):
        try:
            silent = np.zeros(16000, dtype=np.float32)
            t0 = time.time()
            segments, _ = self.model.transcribe(
                silent, language=self.language, beam_size=1, vad_filter=False
            )
            for _ in segments:
                pass
            self.logger.info(f"Whisper warmup completed in {time.time() - t0:.2f}s")
        except Exception as e:
            self.logger.debug(f"Whisper warmup skipped: {e}")

    # Mono float32 @ 16 kHz in, plain text out (None/'' when there's no speech).
    # An optional VAD pre-check short-circuits silent clips so we don't pay for a
    # full decode on an accidental hotkey press. Callers rely on this contract —
    # whisper_engine_cpp and system_audio both feed it the same array shape.
    # `use_prompt=False` leaves out the dictation prompt (meeting mode): it is
    # a style hint for the app in front ("Friendly tone."), which a meeting
    # never wants, and on a short or quiet clip Whisper writes it back as text.
    # The wake word's checks pass `beam_size` (1, fast), `task` ("transcribe",
    # even under the Translate profile) and `quiet`: nothing printed or logged
    # about the text, and `last_info` left to the dictation it describes.
    def transcribe_audio(self,
                         audio_data: np.ndarray,
                         language: Optional[str] = None,
                         use_prompt: bool = True,
                         beam_size: Optional[int] = None,
                         task: Optional[str] = None,
                         quiet: bool = False) -> Optional[str]:
        if self.model is None:
            return None
        
        if audio_data is None or len(audio_data) == 0:
            self.logger.warning("No audio data to transcribe")
            return None
        
        try:
            speech_detected = True
            if self.vad_manager and self.vad_manager.is_available():
                speech_detected = self.vad_manager.check_audio_for_speech(audio_data)
            
            if not speech_detected:
                if not quiet:
                    print("   ✗ No speech detected, skipping transcription")
                return None
                       
            start_time = time.time() # Time transcription for user feedback
            
            # Prep audio for faster-whisper
            if len(audio_data.shape) > 1:
                audio_data = audio_data.flatten()
            
            audio_data = audio_data.astype(np.float32)
            
            # The segments come back as a lazy generator; consuming it is where
            # the decoding runs, so that stays on the engine thread too, along
            # with picking the model. Each piece keeps Whisper's own numbers on
            # how sure it was, so auto-dictation can leave out what was noise.
            def decode():
                model, key, lang, prompt, detected_probability = self._route(audio_data, language)
                kwargs = dict(beam_size=beam_size or self.beam_size, language=lang, task=task or self.task,
                              condition_on_previous_text=False)
                if prompt and use_prompt:
                    kwargs["initial_prompt"] = prompt
                if self.hotwords:
                    kwargs["hotwords"] = self.hotwords
                segments, info = model.transcribe(audio_data, **kwargs)
                pieces = [{'text': s.text, 'start': float(s.start), 'end': float(s.end),
                           'no_speech_prob': round(float(s.no_speech_prob), 3),
                           'avg_logprob': round(float(s.avg_logprob), 3),
                           'compression_ratio': round(float(s.compression_ratio), 2)} for s in segments]
                return pieces, info, key, detected_probability, getattr(self, '_raw_language_probability', None)

            pieces, info, used_key, detected_probability, raw_probability = ENGINE_THREAD.run(decode)
            transcribed_text = ''.join(p['text'] for p in pieces).strip()
            if quiet:
                self.logger.debug(f"Wake check: {time.time() - start_time:.2f}s, {len(transcribed_text)} chars")
                return transcribed_text or None

            end_time = time.time()
            transcription_time = end_time - start_time
            print(f"   ✓ Transcription completed in {transcription_time:.1f} seconds")

            # Log some info about what we transcribed. A language Koli detected
            # and passed on reads as certain to Whisper; the detection's own
            # confidence is the honest one.
            detected_language = info.language
            confidence = detected_probability if detected_probability is not None else info.language_probability
            # `language_raw_probability`: Whisper's own score for the language
            # picked (not its share of your language list), for the guards;
            # `audio_seconds`: how long the clip was.
            raw = raw_probability if raw_probability is not None else confidence
            self.last_info = {
                'language': detected_language,
                'language_probability': round(float(confidence or 0), 3),
                'language_raw_probability': round(float(raw or 0), 3),
                'audio_seconds': round(len(audio_data) / 16000, 2),
                'seconds': round(transcription_time, 2),
                'model': used_key, 'device': self.device,
                'compute_type': self.compute_type, 'beam_size': self.beam_size,
                'backend': 'faster_whisper',
                'segments': pieces,
            }
            self.logger.info(f"Transcription complete. Language: {detected_language} (confidence: {confidence:.2f}) - Time: {transcription_time:.2f}s")
            # The numbers only, never the text: why a piece was kept or left out.
            self.logger.info("Pieces (no-speech / log-prob / repetition): " + ', '.join(
                f"{p['no_speech_prob']:.2f}/{p['avg_logprob']:.2f}/{p['compression_ratio']:.1f}" for p in pieces))
            if self.log_transcriptions:
                self.logger.info(f"Transcribed text: '{transcribed_text}'")
            else:
                self.logger.info(f"Transcribed {len(transcribed_text)} chars")
            
            if transcribed_text:
                print(f"   ✓ Transcribed: '{transcribed_text}'")
                return transcribed_text
            else:
                self.logger.info("Transcription was empty")
                return None
                
        except Exception as e:
            self.logger.error(f"Transcription failed: {e}")
            if not quiet:   # a failing wake check would otherwise pop a problem per burst
                _report_problem(f"Transcription failed: {e}")
            return None
    
    
    # Gives the graphics memory back now instead of whenever Python collects
    # the engine: after a few model switches the old weights would otherwise
    # keep a card too full for a second model. Called on a swapped-out engine.
    def unload(self) -> None:
        def free():
            for model in (self.model, getattr(self, 'language_model', None)):
                if model is None:
                    continue
                try:
                    model.model.unload_model()
                except Exception as e:
                    self.logger.debug(f"Unloading a model failed: {e}")
        ENGINE_THREAD.run(free)
        self.model = None
        self.language_model = None
        self.logger.info(f"Unloaded [{self.model_key}]"
                         + (f" and [{self.language_model_key}]" if getattr(self, 'language_model_key', None) else ''))

    def change_model(self,
                     new_model_key: str,
                     progress_callback: Optional[Callable[[str], None]] = None):
        
        if new_model_key == self.model_key:
            if progress_callback:
                progress_callback("Model already loaded")
            return
        
        self._load_model_async(new_model_key, progress_callback)
    