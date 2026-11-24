# The chat's language: settling a weak language guess

*Design note, 2026-11-01. Measured on the self-learning clips before it was
built; implemented on `feat/chat-language`.*

## Goal

When Whisper can't tell your languages apart from the audio (a short
"Commit it", "As you recommended"), use the language you have been writing
in that chat or window, instead of always your first language. English
written as Hebrew gibberish ("שקפת אם נווס פסת" for "Check if news passed")
was the case that kept coming back.

## Behaviour

| Situation | Language Whisper writes |
|-----------|-------------------------|
| Whisper is at least 0.5 sure (its raw score for the language) | Its guess, as before. The chat never overrides a clear guess. |
| Unsure, and this chat has history | The most common language of your last 5 dictations into the same app and window title; the newest of them breaks a tie. |
| Unsure, this chat has no history (or the setting is off) | Your first language, as before. |
| Unsure, top guess outside your languages (Portuguese for a short English word) | The best of your languages, as before: the chat isn't needed there. |
| A language forced by Settings, a rule or a profile | Forced, as before: there is no guess. |
| One language in "Languages I speak" | That one, as before. |

"Chat" means the app plus the window title: Discord, WhatsApp, Telegram and
the browser name the channel, contact or page in the title; an editor names
the file and project. The whole app never stands in for a chat (see below).

The chat's language is passed only by dictations: the hotkey and auto
recordings (`StateManager.chat_language()`, looked up when the recording is
transcribed) and the bubble's phrases (looked up once when the bubble opens,
since clicking into the bubble moves the focus to Koli). Voice commands, the
wake word, meetings and practice never get it. Online models aren't told
it either: naming a language they didn't hear made Gemini write English in
Hebrew letters; only local Whisper uses it, including when an online model
falls back to it.

The weak-guess routing stays on the main model: a weak guess never goes to
the second (Hebrew) model, which translates speech in another language.
`last_info['language_from'] = 'chat'` marks a dictation the chat decided,
and app.log says "Only 0.26 sure it's he: this chat is written in en".

Auto-dictation still drops a clip whose language guess is very weak (below
0.6, `confident_speech`): there a weak guess means the sound may not be
speech at all (a TV), which the chat's language doesn't change.

Setting: `whisper.chat_language` (Settings › General, "Use the chat's
language when unsure"), on by default.

## How it was measured

Every dictation's clip, app, window title, language and Whisper's scores are
kept by self-learning (2303 clips, 276 minutes, October 2026). For the 649
clips Whisper was unsure of, each was transcribed forced to Hebrew and
forced to English with large-v3-turbo; the clearly more confident one (by
0.15 log-probability) counted as the truth, and the doubtful ones were read
by hand. The clips were then replayed in time order with the history as it
was at each moment.

| Rule for an unsure clip | Fixed | Broken |
|-------------------------|-------|--------|
| Chat, then the whole app | 43 | 6 |
| Chat only (built) | 38 | 1 |

The whole app broke short Hebrew replies ("תודה", "כן") in apps where you
mostly write English (Cursor), so only the chat counts.

The replay over-counted: it didn't model the existing rule that a top guess
outside your languages already gets the best of yours. Run through the real
engine on the 39 clips the replay changed, the chat's language fixed 13
(every one Hebrew gibberish for English: "You can commit it", "Merge back to
main", "I see it", "in prod", "Cloudflare") and broke none; the other 26
came out the same as today.

## Short clips heard again (2026-11-24)

The weak-guess rule missed one or two Hebrew words in a Hebrew chat:
"Nakhon" for נכון, "Dov." for טוב, "Gantt, Dakhini." for כן תכיני. Whisper
was sure enough of English (0.56-0.66), or its top guess was Arabic and
English won among your languages, so the chat was never asked.

Now a dictation of 4.5 s or less, written in another language than the
chat's, is decoded once more in the chat's language when that is your main
one. The redo runs on the main model without the prompt and favored words
(written in English, they pulled the Hebrew redo to Latin letters:
"Ganttahini"). It replaces the first version when Whisper's average
log-probability is at least 0.15 better, and never when it is an invented
thank-you (forced to Hebrew, Whisper wrote תודה רבה for "Automate"). The
log says "Short clip written in en (-0.81), this chat is in he: heard again
in he (-0.51), kept"; `language_from: chat` marks it. It costs one more
decode (about 0.2 s) only on such clips.

Measured on 509 short clips whose language differed from the chat's, each
decoded forced to Hebrew and to English, then the 169 English ones in
Hebrew chats run through the real engine. The redo changed 27: about 14
fixed (the four above, "Bootsa" בוצע, "low." לא, "Kamuts time" כמות שתיים,
"Video" בדיוק...), 2 broken ("Again, let's test", "Answer in English
please"; "Dev3" broke at 0.13 and is under the margin now), the rest wrong
either way. The other direction (Hebrew clips in an English chat) broke as
many as it fixed (מטופל became "Metupal"), so only your main language is
tried.

### English that isn't English

"Atma Dehima" for את מדהימה still came out in English: Whisper was 0.06
surer of it. When no word of the English is a real word (each takes two or
more pieces in Whisper's vocabulary, lowercased; your favored words never
count), the chat's version needs only to be within 0.08. In the replay that
fixed "Tukan" (תוקן) and "Bootsam" (בוצע) and broke nothing; the nearest
real English, "Grokbot", was 0.10 surer and stays.

## Not done (ideas for later)

- Your last sentence in the chat as a short hint to Whisper, for names and
  spelling. Needs care: a hint in the prompt was echoed back as the
  transcript (online models, October 2026).
- The other person's messages on screen as a stronger signal of the chat's
  language (UI Automation); more work and more private.
- A chat's language for online models, if a provider can take a hint
  without forcing it.
