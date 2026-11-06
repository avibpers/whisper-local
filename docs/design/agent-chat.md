# The conversation with the agent

*2026-11-06. `agent/chat.py`, the wake listener's `CHAT` state, the window
`ui/web/agent.html` and `js/agent.js`. The owner described the flow and left the
rest to these decisions.*

## Why

After a command ran, the bubble said "Anything else?" and listened 5 seconds,
but what you said then could only run a fixed command: it never reached the AI,
and a miss said nothing. On 2026-11-06 "music" ran, then "open download folder"
matched no command (the command is "open downloads") and was dropped without a
word.

## How it goes

| You say | What happens |
|---|---|
| Your wake word and a fixed command ("music") | It runs, as before. "Anything else?" for `follow_up_seconds` |
| Anything in those seconds | The conversation opens: a floating window, bottom right |
| Your wake word and something no command matches | The AI takes it, and the conversation opens |
| The command hotkey and something the AI takes | The conversation opens too |
| In the conversation, anything | No wake word: a fixed command if one matches, otherwise the AI |
| Quiet for `wake_word.chat_silence_seconds` (20) | It closes. The time doesn't run while the AI works or waits for your answer |
| × in the window | It closes, and an AI action still running stops |

## Decisions

- **Only with your own wake word.** The conversation hears whoever talks, so each
  sentence must sound like you, checked like your wake word. With "Hey Koli",
  which anyone can say, follow-ups stay media keys only and no conversation opens
  by voice.
- **A sentence ends at a 1.5 s pause** (`CHAT_END_S`), shorter than a first
  command's 2 s: you're replying.
- **The window keeps the time**, not the listener: the listener follows
  `ChatSession.is_open()`. Dictating meanwhile pauses the listening; it comes back
  while the window is open.
- **It shows** what you said, what Koli did (done, failed, didn't understand),
  the AI's questions, a status line while it works, and Approve and Cancel. The
  bubble stays quiet meanwhile. The window is hidden from screen sharing.
- **Command phrases ignore plurals**: "open download folder" runs "open downloads".
- **Not in this version:** typing in the window, moving it to a remembered place,
  and a conversation in a meeting (the wake word doesn't listen during one).
