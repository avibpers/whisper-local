# Meeting Search by Meaning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Meetings search box finds meetings by meaning and by words together (all meetings or only the open one), and "Ask all meetings" lets the AI answer from every meeting with links that open the meeting at the right second.

**Architecture:** `meeting/pieces.py` cuts a meeting and its summary into pieces; `meeting/index.py` embeds them with a local Ollama model into `<id>.index.npz` and searches them in memory; `MeetingStore.search` becomes a weighted any-word search; `meeting/find.py` merges both rankings. `services/meeting_search.py` owns the index, a background indexer fed by the store's change callback, the model download and Ask. `meeting/ask.py` plans (search phrases / meetings to open), gathers material and answers with checked references. The Meetings screen shows merged results, an "Only this meeting" switch, a model card and an Ask panel.

**Tech Stack:** Python 3.13 stdlib, numpy, Ollama HTTP API (`/api/embed`, `/api/pull`), Koli `llm.complete`, pywebview + Preact/htm pages, unittest.

**Spec:** `docs/design/meeting-search.md`

## Global Constraints

- Settings: `meeting.search.meaning` (toggle, default `true`, label "Search by meaning"), `meeting.search.model` (text, default `qwen3-embedding:0.6b`, label "Meaning model (Ollama)"), section `meetings`, group `Search`. Ollama address from `ai.ollama.address` (default `http://localhost:11434`).
- Pieces: transcript windows `WINDOW_S = 75`, `OVERLAP_S = 20`; summary items shorter than `MIN_ITEM_CHARS = 15` dropped; summary fields skipped: `labels, generator, template, version, written, language, date, meeting_id, title, participants, roles, attendees, instructions`.
- Embedded text = `Meeting "<title>" on <YYYY-MM-DD>\n<piece text>`. Query text = `Instruct: Given a question about past meetings, retrieve the passages that answer it\nQuery: <question>`.
- Index file `<meetings>/<id>.index.npz`: arrays `vectors` (float16, unit length), `hashes` (str, sha1 of embedded text), `rows` (0-d str, JSON list of pieces), `model` (0-d str). Loaded with `allow_pickle=False`.
- Search: up to `PER_MEETING = 4` pieces per meeting for Ask material, `TOTAL = 30`; results show up to 3 places per meeting (10 with a meeting filter). Merge: reciprocal rank `1 / (60 + rank)` per list, rank from 1.
- Word search (measured in the lab, option C): whole words (regex `[\w״"׳'-]+`, lower-cased); a Hebrew word (first char in `\u05d0-\u05ea`) longer than 3 letters also counts without one leading `ו ה ב ל מ ש כ`, in query and meeting; query words in more than `COMMON_SHARE = 0.3` of meetings are ignored; a hit holds every remaining query word; score `Σ log(1 + N / df) · log(1 + times_said)`; no remaining word → no word hits.
- Meaning results wait at most `QUERY_WAIT_S = 2.0` for the model (a cold load measured 3.5 s, warm 0.02 s); otherwise words only with `meaning: "loading"` and the page asks again after 2 s. Every embed call sends `keep_alive: "30m"`.
- Ask: plan JSON `{"task", "search": [≤3], "open": [≤3]}`; material ≤ `MAX_MATERIAL = 60000` chars; an opened meeting that doesn't fit gives its 12 best pieces; answer JSON `{"reply", "refs": [{"id", "at"}]}`; history file `<user data>/meetings_ask.json`, last `HISTORY = 10` questions sent; model route feature `meetings`; usage feature `meeting_ask` via `usage.ledger.for_meeting(None, 'meeting_ask')`.
- The indexer never embeds while a meeting is running; it debounces a meeting's changes by 10 s.
- Every new module starts with a 2–4 line header comment; non-trivial functions get a one-line intent comment (project CLAUDE.md). No `alert()/confirm()/prompt()` in pages; reuse existing CSS classes (`mc-*`, `menu-item`, `card`, `chip`) before adding any.
- Tests: from the worktree root `PYTHONUTF8=1 S:/Koli/.venv/Scripts/python.exe -m unittest tests.test_meeting_search -v`; new tests in `tests/test_meeting_search.py` (same `sys.path.insert(0, str(ROOT / "src"))` header as `tests/test_meeting.py`). Full suite before the last commit: `... -m unittest discover -s tests`.

