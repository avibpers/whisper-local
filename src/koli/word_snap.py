# word_snap.py
# Snaps near misses to your own words after Whisper: "Charge B" becomes
# "ChargeBee", "Kubernetis" becomes "Kubernetes". Hotwords only nudge Whisper
# (and online models only get them as a prompt), so a close-but-wrong spelling
# still gets through; this catches it. After Handy's custom-word matcher:
# spans of up to three words, edit distance over the longer length, a boost
# when they sound alike (Soundex, Latin letters only), a strict threshold,
# and never across punctuation or a line break. Pure: text and words in,
# text out.

import re
from typing import Iterable, Optional

# Accepted when the distance score is under this (0 = identical). Handy's
# default; one wrong letter in an eight-letter name passes, two don't.
THRESHOLD = 0.18
# A sound-alike match counts this much of its distance, for words this long
# and no further apart than this.
PHONETIC_WEIGHT = 0.3
PHONETIC_MIN_LENGTH = 5
PHONETIC_MAX_SCORE = 0.4
MAX_SPAN_WORDS = 3
MAX_KEY_LENGTH = 50
MIN_WORD_LENGTH = 4

# Ordinary words never replaced on their own: a custom "Deno" must not turn
# "demo" or "done" into it.
_PROTECTED = {
    'the', 'and', 'that', 'this', 'with', 'have', 'from', 'they', 'will', 'would', 'there', 'their',
    'what', 'about', 'which', 'when', 'make', 'like', 'time', 'just', 'know', 'take', 'people',
    'into', 'year', 'your', 'good', 'some', 'could', 'them', 'other', 'than', 'then', 'look',
    'only', 'come', 'over', 'think', 'also', 'back', 'after', 'work', 'first', 'well', 'even',
    'want', 'because', 'these', 'give', 'most', 'done', 'here', 'where', 'were', 'been', 'more',
    'note', 'notes', 'code', 'cold', 'call', 'mail', 'file', 'line', 'list', 'data', 'test',
    'demo', 'name', 'page', 'send', 'sent', 'said', 'does', 'real', 'read', 'ready',
}

_TOKEN = re.compile(r'\S+')
_EDGE = re.compile(r'^(\W*)(.*?)(\W*)$', re.S)


# Lower-case letters and digits only: "Charge-B" and "charge b" compare equal.
def _key(text: str) -> str:
    return ''.join(ch for ch in text.lower() if ch.isalnum())


# A name-like word worth snapping to: a capital or a digit somewhere, and
# long enough that one letter's difference means something.
def _snappable(word: str) -> bool:
    return len(_key(word)) >= MIN_WORD_LENGTH and any(ch.isupper() or ch.isdigit() for ch in word)


def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


_SOUNDEX_CODES = {**dict.fromkeys('bfpv', '1'), **dict.fromkeys('cgjkqsxz', '2'),
                  **dict.fromkeys('dt', '3'), 'l': '4', **dict.fromkeys('mn', '5'), 'r': '6'}


# American Soundex: the first letter and three digits for the consonants.
def _soundex(word: str) -> Optional[str]:
    if not word or not word.isascii() or not word.isalpha():
        return None
    word = word.lower()
    out, last = word[0].upper(), _SOUNDEX_CODES.get(word[0], '')
    for ch in word[1:]:
        code = _SOUNDEX_CODES.get(ch, '')
        if code and code != last:
            out += code
        if ch not in 'hw':
            last = code
    return (out + '000')[:4]


# How far `candidate` is from `target` (both keys); None when the lengths are
# too different to be the same word said badly.
def _score(candidate: str, target: str) -> Optional[float]:
    longest = max(len(candidate), len(target))
    if abs(len(candidate) - len(target)) > max(2.0, longest * 0.25):
        return None
    distance = _levenshtein(candidate, target)
    score = distance / longest
    # Sounding alike counts for words of five letters or more that are no
    # more than 40% different: four-letter names share Soundex codes with
    # too many others ("Kyle" and "Koli" are both K400).
    if longest >= PHONETIC_MIN_LENGTH and score <= PHONETIC_MAX_SCORE:
        sound = _soundex(candidate)
        if sound is not None and sound == _soundex(target):
            score *= PHONETIC_WEIGHT
    return score


# Keeps a shouted span shouted; otherwise your word is written as you wrote it.
def _cased(original: str, word: str) -> str:
    letters = [ch for ch in original if ch.isalpha()]
    if len(letters) > 1 and all(ch.isupper() for ch in letters):
        return word.upper()
    return word


def snap_to_words(text: str, words: Iterable[str], threshold: float = THRESHOLD) -> str:
    targets = [(w, _key(w)) for w in dict.fromkeys(str(w).strip() for w in (words or [])) if _snappable(w)]
    if not text or not targets:
        return text
    tokens = list(_TOKEN.finditer(text))
    out, cursor, i = [], 0, 0
    while i < len(tokens):
        best = None   # (score, span length, word)
        for n in range(min(MAX_SPAN_WORDS, len(tokens) - i), 0, -1):
            span = tokens[i:i + n]
            if not _joinable(text, span):
                continue
            candidate = _key(''.join(t.group() for t in span))
            if len(candidate) < 3 or len(candidate) > MAX_KEY_LENGTH:
                continue
            if n == 1 and candidate in _PROTECTED:
                continue
            for word, target in targets:
                score = _score(candidate, target)
                if score is not None and score < threshold and (best is None or score < best[0]):
                    best = (score, n, word)
        if best is None:
            i += 1
            continue
        _, n, word = best
        first, last = tokens[i], tokens[i + n - 1]
        prefix = _EDGE.match(first.group()).group(1)
        suffix = _EDGE.match(last.group()).group(3)
        original = text[first.start():last.end()]
        replacement = prefix + _cased(original, word) + suffix
        out.append(text[cursor:first.start()])
        out.append(replacement)
        cursor = last.end()
        i += n
    out.append(text[cursor:])
    return ''.join(out)


# Whether the tokens of a span may be read as one: only spaces between them,
# and no punctuation closing any but the last ("Charge B, che" stops at "B,").
def _joinable(text: str, span) -> bool:
    for left, right in zip(span, span[1:]):
        if _EDGE.match(left.group()).group(3):
            return False
        if text[left.end():right.start()].strip(' \t'):
            return False   # a line break or anything but spaces
    return True
