# agent/
# AI actions: a sentence no fixed voice command matches becomes a short
# script that can call only Koli's own actions. The script runs once, in
# rehearsal, to produce a list of operations; the policy, a second model and
# (when needed) your spoken password judge that list; then the list runs for
# real, logged for undo.
#   actions.py   the actions, rehearsal (Recorder) and the real run (replay)
#   sandbox.py   the code check and running a script in rehearsal
#   policy.py    your folders and the danger level of a list
#   planner.py   the writer and reviewer prompts
#   journal.py   history, undo, repeats that could become commands
#   approval.py  the spoken password
#   runner.py    the whole flow, with the bubble's events
# Design: docs/design/voice-agent.md.