## Review Focus

1. **Ollama not running, model not downloaded, or the GPU full** when you type: word results still show at once, with one line saying why meaning results are missing. Test in Task 6 (`test_search_without_model_returns_words_and_reason`).
2. **Searching while a meeting is live**: the search answers; the live meeting is not indexed until it ends. Test in Task 6 (`test_indexer_waits_while_meeting_runs`).
3. **The AI cites a meeting or second it wasn't given** (made-up id, time past the end): the reference and its `[n]` marker are dropped, the rest of the answer stays. Test in Task 7 (`test_answer_drops_unknown_refs_and_markers`).
4. **A long meeting opened whole** (1,216 turns) together with two others: the material stays under 60,000 characters by using that meeting's best pieces. Test in Task 7 (`test_material_uses_pieces_when_meeting_too_long`).
5. **Changing the model in Settings**: every index with another model name is rebuilt, and a search never mixes vectors of two models. Test in Task 4 (`test_other_model_rebuilds`) and Task 6 (`test_model_change_reindexes`).

---

### Task 1: Embeddings through Ollama

**Files:**
- Modify: `src/koli/llm/transports/ollama.py` (add `embed`, `pull`)
- Modify: `src/koli/llm/__init__.py` (add `embed` to `__all__` and the module)
- Test: `tests/test_meeting_search.py` (`EmbedTests`)

**Interfaces:**
- Produces: `ollama.embed(base_url: str, model: str, texts: list, timeout: float = 120) -> list[list[float]]` (POST `/api/embed` `{"model", "input": texts}`, returns `embeddings`; `error` in the answer raises `ProviderError`); `ollama.pull(base_url: str, model: str, on_progress: Callable[[float, str], None], timeout: float = 3600) -> None` (POST `/api/pull` with `stream: true`, progress = `completed / total` per line, status text); `llm.embed(model: str, texts: list, batch: int = 64, timeout: float = 120) -> numpy.ndarray` (float32, rows unit length, Ollama address from `ai.ollama.address`; empty `texts` → shape `(0, 0)`). `ollama.embed` sends `keep_alive: "30m"`.

- [ ] **Step 1: Write the failing tests**

```python
class EmbedTests(unittest.TestCase):
    def test_embed_batches_and_normalizes(self):
        from koli import llm
        calls, payloads = [], []
        def fake(url, payload, headers=None, timeout=30):
            calls.append(payload['input']); payloads.append(payload)
            return {'embeddings': [[3.0, 4.0] for _ in payload['input']]}
        with mock.patch('koli.llm.transports.ollama.http.post_json', fake):
            out = llm.embed('qwen3-embedding:0.6b', ['a', 'b', 'c'], batch=2)
        self.assertEqual([len(c) for c in calls], [2, 1])
        self.assertEqual(payloads[0]['keep_alive'], '30m')      # stays loaded between questions
        np.testing.assert_allclose(out[0], [0.6, 0.8], rtol=1e-5)

    def test_embed_error_raises(self):
        from koli import llm
        from koli.llm.types import ProviderError
        with mock.patch('koli.llm.transports.ollama.http.post_json', lambda *a, **k: {'error': 'model not found'}):
            with self.assertRaises(ProviderError):
                llm.embed('missing', ['a'])

    def test_pull_reports_progress(self):
        from koli.llm.transports import ollama
        lines = [b'{"status":"pulling","total":200,"completed":100}\n', b'{"status":"success"}\n']
        class Resp(list):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        seen = []
        with mock.patch('koli.llm.transports.ollama.http.post_stream', lambda *a, **k: Resp(lines)):
            ollama.pull('http://x', 'm', lambda p, s: seen.append((p, s)))
        self.assertEqual(seen[0], (0.5, 'pulling'))
```

- [ ] **Step 2: Run** `PYTHONUTF8=1 S:/Koli/.venv/Scripts/python.exe -m unittest tests.test_meeting_search.EmbedTests -v` — expect FAIL (`embed` not defined).
- [ ] **Step 3: Implement** `ollama.embed`, `ollama.pull`, `llm.embed` per Interfaces.
- [ ] **Step 4: Run** the same command — expect PASS.
- [ ] **Step 5: Commit** `git add src/koli/llm tests/test_meeting_search.py` · `git commit -m "feat(llm): embeddings and model download through Ollama"`

