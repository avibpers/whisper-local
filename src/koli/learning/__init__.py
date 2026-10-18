# learning/
# Koli learns your voice by itself, from the dictations you already make.
# Phase 1 (this package today): keep each dictation's clip and what happened
# to it, inside a disk cap you choose. Later phases re-listen overnight, vote,
# learn and test without asking you. Design: docs/design/self-learning.md.
#
#   clips.py     the clip store: audio files + one record per dictation
#   retention.py what gets deleted when, so the disk never keeps growing
#   recorder.py  watches each dictation and hands it to the store
