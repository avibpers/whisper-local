# practice/intake.py
# Who you are, for choosing what to read: the intake answers (languages,
# native language or accent, line of work, words Koli gets wrong) and the
# terms worth practising that Koli already knows about you: frequent unusual
# words in your dictation history, your corrections and replacements, and
# words that failed in earlier rounds. Only these terms are ever sent to an AI
# model, never whole dictations.

import re
from collections import Counter

from .findings import COMMON, clean

DOMAINS = [['software', 'Software development'], ['business', 'Business and email'], ['medicine', 'Medicine'],
           ['law', 'Law'], ['studies', 'Studies'], ['creative', 'Creative work'], ['other', 'Other']]

MAX_TERMS = 40
HISTORY_LIMIT = 2000
# A word: letters (any script), digits, and the joiners inside names (C++,
# node.js, ה-API).
WORD = re.compile(r"[\w][\w+#.'\-]*[\w+#]|\w")


def normalize_intake(raw: dict, default_languages: list) -> dict:
    raw = raw or {}
    languages = [str(l) for l in (raw.get('languages') or default_languages or ['en']) if l and l != 'auto']
    work = raw.get('work') if raw.get('work') in {d[0] for d in DOMAINS} else 'other'
    words = [str(w).strip() for w in (raw.get('words') or []) if str(w).strip()]
    return {'languages': list(dict.fromkeys(languages)) or ['en'], 'accent': str(raw.get('accent') or '').strip(),
            'work': work, 'work_other': str(raw.get('work_other') or '').strip(), 'words': words[:30]}


# A term worth practising: not common, at least 4 letters, or a name/jargon
# (mixed case, digits, symbols).
def _interesting(word: str, language: str) -> bool:
    w = clean(word)
    if len(w) < 3:
        return False
    lower = w.lower()
    if lower in COMMON.get(language, set()) or lower in COMMON['en']:
        return False
    jargon = any(c.isupper() for c in w[1:]) or any(c.isdigit() for c in w) or any(c in '+#.-' for c in w)
    return jargon or len(w) >= 5


# The terms from what Koli knows: your own words first, then corrections and
# replacements, then failed words from earlier rounds, then history by
# frequency. [{'term', 'from'}], at most MAX_TERMS.
def personal_terms(intake: dict, history: list, corrections: dict, replacements: list, learned_items: list) -> list:
    out, seen = [], set()

    def add(term, source):
        term = clean(str(term or '').strip())
        if term and term.lower() not in seen and len(out) < MAX_TERMS:
            seen.add(term.lower())
            out.append({'term': term, 'from': source})

    for w in intake.get('words') or []:
        add(w, 'you')
    for canonical in (corrections or {}):
        add(canonical, 'corrections')
    for item in replacements or []:
        if isinstance(item, dict) and item.get('to') and not item.get('regex'):
            add(item['to'], 'replacements')
    for item in learned_items or []:
        if item.get('kind') in ('favored', 'correction'):
            add(item.get('term'), 'earlier practice')
    counts = Counter()
    for entry in (history or [])[:HISTORY_LIMIT]:
        language = entry.get('language') or ''
        for word in WORD.findall(entry.get('text') or ''):
            if _interesting(word, language):
                counts[clean(word)] += 1
    for word, n in counts.most_common():
        if n < 2:
            break
        add(word, 'history')
    return out
