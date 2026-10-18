# learning/clips.py
# The clip store: one record per kept dictation in learning\clips.jsonl, and
# its audio as learning\clips\YYYY-MM\<id>.flac. New records are appended, so
# keeping a clip never rewrites the index; deleting rewrites it once per
# batch. Audio can go while its record stays: what happened to a dictation is
# small and is what later phases learn from. Design: docs/design/self-learning.md.

import datetime
import hashlib
import json
import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Callable, Iterable, Optional

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
INDEX = 'clips.jsonl'
AUDIO_DIR = 'clips'
HOLDOUT_SHARE = 30      # percent of clips held back for honest scores, never learned from

# Whisper's numbers kept per piece: enough to replay every guard later.
PIECE_KEYS = ('text', 'start', 'end', 'avg_logprob', 'no_speech_prob', 'compression_ratio')
INFO_KEYS = ('language', 'language_probability', 'language_raw_probability', 'audio_seconds', 'model')


def default_folder() -> Path:
    from ..utils import get_user_app_data_path
    return Path(get_user_app_data_path()) / 'learning'


def new_id() -> str:
    return datetime.datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6]


# Train or holdout, fixed by the id so a clip never changes sides.
def split_for(clip_id: str) -> str:
    bucket = int(hashlib.sha1(clip_id.encode('utf-8')).hexdigest()[:4], 16) % 100
    return 'holdout' if bucket < HOLDOUT_SHARE else 'train'


# Whisper's info for a dictation, without what the record doesn't need.
def slim_info(info: Optional[dict]) -> dict:
    info = info or {}
    out = {k: info[k] for k in INFO_KEYS if k in info}
    pieces = info.get('segments')
    if pieces:
        out['segments'] = [{k: p[k] for k in PIECE_KEYS if k in p} for p in pieces]
    return out


# ── Audio files ──────────────────────────────────────────────────────────────
# FLAC through PyAV, which faster-whisper already installs: lossless at about
# half of WAV. WAV is the fallback when the encoder isn't there.

def _pcm16(audio: np.ndarray) -> np.ndarray:
    return (np.clip(np.asarray(audio, dtype=np.float32).reshape(-1), -1.0, 1.0) * 32767).astype(np.int16)