---

### Task 2: Pieces of a meeting

**Files:**
- Create: `src/koli/meeting/pieces.py`
- Modify: `src/koli/meeting/store.py:113-128` (rename `_brief_text`'s walk into a public `plain_text(value) -> str` used by both)
- Test: `tests/test_meeting_search.py` (`PiecesTests`)

**Interfaces:**
- Produces: `pieces(meeting: dict, brief: dict | None) -> list[dict]`, each `{'kind': 'transcript'|'summary'|'screenshot', 'field': str, 'at': float | None, 'text': str}`; `label(meeting: dict) -> str` (`Meeting "<title>" on <YYYY-MM-DD>`); `embed_text(meeting: dict, piece: dict) -> str` (label + `\n` + text); `store.plain_text(value) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
class PiecesTests(unittest.TestCase):
    def meeting(self, n_turns=10, step=15.0):
        return {'id': 'm1', 'title': 'Pricing', 'started_at': '2026-11-16T17:51:57+02:00',
                'speakers': [{'id': 'you', 'name': 'You'}, {'id': 's1', 'name': 'Shahar'}],
                'turns': [{'start': i * step, 'speaker': 's1' if i % 2 else 'you', 'text': f'turn {i}'} for i in range(n_turns)],
                'screenshots': [{'at': 30.0, 'caption': 'The price table for enterprise'}]}

    def test_transcript_windows_overlap(self):
        from koli.meeting.pieces import pieces
        ps = [p for p in pieces(self.meeting(), None) if p['kind'] == 'transcript']
        self.assertEqual(ps[0]['at'], 0.0)
        self.assertIn('Shahar: turn 1', ps[0]['text'])
        self.assertEqual(ps[1]['at'], 60.0)          # 75 s window, next starts 20 s before its end (55 s → first turn at 60)
        self.assertIn('turn 4', ps[0]['text'])      # 60 s is inside the first window too

    def test_summary_items_plain_and_layout_skipped(self):
        from koli.meeting.pieces import pieces
        brief = {'tldr': 'We agreed the enterprise price', 'decisions': [{'id': 'D1', 'text': 'Enterprise costs 40 per seat', 'at': 120}],
                 'labels': {'tldr': 'סיכום'}, 'generator': 'codex via Koli', 'avi_vision': ['Koli answers every meeting question'],
                 'action_items': [{'task': 'ok'}]}
        ps = [p for p in pieces(self.meeting(), brief) if p['kind'] == 'summary']
        texts = [p['text'] for p in ps]
        self.assertIn('Enterprise costs 40 per seat', texts)
        self.assertEqual(next(p for p in ps if p['field'] == 'decisions')['at'], 120)
        self.assertIn('Koli answers every meeting question', texts)      # a template's own field
        self.assertFalse(any('סיכום' in t or 'codex' in t or '{' in t for t in texts))
        self.assertNotIn('ok', texts)                                     # under 15 characters

    def test_screenshot_caption_piece_and_label(self):
        from koli.meeting.pieces import embed_text, pieces
        m = self.meeting()
        shot = next(p for p in pieces(m, None) if p['kind'] == 'screenshot')
        self.assertEqual(shot['at'], 30.0)
        self.assertTrue(embed_text(m, shot).startswith('Meeting "Pricing" on 2026-11-16\n'))

    def test_empty_meeting_has_no_pieces(self):
        from koli.meeting.pieces import pieces
        self.assertEqual(pieces({'id': 'x', 'title': '', 'turns': [], 'speakers': []}, None), [])
```

- [ ] **Step 2: Run** `... -m unittest tests.test_meeting_search.PiecesTests -v` — expect FAIL (module missing).
- [ ] **Step 3: Implement** `pieces.py` per Interfaces and Global Constraints (windows: the next window starts at the first turn at or after `start + WINDOW_S - OVERLAP_S`, always moving at least one turn; summary: per top-level field, each list item or the scalar is one piece through `plain_text`; `at` = item `at`, else `start`, else `None`). Make `store._brief_text` call `plain_text`.
- [ ] **Step 4: Run** the PiecesTests and `tests.test_meeting` — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(meetings): cut meetings into pieces for search"` (pieces.py, store.py, the test file).

---

### Task 3: Word search that scores any word

**Files:**
- Modify: `src/koli/meeting/store.py:236-290` (`list` skips `CHAT_SUFFIX`; `search` rescored)
- Test: `tests/test_meeting_search.py` (`WordSearchTests`)

**Interfaces:**
- Produces: `MeetingStore.search(query: str, limit: int = 50, meeting_id: str | None = None) -> list[dict]`, each hit `{'id', 'title', 'started_at', 'score': float, 'where', 'snippet', 'at'}` best first (word rules in Global Constraints; document frequencies over all meetings, cached by file mtime); `MeetingStore.list()` never reads `*.chat.json`.

- [ ] **Step 1: Write the failing tests** (build meetings with `new_meeting` + turns as in `tests/test_meeting.py:StoreTests`)

```python
class WordSearchTests(unittest.TestCase):
    # setUp: ten meetings all saying "the plans for today"; "a" also "the budget for next year, budget again",
    # "b" "תקציב של השנה", "c" "budget" once, "d" "מס״ב נפל"
    def test_common_words_ignored_every_rare_word_needed(self):
        self.assertEqual([h['id'] for h in self.store.search('what about the budget')], ['a', 'c'])
        self.assertEqual(self.store.search('budget lunch'), [])    # "lunch" is in no meeting

    def test_ranked_by_times_said(self):
        self.assertEqual(self.store.search('budget')[0]['id'], 'a')

    def test_hebrew_prefix_both_ways(self):
        self.assertEqual(self.store.search('בתקציב')[0]['id'], 'b')  # the meeting says "תקציב"

    def test_whole_words_only(self):
        self.assertEqual(self.store.search('udge'), [])

    def test_abbreviation_with_gershayim(self):
        self.assertEqual(self.store.search('מס״ב')[0]['id'], 'd')

    def test_meeting_filter(self):
        self.assertEqual({h['id'] for h in self.store.search('budget', meeting_id='c')}, {'c'})

    def test_list_skips_chat_files(self):
        self.store.save_chat('a', [{'role': 'user', 'text': 'hi'}])
        with self.assertNoLogs('koli.meeting.store', level='WARNING'):
            self.assertEqual(len(self.store.list()), 3)
```

- [ ] **Step 2: Run** `... WordSearchTests -v` — expect FAIL.
- [ ] **Step 3: Implement** the word rules from Global Constraints; `where/snippet/at` as today but for the rarest query word; ties newest first.
- [ ] **Step 4: Run** WordSearchTests and `tests.test_meeting` — expect PASS (update any `StoreTests` search assertion that relied on "every word", keeping its intent).
- [ ] **Step 5: Commit** `git commit -m "feat(meetings): word search scores any word, rare words and Hebrew prefixes"`

---

### Task 4: The index

**Files:**
- Create: `src/koli/meeting/index.py`
- Modify: `src/koli/meeting/store.py` (`delete` removes `<id>.index.npz`; `INDEX_SUFFIX = '.index.npz'`)
- Test: `tests/test_meeting_search.py` (`IndexTests`)

**Interfaces:**
- Consumes: `pieces`, `embed_text` (Task 2).
- Produces: `class MeetingIndex(folder: Path, model: str, embed: Callable[[list[str]], np.ndarray])` with `update(meeting: dict, brief: dict | None) -> int` (pieces embedded now), `is_current(meeting_id: str, mtime: float) -> bool` (file exists, same model, newer than `mtime`; callers pass the newer of the meeting file's and the summary file's mtime), `remove(meeting_id: str)`, `search(query_vector: np.ndarray, meeting_id: str | None = None, top: int = 120) -> list[dict]` (piece dicts + `id`, `title`, `started_at`, `score`, best first), `query_text(question: str) -> str`; module function `cap(hits: list, per_meeting: int, total: int) -> list`.

- [ ] **Step 1: Write the failing tests** (a fake embedder: a vector from word hashes, unit length, counting calls)

```python
class IndexTests(unittest.TestCase):
    def test_update_reuses_unchanged_pieces(self):
        n1 = self.index.update(self.m, None)
        self.m['turns'].append({'start': 900.0, 'speaker': 'you', 'text': 'new topic'})
        n2 = self.index.update(self.m, None)
        self.assertGreater(n1, 0); self.assertEqual(n2, 1)

    def test_retitle_reembeds_everything(self):
        n1 = self.index.update(self.m, None)
        self.m['title'] = 'Another title'
        self.assertEqual(self.index.update(self.m, None), n1)

    def test_other_model_rebuilds(self):
        self.index.update(self.m, None)
        other = MeetingIndex(self.folder, 'other-model', self.embed)
        self.assertFalse(other.is_current(self.m['id'], 0))
        self.assertEqual(other.search(self.embed(['budget'])[0]), [])   # never mixes models

    def test_file_has_no_pickle(self):
        self.index.update(self.m, None)
        np.load(self.folder / f"{self.m['id']}.index.npz", allow_pickle=False)['rows']

    def test_search_filter_and_cap(self):
        hits = cap(self.index.search(self.embed(['budget'])[0]), per_meeting=2, total=30)
        self.assertLessEqual(max(Counter(h['id'] for h in hits).values()), 2)
        self.assertTrue(all(h['id'] == 'b' for h in self.index.search(self.embed(['budget'])[0], meeting_id='b')))

    def test_store_delete_removes_index(self):
        self.store.save(self.m); self.index.update(self.m, None)
        self.store.delete(self.m['id'])
        self.assertFalse((self.folder / f"{self.m['id']}.index.npz").exists())
```

- [ ] **Step 2: Run** `... IndexTests -v` — expect FAIL.
- [ ] **Step 3: Implement** per Global Constraints. In-memory cache: `{meeting_id: (mtime, vectors float32, rows)}`, refreshed for a file whose mtime changed; `search` stacks only entries whose stored model equals `self.model`. Writes go to a temp file then `os.replace`.
- [ ] **Step 4: Run** — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(meetings): per-meeting meaning index"`

---

### Task 5: Merging words and meaning

**Files:**
- Create: `src/koli/meeting/find.py`
- Test: `tests/test_meeting_search.py` (`MergeTests`)

**Interfaces:**
- Consumes: word hits (Task 3), meaning hits (Task 4).
- Produces: `merge(word_hits: list, meaning_hits: list, places: int = 3, limit: int = 50) -> list[dict]`, each `{'id', 'title', 'started_at', 'by': list[str] ('words', 'meaning'), 'places': [{'where', 'snippet', 'at'}]}` best first. Meaning places come first; `where` is `transcript|summary|screenshot|title`; snippet ≤ 160 chars, newlines as " · ".

- [ ] **Step 1: Write the failing tests**

```python
class MergeTests(unittest.TestCase):
    def test_found_by_both_ranks_above_either(self):
        words = [w('x'), w('both')]; meaning = [mh('y', 0.9), mh('both', 0.8)]
        out = merge(words, meaning)
        self.assertEqual(out[0]['id'], 'both')
        self.assertEqual(out[0]['by'], ['words', 'meaning'])

    def test_places_capped_meaning_first(self):
        out = merge([w('a')], [mh('a', 0.9, at=10), mh('a', 0.8, at=20), mh('a', 0.7, at=30), mh('a', 0.6, at=40)], places=3)
        self.assertEqual([p['at'] for p in out[0]['places']], [10, 20, 30])

    def test_words_only_when_no_meaning(self):
        self.assertEqual([r['id'] for r in merge([w('a'), w('b')], [])], ['a', 'b'])
```

- [ ] **Step 2: Run** — expect FAIL. **Step 3: Implement** reciprocal-rank merge (Global Constraints; a meeting's meaning rank is the rank of its best piece). **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(meetings): merge word and meaning results"`

---

### Task 6: The search service, the indexer and the settings

**Files:**
- Create: `src/koli/services/meeting_search.py`
- Modify: `src/koli/meeting/store.py` (`MeetingStore.on_change: Callable[[str], None] | None`, called after `save`, `save_brief`, `delete` outside the lock, exceptions logged)
- Modify: `src/koli/services/meetings.py:90-175` (create `self.finder = MeetingSearch(...)`, set `store.on_change`, `finder.start()` where the detector starts), `:1629-1630` (`search(query, meeting_id=None)` → `self.finder.search`). No applier: `MeetingSearch` reads `meeting.search.*` on every pass and search.
- Modify: `src/koli/config.defaults.yaml:679-745` (`meeting.search: {meaning: true, model: qwen3-embedding:0.6b}`), `src/koli/settings/schema.py:~519` (two `_S` rows, group `Search`)
- Modify: `src/koli/ui/bridge.py:734-737` (`meetings_search(query, meeting_id=None)`), add `meetings_search_status()`, `meetings_search_download()`
- Test: `tests/test_meeting_search.py` (`SearchServiceTests`)

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces: `class MeetingSearch(store: MeetingStore, setting: Callable[[str, object], object], is_running: Callable[[], bool], publish: Callable[[str, dict], None], embed: Callable | None = None, tasks=None)` with
  - `search(query: str, meeting_id: str | None = None) -> dict` → `{'results': merge(..., places=10 if meeting_id else 3), 'meaning': 'on'|'loading'|'off'|'no_ollama'|'no_model'|'indexing'}`
  - `changed(meeting_id: str)` (queue, 10 s debounce), `start()` (indexer thread; first pass queues every meeting whose index isn't current), `stop()`
  - `status() -> dict` `{'meaning', 'indexed': int, 'total': int, 'model': str}`, published as event `meeting_search` whenever it changes
  - `download() -> int` (task id; `ollama.pull` in `get_task_manager().run('Downloading the meaning model', fn, kind='model')`, then starts indexing)
  - a model setting different from the current index's model (seen on any pass or search) → new `MeetingIndex`, every meeting queued
  - `hits_for(question: str, meeting_id: str | None, per_meeting: int, total: int) -> list` (meaning pieces, used by Task 7).
- Errors from Ollama map to `meaning`: connection refused → `no_ollama`; "not found" → `no_model`; timeout past `QUERY_WAIT_S` → `loading` (and a background warm-up call).

- [ ] **Step 1: Write the failing tests** (fake embed, `is_running` flag, fake publish; store in a temp folder)

```python
class SearchServiceTests(unittest.TestCase):
    def test_search_without_model_returns_words_and_reason(self):
        svc = self.service(embed=raising(ProviderError('model "qwen3-embedding:0.6b" not found')))
        out = svc.search('budget')
        self.assertEqual(out['meaning'], 'no_model')
        self.assertEqual(out['results'][0]['by'], ['words'])

    def test_slow_model_returns_loading(self):
        svc = self.service(embed=sleeping(3.0))
        t = time.monotonic(); out = svc.search('budget')
        self.assertLess(time.monotonic() - t, 2.5); self.assertEqual(out['meaning'], 'loading')

    def test_indexer_waits_while_meeting_runs(self):
        self.running = True; svc = self.service(); svc.changed('a'); svc.run_pending(now=time.time() + 11)
        self.assertEqual(self.embed_calls, 0)
        self.running = False; svc.run_pending(now=time.time() + 11)
        self.assertGreater(self.embed_calls, 0)

    def test_store_change_queues_meeting(self):
        svc = self.service(); self.store.save_brief('a', {'tldr': 'We agreed the enterprise price'})
        self.assertIn('a', svc.pending())

    def test_model_change_reindexes(self):
        svc = self.service(); svc.run_all()
        self.settings['meeting.search.model'] = 'other'; svc.run_pending(now=time.time())
        self.assertEqual(set(svc.pending()), {'a', 'b', 'c'})

    def test_meaning_off_setting(self):
        self.settings['meeting.search.meaning'] = False
        self.assertEqual(self.service().search('budget')['meaning'], 'off')
```

`run_pending(now)`, `run_all()` and `pending()` are the indexer's single-step entry points; the thread calls `run_pending` every second.

- [ ] **Step 2: Run** — expect FAIL. **Step 3: Implement** per Interfaces; the thread is a daemon, low work per tick (one meeting per call). **Step 4: Run** SearchServiceTests and `tests.test_meeting` — PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(meetings): search service, background indexer, settings"`

---

### Task 7: Ask all meetings

**Files:**
- Create: `src/koli/meeting/ask.py`
- Modify: `src/koli/services/meeting_search.py` (add `ask`, `ask_history`, `ask_clear`)
- Modify: `src/koli/ui/bridge.py` (add `meetings_ask(question)`, `meetings_ask_history()`, `meetings_ask_clear()` next to `meeting_chat*`)
- Test: `tests/test_meeting_search.py` (`AskTests`)

**Interfaces:**
- Consumes: `MeetingSearch.hits_for`, `cap` (Task 4), `llm.complete`, `usage.ledger.for_meeting`.
- Produces (`meeting/ask.py`): `meeting_list(heads: list) -> str` (one line per meeting: id | start | title | app | people | turns | summary or not); `plan_messages(listing: str, history: list, question: str) -> list`; `clean_plan(plan: dict, known_ids: set) -> dict`; `material(plan: dict, question: str, load: Callable[[str], dict], hits_for: Callable) -> tuple[str, dict]` (text, `{meeting_id: duration_s}` of what went in); `answer_messages(task: str, material: str, history: list, question: str) -> list`; `clean_answer(answer: dict, given: dict) -> dict` → `{'reply', 'refs': [{'n', 'id', 'title', 'at'}]}`; `ask(question, history, heads, load, hits_for, complete, parse) -> dict`.
- Produces (service): `MeetingSearch.ask(question: str) -> dict` `{'messages': history}`; history items `{'role', 'text', 'at', 'refs'?}` saved to `<user data>/meetings_ask.json`.

- [ ] **Step 1: Write the failing tests** (fake `complete` returning scripted JSON per call)

```python
class AskTests(unittest.TestCase):
    def test_plan_drops_unknown_ids(self):
        self.assertEqual(clean_plan({'task': 't', 'search': ['a', 'b', 'c', 'd'], 'open': ['m1', 'nope']}, {'m1'}),
                         {'task': 't', 'search': ['a', 'b', 'c'], 'open': ['m1']})

    def test_open_meeting_goes_in_whole(self):
        text, given = material({'search': [], 'open': ['m1']}, 'q', self.load, self.hits_for)
        self.assertIn('0:15 Shahar: turn 1', text); self.assertIn('m1', given)

    def test_material_uses_pieces_when_meeting_too_long(self):
        text, _ = material({'search': [], 'open': ['long', 'm1', 'm2']}, 'q', self.load, self.hits_for)
        self.assertLessEqual(len(text), 60000)
        self.assertEqual(self.hits_for_calls[-1][1], 'long')            # restricted to that meeting

    def test_answer_drops_unknown_refs_and_markers(self):
        out = clean_answer({'reply': 'Pricing [1], budget [2].', 'refs': [{'id': 'm1', 'at': 30}, {'id': 'ghost', 'at': 5}]},
                           {'m1': 600.0})
        self.assertEqual(out['reply'], 'Pricing [1], budget.')
        self.assertEqual([r['id'] for r in out['refs']], ['m1'])

    def test_ref_past_the_end_dropped(self):
        self.assertEqual(clean_answer({'reply': 'x [1]', 'refs': [{'id': 'm1', 'at': 9999}]}, {'m1': 600.0})['refs'], [])

    def test_ask_records_cost_without_meeting(self):
        with mock.patch('koli.meeting.ask.for_meeting') as fm:
            ask('where is pricing?', [], self.heads, self.load, self.hits_for, self.complete, json.loads)
        fm.assert_called_with(None, 'meeting_ask')

    def test_history_saved_and_cleared(self):
        svc = self.service(); svc.ask('where is pricing?')
        self.assertEqual([m['role'] for m in svc.ask_history()], ['user', 'assistant'])
        svc.ask_clear(); self.assertEqual(svc.ask_history(), [])
```

- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement.** Prompts (exact text the implementer may polish, not change in meaning):
  - Plan system: "You help the user with their past meetings. You get the list of all meetings and the question. Choose how to answer: \"search\": up to 3 short phrases to find passages by meaning (add a Hebrew and an English phrasing when useful), for topics; \"open\": up to 3 meeting ids to read whole, when the question points at meetings by date, person or title. Use one or both. Answer with JSON only: {\"task\": \"<what the user wants, one line>\", \"search\": [...], \"open\": [...]}"
  - Answer system: "You answer questions about the user's past meetings from the material only: pieces of many meetings and/or whole transcripts, each headed [title | date | id | where m:ss]. Say so when the material doesn't tell. Mark each fact with [n] and list n in refs as {\"id\": \"<meeting id>\", \"at\": <second>}. Answer in the language of the question, short and concrete. JSON only: {\"reply\": \"...\", \"refs\": [...]}"
  - Material order: opened meetings first, then search pieces (`cap(per_meeting=4, total=30)` over the union of all phrases, best score per piece). Raise `SummaryError('Choose an AI model in Settings › AI first')` path as `chat_ask` does.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `git commit -m "feat(meetings): ask the AI about all meetings"`

---

### Task 8: The Meetings screen

**Files:**
- Modify: `src/koli/ui/web/js/screens/meetings.js:276-330` (results from `{results, meaning}`; up to 3 places per result, each a button opening that second; "by meaning" chip; "Only this meeting" switch passing the open meeting id; status line; model card; "Ask all meetings" button in `page-head`; Ctrl+Enter in the search box opens Ask with the text)
- Create: `src/koli/ui/web/js/screens/meetings_ask.js` (`AskAll({ initial, onOpen })`, the conversation with `mc-*` classes from `chat.js`; `[n]` in a reply becomes a button calling `onOpen(id, at)`)
- Modify: `src/koli/ui/web/js/screens/usage.js:15-16` (`meeting_ask: 'Ask all meetings'` in `FEATURES`)
- Modify: `src/koli/ui/web/js/mock-api.js` (`meetings_search`, `meetings_search_status`, `meetings_search_download`, `meetings_ask*`); not `mock-data.json` (another task has it open)
- Test: manual in the browser mock (`index.html?mock=1`) and the real app

**Interfaces:**
- Consumes: bridge `meetings_search(query, meeting_id)`, `meetings_search_status()`, `meetings_search_download()`, `meetings_ask(question)`, `meetings_ask_history()`, `meetings_ask_clear()`; event `meeting_search` (status).

Copy (exact):
- Status line: `loading` → "Search by meaning is starting…"; `indexing` → "Preparing search by meaning: {indexed} of {total} meetings"; `no_ollama` → "Search by meaning needs Ollama running (Settings › AI)"; `off` → nothing.
- Model card (`no_model`): title "Search by meaning", body "Find meetings by what was said, in Hebrew or English, not only by exact words. Needs a 640 MB model, kept on this computer.", button **Download**; progress from the task.
- Empty result: "No meeting matches “{query}”."
- Ask empty state examples: "Where did we talk about pricing?", "What did we decide about the Enterprise page?", "Summarize my last call with Shahar".

- [ ] **Step 1:** Serve the pages (`python -m http.server` in `src/koli/ui/web`) and open `index.html?mock=1#meetings`; confirm the mock answers the new calls.
- [ ] **Step 2:** Build the search results, switch, status line and card; check in the mock: a query shows places with times; clicking one opens the transcript at that second; "Only this meeting" limits results; with the mock answering `meaning: "loading"` once, the page asks again after 2 s and the meaning places appear.
- [ ] **Step 3:** Build `AskAll`; check in the mock: a question shows Thinking…, then the reply with `[1]` buttons that open the meeting; Clear uses `confirmModal`.
- [ ] **Step 4:** Grep `src/koli/ui/web/css` for each class you planned to add; reuse existing ones.
- [ ] **Step 5: Commit** `git commit -m "feat(meetings): search by meaning and Ask all meetings on the Meetings screen"`

---

### Task 9: Docs, live check, clean-up

**Files:**
- Modify: `docs/project-index.md` (Meeting Mode row: `pieces.py`, `index.py`, `find.py`, `ask.py`, `services/meeting_search.py`; design link)
- Modify: `docs/design/meeting-search.md` (anything the build changed)
- Delete after the owner's live test: `.temp/embed-lab/` (in `S:\Koli`, untracked)

- [ ] **Step 1:** Full suite: `PYTHONUTF8=1 S:/Koli/.venv/Scripts/python.exe -m unittest discover -s tests` — all PASS.
- [ ] **Step 2:** Launch the worktree's app (`/test-from-wsl`, launch only) and confirm the log shows the indexer starting and no errors.
- [ ] **Step 3:** Hand the owner the live test: the 4 questions from 2026-11-17 (file retention, resizer, salary appendix, bank reconciliation), "מס״ב", "Summarize my Nov 16 evening call with Shahar", and one in "Only this meeting".
- [ ] **Step 4: Commit** docs after the owner's OK: `git commit -m "docs(meetings): search by meaning"`
