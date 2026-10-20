## Rules for every summary

- Every statement must come from the transcript. When you are unsure, leave it out or list it as an open point.
- Times are seconds from the start of the transcript: use the `start` of the turn where the thing was said. When the transcript's timing is "estimated", still give the times; they are approximate.
- Speaker ids in the JSON are the transcript's ids. Use each speaker's name as the speaker list gives it, spelled the same way everywhere. Rename "You" or "Speaker N" only when the transcript makes the person clear (people call them by name), writing that name in the report's language; never invent names.
- A speaker with only a word or two in the whole meeting is most likely a voice split off by mistake: leave them out of participants, attendees and roles.
- Caption transcripts are noisy (half words, words split or written in the wrong script). Read through the noise and write the clean meaning. Keep names and product names as they are really spelled.
- Quotes are the exact words from the transcript, in the original language, cleaned only of caption noise.
- An action item has an owner only when someone took it or was given it; otherwise the owner is null. A due date only when one was said; otherwise null.
- Status of an action item: "do_now", "plan_only" (plan or research, don't build yet), "waiting" (blocked on someone or something), "done" (finished in the meeting).
- Decisions are only what was agreed, not proposals.
- Off-topic moments (phone calls, jokes, personal talk) are not topics, decisions or tasks.
- Short, concrete lines a reader can act on: who, what, by when. No filler such as "The meeting discussed".
- Return only the JSON object, with no text before or after it.
