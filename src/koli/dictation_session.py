# dictation_session.py
# What the dictation bubble holds while you speak: the settled words, the
# words still being recognised, where your caret or selection is, and whether
# you have opened the bubble to fix something. Engines add words; the bubble
# page edits it; at the end its text is what gets written into the app.
# Pure and thread-safe: engines call it from audio and worker threads, the
# bubble page from pywebview's threads (docs/design/dictation-bubble.md).

import re
import threading
from typing import Optional

SENTENCE_END = ('.', '!', '?', '…', '։', '؟', '。')
_FIRST_PERSON = {'I', "I'm", "I've", "I'll", "I'd"}
_WORD = re.compile(r"^(\S+)")


# A newly recognised piece as it should be inserted between `before` and
# `after`: spaced from its neighbours, capitalised at a sentence start, and
# lowercased mid-sentence when Whisper capitalised a plain word only because
# it started a new phrase. Inside the text, Whisper's closing period goes,
# and a word replacing a selection (`replaced`) takes that word's case.
def join_piece(before: str, piece: str, after: str, replaced: str = '') -> str:
    piece = (piece or '').strip()
    if after.strip() and piece.endswith('.') and not piece.endswith('..'):
        piece = piece[:-1].rstrip()
    if not piece:
        return ''
    stripped = before.rstrip(' \t')
    sentence_start = not before.strip() or stripped.endswith('\n') or stripped.endswith(SENTENCE_END)
    first = next((c for c in replaced if c.isalpha()), '')
    if first and not sentence_start:
        piece = (piece[0].upper() if first.isupper() else piece[0].lower()) + piece[1:]
    elif sentence_start:
        piece = piece[0].upper() + piece[1:]
    else:
        piece = _lower_mid_sentence(piece)
    if before and not before[-1].isspace():
        piece = ' ' + piece
    if after and not after[0].isspace() and after[0] not in '.,!?;:)]}…':
        piece = piece + ' '
    return piece


# "Store" after "I went to the" becomes "store"; "I", "NASA" and "McDonald"
# stay as they are.
def _lower_mid_sentence(piece: str) -> str:
    match = _WORD.match(piece)
    word = match.group(1) if match else ''
    letters = [c for c in word if c.isalpha()]
    if (len(letters) > 1 and word.rstrip('.,!?;:') not in _FIRST_PERSON
            and letters[0].isupper() and all(c.islower() for c in letters[1:])):
        return piece[0].lower() + piece[1:]
    return piece


class DictationSession:
    def __init__(self, engine: str, session_id: int, target_window: Optional[dict]):
        self.engine = engine
        self.session_id = session_id
        self.target_window = target_window   # the app window the text will be written into
        self.opened = False      # you clicked into the bubble: the text waits there until you finish
        self.edited = False      # you changed the text by hand
        self.listening = True    # the microphone is feeding this session
        self._settled = ''
        self._interim = ''
        self._original = ''      # the engine's own words, joined, for History and Training
        self._start = self._end = 0
        self._lock = threading.Lock()

    # ── What the engines call ────────────────────────────────────────────────

    def set_interim(self, text: str) -> None:
        with self._lock:
            self._interim = (text or '').strip()

    # A settled piece goes where the caret is, replacing a selection; before
    # you open the bubble the caret simply follows the end of the text.
    def add_final(self, text: str) -> None:
        with self._lock:
            self._interim = ''
            self._insert(text)

    def commit_interim(self) -> None:
        with self._lock:
            pending, self._interim = self._interim, ''
            if pending:
                self._insert(pending)

    def _insert(self, text: str) -> None:
        self._original = self._original + join_piece(self._original, text, '')
        if not self.opened:
            self._start = self._end = len(self._settled)
        before, after = self._settled[:self._start], self._settled[self._end:]
        piece = join_piece(before, text, after, replaced=self._settled[self._start:self._end])
        if not piece:
            return
        self._settled = before + piece + after
        self._start = self._end = len(before) + len(piece.rstrip(' '))

    # ── What the bubble page calls ───────────────────────────────────────────

    # You clicked into the bubble. The local engine is a preview only, so its
    # bubble never opens.
    def open(self) -> bool:
        with self._lock:
            if self.engine == 'local':
                return False
            self.opened = True
            return True

    # The pane's text and caret, in code points (the page converts from its
    # UTF-16 offsets), after you typed, selected or clicked.
    def edit(self, text: str, start: int, end: int) -> None:
        with self._lock:
            text = text or ''
            if text != self._settled:
                self.edited = True
                self._settled = text
            start = max(0, min(int(start), len(text)))
            end = max(start, min(int(end), len(text)))
            self._start, self._end = start, end

    def set_listening(self, on: bool) -> None:
        with self._lock:
            self.listening = bool(on)

    # ── Reading it ───────────────────────────────────────────────────────────

    def text(self) -> str:
        with self._lock:
            return self._settled

    def original(self) -> str:
        with self._lock:
            return self._original

    def words(self) -> int:
        with self._lock:
            return len(self._settled.split())

    def snapshot(self) -> dict:
        with self._lock:
            return {'id': self.session_id, 'engine': self.engine, 'settled': self._settled,
                    'interim': self._interim, 'start': self._start, 'end': self._end,
                    'opened': self.opened, 'listening': self.listening}
