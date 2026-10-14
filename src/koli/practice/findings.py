# practice/findings.py
# What a round of readings suggests for your vocabulary. The rules work with
# no AI: a wrong word becomes a correction (a single heard word only together
# with its neighbour, so a real word elsewhere is never replaced), and a
# wrong, missing or unsure word that isn't common becomes a favored word. With
# a model, it gets the same comparisons and answers with its own findings and
# a "how you speak" note per language; the two are merged.

import json
import logging
import re

from .. import llm
from .align import MISSING, UNSURE, WRONG

logger = logging.getLogger(__name__)

# Words too ordinary to favour or to correct. Short lists on purpose: the
# length floor catches most of the rest.
COMMON = {
    'en': set('''a an the and or but if so to of in on at by for with from as is are was were be been am it
        its this that these those there here i you he she we they me him her us them my your his our their not no
        yes do does did have has had will would can could should may might must just very also then than too
        what which who when where why how all any some more most other into over about after before again only
        out up down off one two three get got make made take like know think want need see say said go going
        come please thanks thank good well okay ok'''.split()),
    'he': set('''את של על זה זו זאת אני אתה את הוא היא אנחנו אתם הם הן לא כן גם עם מה אם כי יש אין רק עוד כל אבל
        או לי לך לו לה לנו להם שלי שלך שלו שלה שלנו היה הייתה היו אז כמו מאוד צריך צריכה רוצה רוצים אפשר
        טוב בסדר תודה בבקשה מי איפה למה איך כבר עכשיו היום מחר אחרי לפני בין אחד אחת שני שתי שלוש עד
        הזה הזאת האלה אותו אותה אותם פה שם כאן איזה איזו אילו כזה כזאת ככה שהוא שהיא שהם שאני רבה'''.split()),
}

MAX_CORRECTION_WORDS = 2      # a longer stretch of the text is too specific to ever match again
MAX_HEARD_WORDS = 3

EDGE_PUNCTUATION = re.compile(r'^[^\w]+|[^\w]+$')


HEBREW_PREFIXED_TERM = re.compile(r'^[הבלמשוכ]-(?=[A-Za-z])')


def clean(word: str) -> str:
    return EDGE_PUNCTUATION.sub('', word or '')


# A favored word or prompt term as it should be learned: an English term
# without the Hebrew prefix the comparison joined it to ("ה-clipboard" →
# "clipboard"). Corrections keep the prefix: it is part of what you write.
def term_of(word: str) -> str:
    return HEBREW_PREFIXED_TERM.sub('', clean(word))


# Corrections for the same text and language become one, with every way it
# was heard ("אתחיל session" heard "התחיל סשן" and "נתחיל סשן").
def _join_corrections(found) -> list:
    out, by_term = [], {}
    for f in found:
        if f['kind'] != 'correction':
            out.append(f)
            continue
        key = (f['language'], f['term'].lower())
        if key in by_term:
            first = by_term[key]
            first['heard'] += [h for h in f['heard'] if h not in first['heard']]
            first['count'] += f['count']
        else:
            by_term[key] = f
            out.append(f)
    return out


def is_common(norm: str, language: str) -> bool:
    return len(norm) <= 2 or norm in COMMON.get(language or '', set()) or norm in COMMON['en']


def _key(f: dict) -> tuple:
    heard = tuple(sorted(h.lower() for h in f.get('heard') or []))
    return (f['kind'], f.get('language') or '', f['term'].lower(), heard if f['kind'] == 'correction' else (),
            f.get('profile') or '')


# ── Rules ────────────────────────────────────────────────────────────────────

