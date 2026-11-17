# Meeting search by meaning, and asking the AI about all meetings

*2026-11-17. `meeting/pieces.py`, `meeting/index.py`, `meeting/find.py`,
`meeting/ask.py`, `services/meeting_search.py`, the Meetings screen. Built from a
test on 118 real meetings first (`.temp/embed-lab/lab.py`); the owner asked for
results before a design.*

## Why

The search box on the Meetings screen finds a meeting only when every word you
type is in it. "באיזו פגישה דיברנו על כמה זמן שומרים קבצים" found nothing, and
so did every other question written as a sentence. It can't match a Hebrew word
with a prefix ("בתקציב" for "תקציב"), a different wording ("hiring" for
"interviews"), or the other language (an English question about a Hebrew
meeting). And the AI could only talk about one meeting at a time, in its Chat
tab: nothing answered "where is the meeting about X?".

## What you get

| Where | What it does |
|---|---|
| The search box, all meetings | Finds meetings by meaning and by words together. Each meeting shows up to 3 places it matched (summary line, transcript at m:ss, screenshot caption); a click opens it there |
| The search box with **Only this meeting** on | The same search limited to the meeting that's open, up to 10 places in it |
| **Ask all meetings** (button in the page head; Ctrl+Enter in the search box asks what's typed) | The right side shows a conversation like the Chat tab's. The AI answers from all your meetings: which meeting, when, what was said, with numbered links that open the meeting at that second. Follow-up questions keep the conversation |
| The Chat tab of one meeting | Unchanged: the AI reads that whole meeting |

Meetings with no summary are searched and asked about too, from their transcript.
"Summarize the call with Shahar on Nov 16" works by opening that meeting whole.

## What the test showed (2026-11-17, 118 meetings, 3,287 pieces)

Measured, and the design keeps them:

- **Qwen3-Embedding 0.6B through Ollama is good enough** for Hebrew, English and
  questions in one language about a meeting in the other: the right meeting first
  for "file retention", "resizer in the test environment" (English → Hebrew),
  "salary appendix" (English → Hebrew), and bank reconciliation in a Discord call
  with no summary (top 3). Indexing all meetings took 160 s once.
- **Transcript windows of 75 s with 20 s overlap**, "Name: text" lines, the meeting's
  title and date in front of every piece. Without the title, "the meeting about X"
  finds a sentence, not a meeting.
- **At most 4 pieces per meeting** in what the AI reads. Without the cap one meeting
  took 13 of 30 places and the best meeting fell out.
- **The AI plans first, then answers.** It reads the list of all meetings (date,
  title, app, people, summary or not) and the question, and chooses search phrases
  (it writes both a Hebrew and an English one) and/or meetings to open whole. A date
  or a person ("the Nov 16 evening call") opens the meeting; a topic searches. About
  4 s to plan and 7–12 s to answer with gpt-6.1-terra.
- **Words still matter.** Meaning alone put "מס״ב" 8th and "בעיה במס״ב" 2nd; the
  word search found both first. (An earlier note said word search found nothing
  for it; that output had been cut short.) Names, numbers and order IDs are the
  same.
- **How words and meaning combine** (8 known questions, the right meeting's place):

  | Way to combine | Better than meaning alone | Worse |
  |---|---|---|
  | Any word scores, rare words more, equal merge | 2 | 5 (a sentence matches some word in most meetings) |
  | Only meetings with every uncommon word, ranked by how often they say them | 3 ("מס״ב" #8 → #1, "בעיה במס״ב" #2 → #1) | 1 (file retention #2 → #3) |

  The second is the design.
- **Model timings:** a warm question embeds in 0.02 s; a cold one (Ollama unloaded
  the model) takes 3.5 s; the largest meeting (85 pieces) embeds in 1.7 s. The model
  is 639 MB; all vectors take 13.5 MB in memory.
- **Summary noise:** fields such as the template's labels and the generator line
  matched every Hebrew question. Only content goes in.

Not tested, decided here: the clickable references, searching inside one meeting,
the download card.

## How it works

### Pieces

`meeting/pieces.py` turns a meeting and its summary into pieces, each
`{kind, at, text}`:

- **transcript:** consecutive turns grouped into 75 s windows that overlap by 20 s,
  one "Name: text" line per turn; `at` is the window's first second.
- **summary:** one piece per item of each summary field (each decision, topic,
  action item, agenda point, the overview), as plain text through the same walk
  `store.py` uses for word search, never raw JSON. Fields a template adds are
  included; layout fields are not (`labels`, `generator`, `template`, `version`,
  `written`, `language`, `date`, `meeting_id`, `title`, `participants`, `roles`,
  `attendees`, `instructions`). An item shorter than 15 characters is dropped.
  `at` is the item's own `at` or `start` when it has one.
- **screenshot:** each caption, at its second.

What gets embedded is the label `Meeting "<title>" on <date>` plus the piece.

### The index

`meeting/index.py` keeps `<id>.index.npz` beside the meeting: the vectors
(float16), a hash of each piece's embedded text, the pieces as a JSON string, and
the model name. No pickle. Updating a meeting embeds only the pieces whose hash is
new. A different model name rebuilds the file. Deleting a meeting deletes its
index.

All meetings' vectors are kept in memory (about 13 MB for today's 3,300 pieces)
and reloaded per meeting when its file changes. A search is one matrix product.

The question gets Qwen3's instruction in front: `Instruct: Given a question about
past meetings, retrieve the passages that answer it\nQuery: <question>`. Pieces
get none.

An index is current when it is newer than both the meeting file and its summary
file and was made with the chosen model.

**When a meeting is indexed:** when it ends, when its summary is written or
changed (a summary, the Chat tab's changes, a rename, attendees, a caption), and
for every meeting without an up-to-date index when Koli starts. The indexer waits
while a meeting is running, so Whisper keeps the GPU. A new summary usually
retitles the meeting, and the title is in every piece, so the whole meeting is
embedded again: up to 2 s, intended. The model setting is read on every pass, so
changing it re-indexes every meeting without a restart.

### Words

The word search compares whole words, not pieces of words. Words that appear in
more than 30% of meetings ("the", "דיברנו", "פגישה") are ignored. A meeting is a
word hit when it holds every remaining word of the query, and it ranks by
`Σ log(1 + meetings / meetings_with_word) · log(1 + times_said)`. A Hebrew word
longer than 3 letters also counts without one leading prefix letter (ו ה ב ל מ ש
כ), in the question and in the meeting, so "בתקציב" and "תקציב" find each other. A
query left with no word after that has no word hits; meaning alone answers it. A
hit still says where it matched, with a snippet and a second.

### Both together

`meeting/find.py` merges the two meeting rankings by reciprocal rank (`1 / (60 +
rank)` from each list, equal weight), as measured above. Each result keeps up to 3 matched places, meaning places
first, and says what found it (`words`, `meaning` or both).

### Never waiting on the model

Every call to the model asks Ollama to keep it loaded for 30 minutes
(`keep_alive: "30m"`), so typing doesn't pay the 3.5 s cold start each time. The
search box answers with the word results right away. The meaning results come in
the same call when the model answers within 2 s; otherwise the call returns words
only with `meaning: "loading"`, the model loads in the background, and the page
asks again 2 s later. If Ollama isn't running, the model isn't
downloaded or the GPU is full, the page shows word results and one line saying
why. Search during a live meeting works: one question is a tiny embedding.

### Asking all meetings

`meeting/ask.py`, the same shape as `meeting/chat.py`:

1. **Plan.** The AI gets the meeting list and the question (and the earlier
   questions), and answers `{"task", "search": [up to 3 phrases], "open": [up to 3
   meeting ids]}`. Unknown ids are dropped.
2. **Material.** Search phrases give up to 30 pieces, at most 4 per meeting. An
   opened meeting goes in whole if the material stays under 60,000 characters;
   otherwise its 12 best pieces for the question go in.
3. **Answer.** The AI answers `{"reply", "refs": [{"id", "at"}]}`, with `[1]`,
   `[2]` in the reply pointing at `refs`. A ref whose meeting wasn't in the
   material, or whose second is past the meeting's end, is dropped, and so is its
   marker.

Both steps use the Meetings model (`ai.meetings.model`, "Summarize with"). The
cost is recorded under the feature `meeting_ask` ("Ask all meetings" on the Usage
screen), with no meeting. The
conversation is kept in `<data>/meetings_ask.json` (outside the meetings folder,
so the meeting list never reads it), last 10 questions sent again, a Clear button.

## Settings

In Meetings › Search:

| Key | Label | Default |
|---|---|---|
| `meeting.search.meaning` | Search by meaning | on |
| `meeting.search.model` | Meaning model (Ollama) | `qwen3-embedding:0.6b` |

Ollama's address is the one on the Ollama card (`ai.ollama.address`). When the
model isn't downloaded, the Meetings screen shows a card: "Search by meaning
needs a 640 MB model" with **Download**, which pulls it through Ollama as a
background task with progress.

## Decisions

- **Local only for embeddings.** Meetings stay on this computer for search; only
  Ask sends pieces to the AI model you chose, as summaries already do.
- **No vector database.** A few thousand pieces fit in memory; one file per
  meeting follows the store's `<id>.<kind>` pattern.
- **One shared search function** (`MeetingSearch.search(query, meeting_id=None)`)
  serves the search box, the in-meeting find and Ask.
- **Fix found on the way:** the meeting list tries to read `<id>.chat.json` as a
  meeting and logs "Skipping unreadable meeting". It skips chat files now.

## Later

- The voice agent asking the same question ("Hey Koli, which meeting talked about
  pricing?") through `meeting/ask.py`.
- The Chat tab on a meeting too long for the model: its best pieces instead of the
  whole transcript.
- OpenAI or Gemini embeddings for computers without Ollama.
- A bigger model (`qwen3-embedding:4b`) if real use shows misses.
