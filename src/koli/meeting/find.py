# meeting/find.py
# Merges the word-search ranking and the meaning-search ranking into one list with a
# single entry per meeting, so a meeting both found counts above one found by only one.
import re
from typing import Optional

RANK_OFFSET = 60        # reciprocal rank fusion: 1 / (RANK_OFFSET + rank), rank from 1
SNIPPET_MAX_CHARS = 160
_LINE_BREAKS = re.compile(r"\r\n|\r|\n")


# ── Helpers ──────────────────────────────────────────────────────────────────

# One line of at most SNIPPET_MAX_CHARS; line breaks shown as " · " so a piece reads inline.
def _one_line_snippet(text: Optional[str]) -> str:
    return _LINE_BREAKS.sub(" · ", text or "")[:SNIPPET_MAX_CHARS]


# Reciprocal-rank contribution of a meeting at 1-based rank in one list.
def _rank_score(rank: int) -> float:
    return 1.0 / (RANK_OFFSET + rank)


# Meeting ids of a ranked hit list in first-appearance order (a meaning list has many pieces per meeting).
def _meetings_in_rank_order(hits: list) -> list:
    ordered = []
    seen = set()
    for hit in hits:
        if hit["id"] not in seen:
            seen.add(hit["id"])
            ordered.append(hit["id"])
    return ordered


# ── Merge ────────────────────────────────────────────────────────────────────

# Both rankings best first; returns meetings best first, each with up to `places` places
# (meaning pieces first, then the word hit's place when it is somewhere new).
def merge(word_hits: list, meaning_hits: list, places: int = 3, limit: int = 50) -> list:
    word_hit_by_meeting = {}
    for hit in word_hits:
        word_hit_by_meeting.setdefault(hit["id"], hit)
    pieces_by_meeting = {}
    for piece in meaning_hits:
        pieces_by_meeting.setdefault(piece["id"], []).append(piece)

    # Score per meeting, filled words list first so a tie keeps the word ranking's order.
    scores = {}
    for rank, meeting_id in enumerate(_meetings_in_rank_order(word_hits), start=1):
        scores[meeting_id] = scores.get(meeting_id, 0.0) + _rank_score(rank)
    for rank, meeting_id in enumerate(_meetings_in_rank_order(meaning_hits), start=1):
        scores[meeting_id] = scores.get(meeting_id, 0.0) + _rank_score(rank)
    ranked_ids = sorted(scores, key=lambda meeting_id: -scores[meeting_id])  # stable sort

    merged = []
    for meeting_id in ranked_ids[:limit]:
        word_hit = word_hit_by_meeting.get(meeting_id)
        pieces = pieces_by_meeting.get(meeting_id, [])
        meeting_places = [
            {"where": piece["kind"], "snippet": _one_line_snippet(piece.get("text")), "at": piece.get("at")}
            for piece in pieces
        ]
        if word_hit is not None:
            word_place = {"where": word_hit.get("where"), "snippet": _one_line_snippet(word_hit.get("snippet")),
                          "at": word_hit.get("at")}
            # A place without a second (summary, screenshot captions) is the same only when its text is too.
            already_shown = any(
                place["where"] == word_place["where"] and place["at"] == word_place["at"]
                and (word_place["at"] is not None or place["snippet"] == word_place["snippet"])
                for place in meeting_places[:places]
            )
            if not already_shown:
                meeting_places.append(word_place)
        source = word_hit if word_hit is not None else pieces[0]
        merged.append({
            "id": meeting_id,
            "title": source.get("title"),
            "started_at": source.get("started_at"),
            "by": (["words"] if word_hit is not None else []) + (["meaning"] if pieces else []),
            "places": meeting_places[:places],
        })
    return merged
