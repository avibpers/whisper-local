# Meeting summaries written by the AI, from templates

A meeting's Summary tab, its PDF and its Google Doc show `<id>.brief.json`.
Until now that file was written by hand. This writes it with the AI model, from
a **summary template** the user picks, and lets the user manage the templates.

## Templates

A template is one Markdown file, shared with the `meeting-sum` skill
(github.com/avibpers/AvushSkills): a YAML header for the layout and a body that
is the **system prompt** the model follows to write the brief.

```yaml
---
id: board-meeting
name: Board / management meeting
description: One line for the list.
language: meeting          # meeting = the language most of the meeting was in, or a code (he, en)
title: { mode: fixed, choices: ["Board Meeting Summary", "Management Meeting Summary"] }
date: date                 # date or datetime
blocks: [header, participants, agenda, agenda_details, next_meeting]
doc_blocks: [...]          # optional: the Google Doc's order
labels: { agenda: Meeting Agenda }
---
<system prompt; {{common_rules}} is replaced by _common.md>
```

- Built in (`meeting/templates/`): `standard` (the visual brief) and
  `board-meeting` (minutes: participants, agenda, per item discussion /
  decisions / action items, points for the next meeting).
- The user's own: `%APPDATA%\koli\summary_templates\`. Koli also reads
  `~/.claude/meeting-templates/`, so templates made with the skill appear in
  Koli. A user template with a built-in's id replaces it. Koli writes only to
  its own folder; built-ins are never edited (Edit makes a copy).
- `meeting.summary.template` is the default.

**Blocks** the app, the PDF and the Google Doc know: header, tldr, counts,
participants, agenda, agenda_details, decisions, action_items, lists,
open_questions, parked, next_meeting, speakers, screenshots, topics, sections,
notes, transcript, data, quick, page_break. In the app the transcript and the
data have their own tabs, so those blocks are skipped there.

## Writing the summary

`meeting/summarize.py` builds the messages: the template's prompt as the
system message, then the meeting as text (title, date, length, the speakers
with their ids, every turn as `[seconds] id (name): text`, and the
screenshots' captions at their moments), plus everyone who was there with
their talk time (`store.roster`: the voices, the people the call app showed,
the names added with *+ Person*; someone silent reads "was there but did not
speak"). Your instructions from the Summarize box come as a last user
message and win over the template. The model answers JSON
(`llm.complete('meetings', ..., json=True)`). The answer is checked and
tidied: version, template id, generator ("model via Koli"), list fields as
lists, unknown speaker ids dropped from `participants`. It is saved as the
brief; a fixed-title template keeps one of its titles, so the meeting keeps
its own title in the list. The brief keeps the instructions it was written
with (`instructions`).

**Subject.** Every answer has a `subject` (what the meeting was about, a few
words). A fixed-title template ("Management Meeting Summary") keeps its title
on the report, and the meeting is named by the subject instead.

**The report's words.** A summary in any language but English also brings
`labels`: every fixed word of the report (section headings, tab names, table
headers, statuses; `summary_templates.WORDS`) in that language, and a fixed
title translated. The Summary tab and the Google Doc read them first, so a
Hebrew meeting gets Hebrew headings; English is the fallback.

**Made-up fields.** A section the user asked for must go in `sections`; a
field the model invents anyway (say `avi_vision`) is folded into a section
(`summarize.fold_extras`), when the summary is written and whenever an older
one is read. The Summary tab and the Google Doc show `sections` even when the
template doesn't list them, so both show the same thing.

**Long meetings.** The whole transcript goes to the model in one request
(a 90-minute meeting is about 30,000 tokens; the models used read far more).
The shared rules say the closing summary ("the decision is…") is the main
outcome and comes first; the board template opens with Main Decisions.

**Names from the summary.** The model's `participants` name the speakers
still called "Speaker N" in the meeting (never "You", never a name you gave),
and their saved voice takes the name when it has none, so the next meeting
recognises them. Rename the speaker to correct a wrong guess.

**People who said little or nothing** are in the report: the Summary tab and
the Google Doc list them in the speakers table ("Did not speak") and in the
participants, and the shared rules tell the model to list everyone present.

**Language**, first that applies: the language picked when asking for the
summary; the template's `language` when it is a code; the setting
`meeting.summary.language` when it is a code; else the meeting's language
(the most common `lang` among the turns, by words).

**When**

- By hand: *Summarize* on the Summary tab (template, language and
  instructions for the AI), and *Summarize again* once there is one: a dialog
  with the same choices, starting from the last summary's template and
  instructions.
- The template: the one picked in the meeting panel while the call was
  offered or ran (`summary_template` on the meeting), else the default.
- Automatically (`meeting.summary.auto`, on by default, needs an AI model):
  after a meeting ends, once the transcriber has caught up and the meeting
  has not resumed, and only when it lasted `meeting.summary.min_minutes` (10)
  with at least a few turns. A meeting that resumes and ends again is
  summarized again. An existing hand-run summary with another template is
  replaced only by a later hand run.
- One summary job per meeting at a time, as a background task (kind
  `meeting_summary`, ref = the meeting id), so the page shows progress and a
  failure shows as a problem.

Settings › Meetings › Summary holds them all: `meeting.summary.auto`,
`.min_minutes`, the model (`ai.meetings.model`, kept in the `ai:` part of the
file like every AI feature), `.language` and the timeout (`ai.meetings.timeout`,
180 s); the default template is chosen in Summary templates. The Meetings
screen's *Meeting settings* button opens the same Meetings section in a modal.

## Chat

A meeting's **Chat** tab (`meeting/chat.py`, `screens/chat.js`) asks the AI
about that meeting: it reads the meeting text and the brief, plus the last 20
messages, and answers `{"reply", "changes"}`. Changes it may make, checked
before they are done: `rename_speaker` (an id from the meeting; names the
voice too), `add_attendee` / `remove_attendee`, `set_title`, `edit_summary`
(whole fields of the brief, tidied like a fresh summary) and `resummarize`
(with instructions). On a running meeting only names and attendees change.
The conversation is kept in `<id>.chat.json`; the answer lists what changed.

## Google Drive follows the meeting

A meeting saved to Drive once is updated there after each change (a summary,
a name, an attendee, a caption, a chat edit), a few quiet seconds after the
last one: the same doc, so its link and sharing stay (`gdoc.export` deletes
the doc's extra tabs, uploads the report onto the doc, renames the folder when
the title changed, and adds the tabs again). A doc that is gone is made anew.

## Search

The Meetings screen searches every saved meeting: all words of the query must
appear somewhere in its title, speakers, summary, screenshot captions or
transcript. A transcript hit opens the meeting on that turn.

## Managing templates

Meetings › **Templates**: the list (built-in, mine, from the skill), each with
its sections; actions per template: preview its sections, set as default,
duplicate, edit (the whole file in an editor, checked on save), delete (own
templates, with a confirm). *New template* offers:

- **Describe it**: the user writes what the summary should hold; the model
  writes the template file.
- **From past summaries**: the user pastes one or more summaries they like
  (or opens .md / .txt / .html files); the model works out their shared
  structure, headings, tone and length and writes the template.
- **Copy of**: an existing template under a new name.

A drafted template opens in the editor for review; it is saved only when
the user presses Save and the check passes.

## Not in this step

- Splitting very long meetings into parts for small local models: a model
  with too small a context fails with its own error, shown as a problem.
- Translating the transcript tab.
