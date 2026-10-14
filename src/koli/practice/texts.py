# practice/texts.py
# What you read in a round. With an AI model (Settings › AI, the Practice
# model), it writes sentences and a paragraph for you: your languages, your
# accent, your work and your terms, aimed at what failed so far. Without one,
# or when its answer is unusable, the built-in packs in texts.yaml are used,
# with your own words put into short template sentences. A round never stops
# for want of text.

import json
import logging
import random
import re
from pathlib import Path

from .. import llm

logger = logging.getLogger(__name__)

PACKS_FILE = Path(__file__).with_name('texts.yaml')
MAX_SENTENCE_CHARS = 220
HEBREW = re.compile(r'[֐-׿]')
LATIN = re.compile(r'[A-Za-z]')

_packs = None


def packs() -> dict:
    global _packs
    if _packs is None:
        from ruamel.yaml import YAML
        _packs = YAML(typ='safe').load(PACKS_FILE.read_text(encoding='utf-8')) or {}
    return _packs


def has_pack(language: str) -> bool:
    return language in packs()


def _pack(language: str, domain: str) -> dict:
    lang = packs().get(language) or {}
    return lang.get(domain) or lang.get('general') or {}


# ── How many of what ─────────────────────────────────────────────────────────

# The sentences a round asks for, as [{'language', 'count'}]: 8 with one
# language; with two or more, 4 in each of the first two plus 2 mixed.
def plan(languages: list) -> list:
    languages = list(languages or ['en'])
    if len(languages) == 1:
        return [{'language': languages[0], 'count': 8}]
    return [{'language': languages[0], 'count': 4}, {'language': languages[1], 'count': 4},
            {'language': 'mixed', 'count': 2}]


# Half of a plan (first or second batch), each language rounded so the two
# halves add up to the whole.
def half(full: list, second: bool) -> list:
    out = []
    for p in full:
        first = (p['count'] + 1) // 2
        count = p['count'] - first if second else first
        if count:
            out.append({'language': p['language'], 'count': count})
    return out


def script_language(word: str, languages: list) -> str:
    if HEBREW.search(word):
        return 'he'
    return 'en' if 'en' in languages or not languages else languages[0]


# ── Built-in ─────────────────────────────────────────────────────────────────

# Sentences from the packs for a batch, your words first (in templates), then
# pack sentences not read yet. Mixed sentences are Hebrew templates around a
# Latin term when there is one, else a software pack sentence in Hebrew.
def builtin_sentences(batch: list, intake: dict, focus: list, already: set, rng=random) -> list:
    languages = intake.get('languages') or ['en']
    domain = intake.get('work') or 'general'
    first = languages[0]
    words = list(dict.fromkeys([*focus, *(intake.get('words') or [])]))
    out = []
    for want in batch:
        lang = want['language']
        if lang == 'mixed':
            base = 'he' if 'he' in languages else first
            latin = [w for w in words if LATIN.search(w) and not HEBREW.search(w)]
            pool = [t.replace('{w}', w) for w in latin for t in (packs().get(base) or {}).get('templates', [])]
            pool += [s for s in _pack(base, 'software').get('sentences', []) if LATIN.search(s)]
            target_lang = base
        else:
            mine = [w for w in words if script_language(w, languages) == lang]
            templates = (packs().get(lang) or {}).get('templates', [])
            pool = [rng.choice(templates).replace('{w}', w) for w in mine] if templates else []
            pool += rng.sample(_pack(lang, domain).get('sentences', []), k=len(_pack(lang, domain).get('sentences', [])))
            target_lang = lang
        picked = 0
        for text in pool:
            if picked >= want['count']:
                break
            if text in already:
                continue
            already.add(text)
            targets = [w for w in words if w in text]
            out.append({'language': target_lang, 'text': text, 'targets': targets, 'source': 'builtin'})
            picked += 1
    return out


def builtin_paragraph(language: str, intake: dict) -> dict:
    text = _pack(language, intake.get('work') or 'general').get('paragraph') or ''
    if not text:
        return {}
    return {'language': language, 'text': ' '.join(text.split()), 'targets': [], 'source': 'builtin'}


# ── Written by the model ─────────────────────────────────────────────────────