# `readings`: [{'language', 'comparison'}] (align.compare results). `domain`
# is the intake's line of work. Returns findings with a count and a reason,
# most frequent first.
def rule_findings(readings: list, domain: str = '') -> list:
    found = {}

    # `core`: the text's own words of a correction, without the neighbour it
    # carries, so the model's correction of the same word can replace it.
    def add(kind, language, term, heard=(), profile='', why='', core=''):
        f = {'kind': kind, 'language': language, 'term': term, 'heard': list(heard), 'profile': profile,
             'source': 'rules', 'core': core or term}
        entry = found.setdefault(_key(f), {**f, 'count': 0, 'why': why})
        entry['count'] += 1

    for reading in readings:
        language = reading.get('language') or ''
        comparison = reading.get('comparison') or {}
        failed = []
        for span in comparison.get('spans', []):
            expected = [clean(w) for w in span['expected']]
            for word, norm in zip(expected, span['expected_norm']):
                if word and not is_common(norm, language):
                    add('favored', language, term_of(word), why='failed')
                    failed.append(word)
            if span['status'] != WRONG or not span['heard_norm']:
                continue
            # Never a correction for a short or common word of the text: it
            # would fire on every real use of what was heard. Nor when what
            # was heard is ordinary words ("כמו איזה"), or the stretch is so
            # long it would never match again.
            if all(is_common(n, language) for n in span['expected_norm']):
                continue
            if all(is_common(n, language) for n in span['heard_norm']):
                continue
            if len(expected) > MAX_CORRECTION_WORDS or len(span['heard_norm']) > MAX_HEARD_WORDS:
                continue
            # A single heard word may be a real word ("bird" for "word"), so it
            # is corrected only next to its neighbour. Several heard words for
            # one ("cooper net is" for "Kubernetes") are specific enough alone.
            core = canonical = ' '.join(expected)
            heard = ' '.join(span['heard_norm'])
            if len(span['heard_norm']) == 1:
                neighbour = clean(span['before']) or clean(span['after'])
                if not neighbour:
                    continue
                from ..utils import normalize_spoken
                if clean(span['before']):
                    canonical, heard = f"{neighbour} {canonical}", f"{normalize_spoken(neighbour)} {heard}"
                else:
                    canonical, heard = f"{canonical} {neighbour}", f"{heard} {normalize_spoken(neighbour)}"
            add('correction', language, canonical, [heard], why='heard', core=core)
        for w in comparison.get('words', []):
            if w['status'] == UNSURE and not is_common(w['norm'], language):
                add('favored', language, term_of(w['text']), why='unsure')
                failed.append(clean(w['text']))
    # Profile prompt terms come from the model only: it knows which profile a
    # term belongs to, and the favored words already cover every profile.
    out = []
    for f in _join_corrections(found.values()):
        times = f" ({f['count']}×)" if f['count'] > 1 else ''
        if f['kind'] == 'correction':
            # Isolated (FSI…PDI): a Hebrew word inside the English sentence
            # would otherwise drag the quotes and "(2×)" around.
            heard = '” or “'.join(f"⁨{h}⁩" for h in f['heard'])
            f['reason'] = f"Whisper wrote “{heard}” for “⁨{f['term']}⁩”{times}"
        elif f['kind'] == 'prompt':
            f['reason'] = f"A term from your work that Whisper missed{times}"
        else:
            f['reason'] = (f"Whisper wasn't sure of it{times}" if f.pop('why') == 'unsure'
                           else f"Misheard or dropped{times}")
        f.pop('why', None)
        out.append(f)
    return sorted(out, key=lambda f: -f['count'])


# ── The model's analysis ─────────────────────────────────────────────────────

ANALYSIS_RULES = """You analyse a speech-to-text practice round for one person. They read texts aloud; \
Whisper transcribed each reading twice: "dictated" (with their vocabulary) and "bare" (no vocabulary). \
Every word of the text is marked right, unsure, wrong (with what was heard instead) or missing. \
"context" lists words whose result changed between a short sentence and the longer paragraph.

Find how Whisper mishears this person (accent, sounds, specific words) and suggest vocabulary that fixes \
their future dictation:
- "favored": a word Whisper should lean toward (names, jargon, rare or accent-prone words).
- "correction": when Whisper writes one of "heard", it should be "term". Only when the heard form is never \
something the person would mean; prefer a two-word phrase that makes it unambiguous.
- "prompt": a domain term for a profile's context prompt; "profile" is "dictation", "chat", "code", "notes", \
or "" for every profile.
Items apply per language, on every microphone: give each one's language code. Suggest nothing for words \
that were right in both readings, and never words that weren't read. One item per word: no lists in "term". \
Never a correction whose "heard" is ordinary words the person could really say (like "כמו איזה"), and never \
a one-word correction between two real words that sound alike for this speaker (מאלף and מעלף, when א and \
ע sound the same): that would break the other word every time. Describe such pairs in the notes instead; \
choosing between them by context is a later step's job.
The texts were written by another model and can contain a misspelled or made-up word. When what Whisper \
wrote is the correct word and the text's word is not a real word at all (text "recieve", Whisper \
"receive"), suggest nothing for it and list the text's word in "text_errors". A real word that sounds like \
another is not a text error.
Also write "notes": per language, 2 to 4 sentences on how this person's speech gets misheard (sounds, \
patterns), for a later model that will correct their transcripts.
Answer with one JSON object only: {"findings": [{"kind", "language", "term", "heard": [], "profile", \
"reason"}], "notes": {"<language>": "..."}, "text_errors": ["..."]}"""


# The compact view of one reading the model gets.
def reading_for_model(reading: dict) -> dict:
    comparison = reading.get('comparison') or {}
    issues = [{'word': w['text'], 'result': w['status'], **({'heard': w['heard']} if w.get('heard') else {})}
              for w in comparison.get('words', []) if w['status'] != 'right']
    return {'kind': reading.get('kind', 'sentence'), 'language': reading.get('language'),
            'text': reading.get('text'), 'dictated': reading.get('heard'), 'bare': reading.get('heard_bare'),
            'accuracy': comparison.get('accuracy'), 'issues': issues}


