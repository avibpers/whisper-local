# services/vocabulary.py
# The Vocabulary screen: favored words (whisper.hotwords), corrections,
# replacements and snippets. All of them are settings, so every change goes
# through the settings service and applies to the next dictation. Also scans a
# folder of the user's documents for candidate words (non-interactive version
# of --import-vocab).

import re
from collections import Counter

from ..vocab_import import COMMON_WORDS, WORD_RE, _iter_files


def words(settings) -> list:
    return list(settings.get('whisper.hotwords') or [])


def add_words(settings, new_words: list) -> list:
    current = words(settings)
    lower = {w.lower() for w in current}
    for w in new_words:
        w = str(w).strip()
        if w and w.lower() not in lower:
            current.append(w)
            lower.add(w.lower())
    settings.set('whisper.hotwords', current)
    return current


def remove_word(settings, word: str) -> list:
    current = [w for w in words(settings) if w != word]
    settings.set('whisper.hotwords', current)
    return current


# Top candidate words in a folder, most frequent first, not already favored.
def scan_folder(settings, folder: str, top_n: int = 60) -> dict:
    from pathlib import Path
    root = Path(folder).expanduser()
    if not root.exists():
        raise ValueError(f"Folder not found: {root}")
    counter = Counter()
    files = 0
    for path in _iter_files(root.resolve()):
        files += 1
        try:
            text = path.read_text(encoding='utf-8', errors='replace')
        except OSError:
            continue
        for w in WORD_RE.findall(text):
            if w.lower() not in COMMON_WORDS:
                counter[w] += 1
    known = {w.lower() for w in words(settings)}
    ranked = [(w, c) for w, c in counter.most_common(top_n * 4) if w.lower() not in known]
    # Capitalised / mixed-case terms first: they are names and jargon.
    ranked.sort(key=lambda wc: (wc[0].islower(), -wc[1]))
    return {'files': files, 'candidates': [{'word': w, 'count': c} for w, c in ranked[:top_n]]}


def set_corrections(settings, corrections: dict) -> dict:
    clean = {}
    for canonical, variants in (corrections or {}).items():
        canonical = str(canonical).strip()
        if not canonical:
            continue
        if isinstance(variants, str):
            variants = [variants]
        vs = [str(v).strip() for v in (variants or []) if str(v).strip()]
        if vs:
            clean[canonical] = vs
    settings.set('postprocess.corrections', clean)
    return clean


def set_replacements(settings, items: list) -> list:
    clean = []
    for i, item in enumerate(items or []):
        frm = str(item.get('from', '')).strip()
        if not frm:
            continue
        entry = {'from': frm, 'to': str(item.get('to', ''))}
        if item.get('whole_word') is False:
            entry['whole_word'] = False
        if item.get('case_sensitive'):
            entry['case_sensitive'] = True
        if item.get('regex'):
            try:
                re.compile(frm)
            except re.error as e:
                raise ValueError(f"Row {i + 1}: “{frm}” is not a valid pattern ({e})")
            entry['regex'] = True
        clean.append(entry)
    settings.set('postprocess.replacements', clean)
    return clean


def set_snippets(settings, items: list) -> list:
    clean = []
    for item in items or []:
        trigger = ' '.join(str(item.get('trigger', '')).split())
        if not trigger or item.get('expansion') in (None, ''):
            continue
        entry = {'trigger': trigger, 'expansion': str(item['expansion'])}
        if item.get('mode') == 'alone':
            entry['mode'] = 'alone'
        clean.append(entry)
    settings.set('postprocess.snippets', clean)
    return clean