WRITING_RULES = """You write reading practice for a speech-to-text app. The person reads each text aloud and \
the app compares what Whisper heard with the text, to learn how they speak.
Rules:
- Natural text this person would really dictate (messages, emails, notes, work), fitting their line of work.
- Every word must be a real, correctly spelled word of its language: the app treats the text as the truth, \
so a typo teaches it a wrong word. Check the spelling of every word.
- Numbers, dates and times in words; no digits, abbreviations, emoji or quotation marks; no niqqud in Hebrew.
- Use the target words naturally, and words with sounds that speakers with this person's accent tend to blur.
- A "mixed" sentence is in the person's first non-English language with technical or product terms written \
the way they type them (Latin letters), attached to a Hebrew prefix with a hyphen (ה-commit). Only such \
terms stay in English, never ordinary English words ("meant", "really"). Its "language" is that main language.
- Never repeat a text listed in "already_read"."""

SENTENCES_FORMAT = ('Write the sentences asked for in "write", 8 to 16 words each. Answer with one JSON object '
                    'only: {"sentences": [{"language": "en", "text": "...", "targets": ["..."]}]}')
PARAGRAPH_FORMAT = ('Write one paragraph of 60 to 90 words in the language given in "write", using as many of '
                    'the target words as fit naturally. Answer with one JSON object only: '
                    '{"paragraph": {"language": "he", "text": "...", "targets": ["..."]}}')


def _person(intake: dict) -> dict:
    return {k: intake.get(k) for k in ('languages', 'accent', 'work', 'work_other')}


def sentence_messages(intake: dict, terms: list, focus: list, batch: list, already: list) -> list:
    payload = {'person': _person(intake), 'terms': [t['term'] for t in terms], 'focus_words': focus,
               'already_read': already, 'write': batch}
    return [{'role': 'system', 'content': f"{WRITING_RULES}\n\n{SENTENCES_FORMAT}"},
            {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]


def paragraph_messages(intake: dict, targets: list, language: str, already: list) -> list:
    payload = {'person': _person(intake), 'target_words': targets, 'already_read': already,
               'write': {'language': language}}
    return [{'role': 'system', 'content': f"{WRITING_RULES}\n\n{PARAGRAPH_FORMAT}"},
            {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]


def _clean_text(text) -> str:
    text = ' '.join(str(text or '').split())
    return text.strip('"“”')


# The sentences in a model's answer that are usable: text present and not too
# long, a language you dictate in, not read already.
def parse_sentences(answer: str, languages: list, already: set) -> list:
    data = llm.parse_json(answer) or {}
    out = []
    for s in data.get('sentences') or []:
        if not isinstance(s, dict):
            continue
        text = _clean_text(s.get('text'))
        if not text or len(text) > MAX_SENTENCE_CHARS or text in already:
            continue
        language = str(s.get('language') or '')
        if language not in languages:
            language = 'he' if HEBREW.search(text) and 'he' in languages else languages[0]
        already.add(text)
        targets = [str(t) for t in (s.get('targets') or []) if str(t).strip()]
        out.append({'language': language, 'text': text, 'targets': targets, 'source': 'ai'})
    return out


def parse_paragraph(answer: str, language: str) -> dict:
    data = llm.parse_json(answer) or {}
    p = data.get('paragraph') if isinstance(data.get('paragraph'), dict) else data
    text = _clean_text((p or {}).get('text'))
    if len(text.split()) < 20:
        return {}
    return {'language': language, 'text': text,
            'targets': [str(t) for t in (p.get('targets') or []) if str(t).strip()], 'source': 'ai'}


# ── One call for each ────────────────────────────────────────────────────────

# A batch of sentences: the model's when it answers well, topped up from the
# packs. `use_model` False skips the model.
def sentences(batch: list, intake: dict, terms: list, focus: list, already: set, use_model: bool) -> tuple:
    written, error = [], ''
    if use_model:
        result = llm.complete('practice', sentence_messages(intake, terms, focus, batch, sorted(already)), json=True)
        if result.ok:
            written = parse_sentences(result.text, intake.get('languages') or ['en'], already)
            if not written:
                error = 'The AI model answered without usable sentences; using built-in ones.'
        else:
            error = result.error
    # Top up per language from the packs.
    short = []
    for want in batch:
        lang = want['language']
        have = sum(1 for s in written if (lang == 'mixed' and LATIN.search(s['text']) and HEBREW.search(s['text']))
                   or s['language'] == lang)
        if have < want['count']:
            short.append({'language': lang, 'count': want['count'] - have})
    if short:
        written += builtin_sentences(short, intake, focus, already)
    return written, error


def paragraph(intake: dict, targets: list, language: str, already: set, use_model: bool) -> tuple:
    error = ''
    if use_model:
        result = llm.complete('practice', paragraph_messages(intake, targets, language, sorted(already)), json=True)
        if result.ok:
            p = parse_paragraph(result.text, language)
            if p:
                return p, ''
            error = 'The AI model answered without a usable paragraph; using a built-in one.'
        else:
            error = result.error
    return builtin_paragraph(language, intake), error