def model_messages(intake: dict, readings: list, context: list) -> list:
    payload = {'person': {k: intake.get(k) for k in ('languages', 'accent', 'work', 'work_other')},
               'readings': [reading_for_model(r) for r in readings], 'context': context}
    return [{'role': 'system', 'content': ANALYSIS_RULES},
            {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]


# The model's answer as (findings, notes, text_errors). Pieces it got wrong
# are dropped, never raised: a list in one "term" becomes one item per word,
# and a correction whose heard form is only ordinary words is left out.
def parse_model_answer(text: str) -> tuple:
    from ..utils import normalize_spoken
    data = llm.parse_json(text) or {}
    findings = []
    for f in data.get('findings') or []:
        if not isinstance(f, dict) or f.get('kind') not in ('favored', 'correction', 'prompt'):
            continue
        language = str(f.get('language') or '')
        raw_term = str(f.get('term') or '').strip()
        heard = [str(h).strip() for h in (f.get('heard') or []) if str(h).strip()]
        if f['kind'] == 'correction':
            heard = [h for h in heard if not all(is_common(n, language) for n in normalize_spoken(h).split())]
            if not raw_term or not heard:
                continue
            terms = [raw_term]
        else:
            terms = [term_of(t) for t in re.split(r'[,،;]', raw_term) if term_of(t)]
        for term in terms:
            findings.append({'kind': f['kind'], 'language': language, 'term': term,
                             'heard': heard if f['kind'] == 'correction' else [],
                             'profile': str(f.get('profile') or '') if f['kind'] == 'prompt' else '',
                             'reason': str(f.get('reason') or ''), 'source': 'ai', 'count': 1, 'core': term})
    notes = {str(k): str(v).strip() for k, v in (data.get('notes') or {}).items() if str(v).strip()} \
        if isinstance(data.get('notes'), dict) else {}
    errors = [clean(str(e)) for e in (data.get('text_errors') or []) if clean(str(e))] \
        if isinstance(data.get('text_errors'), list) else []
    return findings, notes, errors


# Findings about a word the text itself got wrong (a typo by the model that
# wrote it): nothing to learn there.
def drop_text_errors(findings: list, errors: list) -> list:
    from ..utils import normalize_spoken
    bad = {normalize_spoken(e) for e in errors or [] if normalize_spoken(e)}
    if not bad:
        return findings
    return [f for f in findings if not bad & set(normalize_spoken(f.get('core') or f['term']).split())]


# Rules and model together: the same item once, the model's reason kept. A
# rule's phrase correction ("תמונת כריכה") gives way to the model's correction
# of the same word ("כריכה"), which the model judged safe on its own.
def merge(rules: list, model: list) -> list:
    covered = {(f['language'], f['term'].lower()) for f in model if f['kind'] == 'correction'}
    merged = {_key(f): dict(f) for f in rules
              if not (f['kind'] == 'correction' and (f['language'], str(f.get('core', '')).lower()) in covered
                      and f['term'].lower() != str(f.get('core', '')).lower())}
    # The model may call "commit" English while it was read in a Hebrew
    # sentence; the reading's language is the one dictation will match on, so
    # the rule's entry is kept, with the model's reason.
    by_term = {k[:1] + k[2:]: k for k in merged}
    for f in model:
        key = _key(f)
        same = key if key in merged else by_term.get(key[:1] + key[2:])
        if same is not None:
            merged[same].update(reason=f['reason'] or merged[same]['reason'], source='ai')
        else:
            merged[key] = dict(f)
            by_term.setdefault(key[:1] + key[2:], key)
    return list(merged.values())


# Leaves out what you already have: your favored words and corrections, and
# learned items.
def drop_known(findings: list, hotwords: list, corrections: dict, learned_items: list) -> list:
    from ..learned_vocabulary import LearnedVocabulary
    words = {str(w).lower() for w in hotwords or []}
    fixes = {(str(k).lower(), str(v).lower()) for k, vs in (corrections or {}).items()
             for v in (vs if isinstance(vs, (list, tuple)) else [vs])}
    learned = {LearnedVocabulary.key(i) for i in learned_items or []}
    out = []
    for f in findings:
        if f['kind'] == 'favored' and f['term'].lower() in words:
            continue
        if f['kind'] == 'correction' and all((f['term'].lower(), h.lower()) in fixes for h in f['heard']):
            continue
        if LearnedVocabulary.key(f) in learned:
            continue
        out.append(f)
    return out
