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
screenshots' captions at their moments). The model answers JSON
(`llm.complete('meetings', ..., json=True)`). The answer is checked and
tidied: version, template id, generator ("model via Koli"), list fields as
lists, unknown speaker ids dropped from `participants`. It is saved as the
brief; a fixed-title template keeps one of its titles, so the meeting keeps
its own title in the list.

**Language**, first that applies: the language picked when asking for the
summary; the template's `language` when it is a code; the setting
`meeting.summary.language` when it is a code; else the meeting's language
(the most common `lang` among the turns, by words).

**When**

- By hand: *Summarize* on the Summary tab (template and language pickers),
  and *Summarize again* once there is one.
- Automatically (`meeting.summary.auto`, on by default, needs an AI model):
  after a meeting ends, once the transcriber has caught up and the meeting
  has not resumed, and only when it lasted `meeting.summary.min_minutes` (2)
  with at least a few turns. A meeting that resumes and ends again is
  summarized again. An existing hand-run summary with another template is
  replaced only by a later hand run.
- One summary job per meeting at a time, as a background task (kind
  `meeting_summary`, ref = the meeting id), so the page shows progress and a
  failure shows as a problem.

Settings: `ai.meetings.model` / `ai.meetings.timeout` (180 s) in Settings ›
AI; `meeting.summary.auto`, `.template`, `.language`, `.min_minutes` in
Settings › Meetings.

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
