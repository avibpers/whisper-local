# practice/align.py
# Lines up the text you read against what Whisper wrote, word by word. Both
# sides are normalised the way spoken commands are (lowercase, punctuation
# dropped, Hebrew kept as written), then a sequence diff says, for each word
# of the text: right, unsure (right, but Whisper wasn't confident), wrong
# (another word in its place), or missing. Added words are listed apart.

import difflib
from typing import Optional

from ..utils import normalize_spoken

UNSURE_BELOW = 0.45     # Whisper's word confidence under this: right, but unsure

RIGHT, UNSURE, WRONG, MISSING = 'right', 'unsure', 'wrong', 'missing'


HEBREW_PREFIXES = set('הבלמשוכ')


# [(shown, normalised)] for a text: the words as written, and what is compared.
# Words that are only punctuation drop out. A Hebrew prefix letter written
# apart from the term after it ("ה clipboard", as models write mixed text)
# joins it, the way Whisper writes it ("הקליפבורד", "ה-clipboard").
def tokens(text: str) -> list:
    out = []
    pending = None
    for word in (text or '').split():
        norm = normalize_spoken(word).replace(' ', '')
        if not norm:
            continue
        if pending is not None:
            out.append((f"{pending[0]}-{word}", pending[1] + norm))
            pending = None
            continue
        if len(norm) == 1 and norm in HEBREW_PREFIXES:
            pending = (word, norm)
            continue
        out.append((word, norm))
    if pending is not None:
        out.append(pending)
    return out


# Whisper's words ({'word', 'prob'}) as tokens with their confidence. A word
# Whisper wrote with a space inside counts as several.
def heard_tokens(words: Optional[list], text: str) -> list:
    if not words:
        return [(shown, norm, None) for shown, norm in tokens(text)]
    out = []
    for w in words:
        for shown, norm in tokens(w.get('word', '')):
            out.append((shown, norm, w.get('prob')))
    return out


# The comparison of one reading. `words` are Whisper's (or None when the
# engine gives text only). Returns:
#   {'words': [{'text', 'status', 'heard', 'prob'}], 'extra': [...],
#    'accuracy': right share of the text's words, 'spans': [...]}
# `spans` keep each wrong or missing stretch with its neighbours, for the
# findings: {'expected': [...], 'heard': [...], 'before', 'after', 'status'}.
def compare(reference: str, heard_text: str, words: Optional[list] = None) -> dict:
    ref = tokens(reference)
    hyp = heard_tokens(words, heard_text)
    matcher = difflib.SequenceMatcher(a=[r[1] for r in ref], b=[h[1] for h in hyp], autojunk=False)
    out, extra, spans = [], [], []
    for op, a0, a1, b0, b1 in matcher.get_opcodes():
        if op == 'equal':
            for i, j in zip(range(a0, a1), range(b0, b1)):
                prob = hyp[j][2]
                status = UNSURE if prob is not None and prob < UNSURE_BELOW else RIGHT
                out.append({'text': ref[i][0], 'norm': ref[i][1], 'status': status, 'heard': hyp[j][0], 'prob': prob})
            continue
        if op == 'insert':
            extra.extend(h[0] for h in hyp[b0:b1])
            continue
        status = WRONG if op == 'replace' else MISSING
        heard = [h[0] for h in hyp[b0:b1]]
        for n, i in enumerate(range(a0, a1)):
            # A replaced stretch shows the heard words on its first word.
            out.append({'text': ref[i][0], 'norm': ref[i][1], 'status': status,
                        'heard': ' '.join(heard) if n == 0 else '', 'prob': None})
        spans.append({'status': status,
                      'expected': [r[0] for r in ref[a0:a1]], 'expected_norm': [r[1] for r in ref[a0:a1]],
                      'heard': heard, 'heard_norm': [h[1] for h in hyp[b0:b1]],
                      'before': ref[a0 - 1][0] if a0 > 0 else '', 'after': ref[a1][0] if a1 < len(ref) else ''})
    right = sum(1 for w in out if w['status'] in (RIGHT, UNSURE))
    return {'words': out, 'extra': extra, 'spans': spans,
            'accuracy': round(right / len(ref), 3) if ref else 1.0}


# How each normalised word of the text came out, for comparing a sentence
# reading with the paragraph: {norm: status}. A word read twice keeps its
# worst result.
def word_status(comparison: dict) -> dict:
    rank = {RIGHT: 0, UNSURE: 1, MISSING: 2, WRONG: 3}
    out = {}
    for w in comparison.get('words', []):
        if rank[w['status']] >= rank.get(out.get(w['norm']), -1):
            out[w['norm']] = w['status']
    return out
