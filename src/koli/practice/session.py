# practice/session.py
# One round of practice: the readings (sentences in two batches, then one
# paragraph), each one's recording and results, and where the round stands.
# Each reading is kept in %APPDATA%\koli\practice\<round>\ (NN.wav and
# session.json): text, what Whisper wrote with and without your vocabulary,
# and the word-by-word comparison. That is the material a fine-tune of
# Whisper on your voice would need later.

import json
import logging
import time
import uuid
import wave
from pathlib import Path

import numpy as np

from . import align

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
TRIM_FRAME = 320            # 20 ms
TRIM_KEEP_S = 0.35          # quiet kept after the last sound


# The recording without the quiet after you stopped reading. The pause that
# ends a reading (up to 2.5 s) is silence, and Whisper fills long silence
# with invented words ("תודה רבה, תודה רבה…").
def trim_silence(audio: np.ndarray) -> np.ndarray:
    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    frames = len(audio) // TRIM_FRAME
    if frames < 10:
        return audio
    rms = np.sqrt(np.mean(audio[:frames * TRIM_FRAME].reshape(frames, TRIM_FRAME) ** 2, axis=1))
    loud = np.nonzero(rms > max(0.006, float(np.percentile(rms, 95)) * 0.12))[0]
    if not len(loud):
        return audio
    end = min(len(audio), (int(loud[-1]) + 1) * TRIM_FRAME + int(TRIM_KEEP_S * SAMPLE_RATE))
    return audio[:end]

# Where a round stands: reading the first batch, the second batch, the
# paragraph, then the results.
FIRST, SECOND, PARAGRAPH, RESULTS = 'first', 'second', 'paragraph', 'results'


class Round:
    def __init__(self, folder: Path, intake: dict, terms: list, mic: str, model: str):
        self.id = time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:4]
        self.folder = Path(folder) / self.id
        self.intake = intake
        self.terms = terms
        self.mic = mic
        self.model = model          # "provider/model" writing and analysing, or '' for built-in only
        self.planned = 0            # sentences the whole round will have (both batches)
        self.stage = FIRST
        self.items = []             # {'index', 'kind', 'language', 'text', 'targets', 'source', 'result', 'skipped'}
        self.notes = []             # what the user should know (a model that failed, a missing pack)
        self.findings = []
        self.model_notes = {}
        self.started = time.strftime('%Y-%m-%dT%H:%M:%S')

    # ── Readings ────────────────────────────────────────────────────────────

    def add(self, texts: list, kind: str = 'sentence'):
        for t in texts:
            self.items.append({'index': len(self.items), 'kind': kind, 'language': t['language'], 'text': t['text'],
                               'targets': t.get('targets') or [], 'source': t.get('source', ''),
                               'result': None, 'skipped': False, 'stage': self.stage})

    def item(self, index: int) -> dict:
        if not 0 <= int(index) < len(self.items):
            raise ValueError('There is no such reading')
        return self.items[int(index)]

    def stage_done(self) -> bool:
        return all(i['result'] or i['skipped'] or i.get('flagged') for i in self.items if i['stage'] == self.stage)

    def already_read(self) -> set:
        return {i['text'] for i in self.items}

    # Readings with results, leaving out texts you flagged as wrong: a typo in
    # the text must not teach Koli a wrong word.
    def read(self) -> list:
        return [i for i in self.items if i['result'] and not i.get('flagged')]

    # Text words that failed in some reading so far, the most recent first:
    # what the next batch and the paragraph aim at.
    def failed_words(self) -> list:
        from .findings import clean, is_common
        words = []
        for item in reversed(self.read()):
            for w in item['result']['comparison']['words']:
                if w['status'] != align.RIGHT and not is_common(w['norm'], item['language']):
                    words.append(clean(w['text']))
        return list(dict.fromkeys(words))[:12]

    # ── Recording and results ───────────────────────────────────────────────

    def save_audio(self, index: int, audio: np.ndarray) -> str:
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"{int(index):02d}.wav"
        pcm = (np.clip(np.asarray(audio, dtype=np.float32).reshape(-1), -1, 1) * 32767).astype('<i2')
        with wave.open(str(path), 'wb') as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(SAMPLE_RATE)
            f.writeframes(pcm.tobytes())
        return path.name

    # A reading's results from Whisper's two passes ({'text', 'words'} or None).
    def set_result(self, index: int, dictated: dict, bare: dict, audio_file: str, seconds: float):
        item = self.item(index)
        dictated = dictated or {'text': '', 'words': None}
        bare = bare or {'text': '', 'words': None}
        item['result'] = {
            'heard': dictated.get('text') or '', 'heard_bare': bare.get('text') or '',
            'heard_language': dictated.get('language'), 'model': dictated.get('model'),
            'comparison': align.compare(item['text'], dictated.get('text') or '', dictated.get('words')),
            'comparison_bare': align.compare(item['text'], bare.get('text') or '', bare.get('words')),
            'audio': audio_file, 'seconds': round(seconds, 1),
            'confidence': dictated.get('words') is not None,
        }
        item['skipped'] = False
        self.save()
        return item

    # For the findings and the model: each reading with its text and results.
    def readings(self) -> list:
        return [{'kind': i['kind'], 'language': i['language'], 'text': i['text'],
                 'heard': i['result']['heard'], 'heard_bare': i['result']['heard_bare'],
                 'comparison': i['result']['comparison']} for i in self.read()]

    # Words whose result differs between the sentences and the paragraph.
    def context_effects(self) -> list:
        sentences, paragraph = {}, {}
        for item in self.read():
            target = paragraph if item['kind'] == 'paragraph' else sentences
            for norm, status in align.word_status(item['result']['comparison']).items():
                if align.RIGHT != status or norm not in target:
                    target[norm] = status
        shown = {}
        for item in self.read():
            for w in item['result']['comparison']['words']:
                shown.setdefault(w['norm'], w['text'])
        good = (align.RIGHT, align.UNSURE)
        out = []
        for norm, alone in sentences.items():
            in_text = paragraph.get(norm)
            if in_text is None or (alone in good) == (in_text in good):
                continue
            out.append({'word': shown.get(norm, norm), 'alone': alone, 'in_context': in_text,
                        'effect': 'fixed' if in_text in good else 'changed'})
        return out

    # Share of the text's words heard right, per language, in the dictated pass.
    def accuracy(self) -> dict:
        totals = {}
        for item in self.read():
            words = item['result']['comparison']['words']
            right = sum(1 for w in words if w['status'] in (align.RIGHT, align.UNSURE))
            t = totals.setdefault(item['language'], [0, 0])
            t[0] += right
            t[1] += len(words)
        return {lang: round(r / n, 3) for lang, (r, n) in totals.items() if n}

    # ── Views ───────────────────────────────────────────────────────────────

    def view(self) -> dict:
        return {'id': self.id, 'stage': self.stage, 'model': self.model, 'mic': self.mic, 'notes': self.notes,
                'planned': max(self.planned, sum(1 for i in self.items if i['kind'] == 'sentence')),
                'items': [{k: v for k, v in i.items()} for i in self.items],
                'accuracy': self.accuracy()}

    def save(self):
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            data = {'id': self.id, 'started': self.started, 'mic': self.mic, 'model': self.model,
                    'intake': self.intake, 'items': self.items, 'findings': self.findings, 'notes': self.model_notes}
            (self.folder / 'session.json').write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')
        except OSError as e:
            logger.warning(f"Couldn't save the practice round: {e}")
