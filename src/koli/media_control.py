# media_control.py
# What happens to other sound while you dictate: once Koli hears you speak (or
# when the recording starts, if voice detection is off), pause the music that
# is playing and/or lower the computer's volume to a chosen level (only if it
# is louder than that); when the recording ends, play the music again and put
# the volume back. The OS calls live in platform/*/media.py; this module holds
# the rules.

import logging
import queue
import threading

logger = logging.getLogger(__name__)


# Runs the media-player calls one after another on a background thread, so the
# hotkey and audio threads never wait for the players, and a "play again" can
# never overtake the "pause" it undoes.
class _SerialWorker:
    def __init__(self):
        self._jobs = queue.Queue()
        self._thread = None
        self._start_lock = threading.Lock()

    def __call__(self, fn):
        with self._start_lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True, name='media-control')
                self._thread.start()
        self._jobs.put(fn)

    def _run(self):
        while True:
            fn = self._jobs.get()
            try:
                fn()
            except Exception as e:
                logger.warning(f"Media control job failed: {e}")


class MediaControl:
    # `backend` provides pause_playing(), resume_playing(apps),
    # get_master_volume() (0..1 or None) and set_master_volume(0..1).
    # `run_async` runs the player and volume calls off the calling thread, in
    # order.
    def __init__(self, backend=None, run_async=None):
        self._backend = backend
        self._run_async = run_async or _SerialWorker()
        self._lock = threading.Lock()
        self._waiting = None           # what this recording does once you speak
        self._quieted = False          # something was quieted, so the end undoes it
        # Worker thread only:
        self._paused_apps = []         # what the last pause stopped
        self._restore_to = None        # the volume before we lowered it
        self._lowered_to = None        # the volume we set, to notice a user change

    @property
    def backend(self):
        if self._backend is None:
            from .platform import media
            self._backend = media
        return self._backend

    # ── Recording start / speech / end ───────────────────────────────────────

    # `hears_voice`: voice detection runs during this recording and will call
    # on_speech(). Without it everything happens right away.
    def on_recording_start(self, audio_cfg: dict, hears_voice: bool = False):
        plan = self._plan(audio_cfg or {})
        if plan is None:
            return
        if hears_voice:
            with self._lock:
                self._waiting = plan
        else:
            self._quiet_down(plan)

    # You started talking: quiet the rest down now, once per recording. A
    # speech event that arrives after the recording ended does nothing.
    def on_speech(self):
        with self._lock:
            plan, self._waiting = self._waiting, None
        if plan is not None:
            self._quiet_down(plan)

    # Called on every state change away from recording, so it must be cheap
    # and do the work only once.
    def on_recording_end(self):
        with self._lock:
            self._waiting = None
            undo, self._quieted = self._quieted, False
        if undo:
            self._run_async(self._bring_back)

    # What the settings ask for: (pause the music?, volume level or None).
    @staticmethod
    def _plan(audio_cfg: dict):
        pause = bool(audio_cfg.get('pause_media_on_record'))
        lower = audio_cfg.get('lower_volume') or {}
        level = lower.get('level', 20) if lower.get('enabled') else None
        return (pause, level) if pause or level is not None else None

    def _quiet_down(self, plan):
        pause, level = plan
        with self._lock:
            self._quieted = True

        def run():
            if pause:
                self._pause_media()
            if level is not None:
                self._lower_volume(level)
        self._run_async(run)

    def _bring_back(self):
        self._resume_media()
        self._restore_volume()

    # ── Music ────────────────────────────────────────────────────────────────

    def _pause_media(self):
        try:
            paused = self.backend.pause_playing() or []
        except Exception as e:
            logger.warning(f"Couldn't pause media: {e}")
            return
        # A second pause before the resume (a recording that failed to start,
        # then another) finds nothing playing; keep what the first one stopped.
        self._paused_apps = list(dict.fromkeys(self._paused_apps + list(paused)))
        if paused:
            logger.info(f"Paused media while recording: {', '.join(paused)}")

    def _resume_media(self):
        apps, self._paused_apps = self._paused_apps, []
        if not apps:
            return
        try:
            resumed = self.backend.resume_playing(apps) or []
            if resumed:
                logger.info(f"Resumed media after recording: {', '.join(resumed)}")
        except Exception as e:
            logger.warning(f"Couldn't resume media: {e}")

    # ── Volume ───────────────────────────────────────────────────────────────

    # Lower to `level_percent` only when the computer is louder than that.
    def _lower_volume(self, level_percent):
        try:
            target = max(0.0, min(1.0, float(level_percent) / 100.0))
            current = self.backend.get_master_volume()
        except Exception as e:
            logger.warning(f"Couldn't read the volume: {e}")
            return
        if current is None or current <= target + 0.005:
            return
        try:
            self.backend.set_master_volume(target)
        except Exception as e:
            logger.warning(f"Couldn't lower the volume: {e}")
            return
        logger.info(f"Lowered the volume from {current:.0%} to {target:.0%} while you talk")
        # A second recording that starts before this one restored keeps the
        # original volume to come back to, not the lowered one.
        if self._restore_to is None:
            self._restore_to = current
        self._lowered_to = target

    # Put the volume back, unless the user changed it while recording.
    def _restore_volume(self):
        if self._restore_to is None:
            return
        original, lowered = self._restore_to, self._lowered_to
        self._restore_to = self._lowered_to = None
        try:
            current = self.backend.get_master_volume()
            if current is not None and abs(current - lowered) < 0.02:
                self.backend.set_master_volume(original)
        except Exception as e:
            logger.warning(f"Couldn't restore the volume: {e}")
