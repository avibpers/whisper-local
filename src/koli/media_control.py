# media_control.py
# What happens to other sound while you dictate: pause the music that is
# playing, and/or lower the computer's volume to a chosen level (only if it is
# louder than that) and put it back when the recording ends. The OS calls live
# in platform/*/media.py; this module holds the rules.

import logging
import threading

logger = logging.getLogger(__name__)


def _in_background(fn):
    threading.Thread(target=fn, daemon=True, name='media-control').start()


class MediaControl:
    # `backend` provides pause_playing(), get_master_volume() (0..1 or None)
    # and set_master_volume(0..1). `run_async` runs the pause off the hotkey
    # thread so recording starts without waiting for the media players.
    def __init__(self, backend=None, run_async=_in_background):
        self._backend = backend
        self._run_async = run_async
        self._restore_to = None     # the volume before we lowered it
        self._lowered_to = None     # the volume we set, to notice a user change

    @property
    def backend(self):
        if self._backend is None:
            from .platform import media
            self._backend = media
        return self._backend

    # ── Recording start / end ────────────────────────────────────────────────

    def on_recording_start(self, audio_cfg: dict):
        audio_cfg = audio_cfg or {}
        if audio_cfg.get('pause_media_on_record'):
            self._run_async(self._pause_media)
        lower = audio_cfg.get('lower_volume') or {}
        if lower.get('enabled'):
            self._lower_volume(lower.get('level', 20))

    def on_recording_end(self):
        self._restore_volume()

    # ── Music ────────────────────────────────────────────────────────────────

    def _pause_media(self):
        try:
            paused = self.backend.pause_playing()
            if paused:
                logger.info(f"Paused media while recording: {', '.join(paused)}")
        except Exception as e:
            logger.warning(f"Couldn't pause media: {e}")

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