def write_audio(audio: np.ndarray, path: Path, rate: int = SAMPLE_RATE) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = _pcm16(audio)
    try:
        import av
        tmp = path.with_suffix('.tmp')
        with av.open(str(tmp), 'w', format='flac') as container:
            stream = container.add_stream('flac', rate=rate, layout='mono')
            step = 4096
            for at in range(0, len(pcm), step):
                frame = av.AudioFrame.from_ndarray(pcm[at:at + step].reshape(1, -1), format='s16', layout='mono')
                frame.sample_rate = rate
                for packet in stream.encode(frame):
                    container.mux(packet)
            for packet in stream.encode(None):
                container.mux(packet)
        os.replace(tmp, path)
        return path
    except Exception as e:
        logger.debug(f"FLAC encoder not available ({e}); keeping WAV")
    import wave
    path = path.with_suffix('.wav')
    with wave.open(str(path), 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(pcm.tobytes())
    return path


# The clip as WAV bytes, for the app window's player: WebView2 refuses FLAC
# in an <audio> data URL ("no supported source"), and every engine plays WAV.
def wav_bytes(path: Path) -> bytes:
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(_pcm16(read_audio(path)).tobytes())
    return buf.getvalue()


# The clip back as float32 at 16 kHz, for the night re-listen and replay.
# Read with PyAV directly: faster-whisper's decode_audio passes an option
# PyAV 19 no longer takes. Clips are written mono at 16 kHz, so no resampling.
def read_audio(path: Path) -> np.ndarray:
    path = Path(path)
    if path.suffix == '.wav':
        import wave
        with wave.open(str(path), 'rb') as f:
            pcm = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
        return pcm.astype(np.float32) / 32767
    import av
    chunks = []
    with av.open(str(path)) as container:
        for frame in container.decode(audio=0):
            chunks.append(frame.to_ndarray().reshape(-1))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    pcm = np.concatenate(chunks)
    return pcm.astype(np.float32) / 32767 if pcm.dtype == np.int16 else pcm.astype(np.float32)


# ── The store ────────────────────────────────────────────────────────────────

class ClipStore:
    def __init__(self, folder: Optional[Path] = None, clock: Callable[[], datetime.datetime] = datetime.datetime.now):
        self.folder = Path(folder) if folder is not None else default_folder()
        self.index_path = self.folder / INDEX
        self._clock = clock
        self._lock = threading.RLock()
        self._records: Optional[list] = None

    # ── Index ───────────────────────────────────────────────────────────────

    def _load(self) -> list:
        if self._records is not None:
            return self._records
        records = []
        if self.index_path.exists():
            with open(self.index_path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        pass   # one broken line never loses the rest
        self._records = records
        return records

    def _rewrite(self, records: list):
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.index_path.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
        os.replace(tmp, self.index_path)
        self._records = records

    def records(self) -> list:
        with self._lock:
            return [dict(r) for r in self._load()]

    def get(self, clip_id: str) -> Optional[dict]:
        with self._lock:
            return next((dict(r) for r in self._load() if r.get('id') == clip_id), None)

    # ── Adding ──────────────────────────────────────────────────────────────

    # Saves the audio (if any) and appends the record. Returns the record.
    def add(self, record: dict, audio: Optional[np.ndarray] = None) -> dict:
        record = dict(record)
        record.setdefault('id', new_id())
        record.setdefault('ts', self._clock().isoformat(timespec='seconds'))
        record.setdefault('split', split_for(record['id']))
        record['audio'] = None
        record['bytes'] = 0
        if audio is not None and len(audio):
            month = record['ts'][:7]
            path = write_audio(audio, self.folder / AUDIO_DIR / month / f"{record['id']}.flac")
            record['audio'] = path.relative_to(self.folder).as_posix()
            record['bytes'] = path.stat().st_size
        with self._lock:
            records = self._load()
            self.folder.mkdir(parents=True, exist_ok=True)
            with open(self.index_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(record, ensure_ascii=False) + '\n')
            records.append(record)
        return dict(record)

    def audio_path(self, record: dict) -> Optional[Path]:
        return self.folder / record['audio'] if record.get('audio') else None

    # ── Size ────────────────────────────────────────────────────────────────

    def used_bytes(self) -> int:
        with self._lock:
            return sum(int(r.get('bytes') or 0) for r in self._load() if r.get('audio'))

    # ── Deleting ────────────────────────────────────────────────────────────

    def _remove_file(self, record: dict):
        path = self.audio_path(record)
        if path is not None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                logger.debug(f"Couldn't delete {path.name}: {e}")

    # Deletes the audio of these clips; their records stay. Returns how many.
    def drop_audio(self, ids: Iterable[str]) -> int:
        wanted = set(ids)
        if not wanted:
            return 0
        with self._lock:
            records = self._load()
            done = 0
            for r in records:
                if r.get('id') in wanted and r.get('audio'):
                    self._remove_file(r)
                    r['audio'], r['bytes'] = None, 0
                    done += 1
            if done:
                self._rewrite(records)
            return done

    # Deletes clips completely, audio and record. Returns how many.
    def delete(self, ids: Iterable[str]) -> int:
        wanted = set(ids)
        if not wanted:
            return 0
        with self._lock:
            records = self._load()
            keep = []
            for r in records:
                if r.get('id') in wanted:
                    self._remove_file(r)
                else:
                    keep.append(r)
            removed = len(records) - len(keep)
            if removed:
                self._rewrite(keep)
            self._prune_empty_dirs()
            return removed

    # Every clip kept at or after `since` (a datetime). Returns how many.
    def delete_since(self, since: datetime.datetime) -> int:
        stamp = since.isoformat(timespec='seconds')
        return self.delete(r['id'] for r in self.records() if r.get('ts', '') >= stamp)

    # Everything: the folder goes, the store starts empty.
    def delete_all(self) -> int:
        import shutil
        with self._lock:
            count = len(self._load())
            if self.folder.exists():
                shutil.rmtree(self.folder, ignore_errors=True)
            self._records = []
            return count

    def _prune_empty_dirs(self):
        root = self.folder / AUDIO_DIR
        if not root.is_dir():
            return
        for month in root.iterdir():
            try:
                if month.is_dir() and not any(month.iterdir()):
                    month.rmdir()
            except OSError:
                pass
