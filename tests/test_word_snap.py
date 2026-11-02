# tests/test_word_snap.py
# Near misses of your own words snap to them after Whisper (after Handy):
# spans of up to three words, never across punctuation or a line break,
# only name-like words, never a common word on its own.

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from koli.text_postprocess import postprocess  # noqa: E402
from koli.word_snap import snap_to_words  # noqa: E402

WORDS = ['ChargeBee', 'Kubernetes', 'Koli', 'GPT4']


class SnapTests(unittest.TestCase):
    def test_near_misses_become_your_words(self):
        self.assertEqual(snap_to_words('deploy to kubernetis today', WORDS), 'deploy to Kubernetes today')
        self.assertEqual(snap_to_words('Hey Kolli, open it', WORDS), 'Hey Koli, open it')
        self.assertEqual(snap_to_words('charge bee pricing', WORDS), 'ChargeBee pricing')
        self.assertEqual(snap_to_words('Use gpt 4 here', WORDS), 'Use GPT4 here')

    def test_a_span_stops_at_punctuation_and_line_breaks(self):
        self.assertEqual(snap_to_words('we use Charge B, che for billing', WORDS),
                         'we use ChargeBee, che for billing')
        self.assertEqual(snap_to_words('charge\nbee', ['ChargeBee']), 'charge\nbee')

    def test_a_short_name_that_only_sounds_alike_is_left_alone(self):
        # "Kyle" and "Koli" share a Soundex code; two letters of four differ.
        self.assertEqual(snap_to_words('ask Kyle about it', WORDS), 'ask Kyle about it')
        self.assertEqual(snap_to_words('Hey Kole', WORDS), 'Hey Kole')

    def test_ordinary_text_is_left_alone(self):
        for text in ('I like it a lot', 'the demo is done', 'call me later'):
            self.assertEqual(snap_to_words(text, WORDS + ['Deno']), text)

    def test_only_name_like_words_are_snapped_to(self):
        # A plain lower-case word in your list is spelled fine by Whisper.
        self.assertEqual(snap_to_words('the cat sat', ['cats']), 'the cat sat')

    def test_shouting_stays_shouting_and_layout_survives(self):
        self.assertEqual(snap_to_words('KUBERNETIS rocks', WORDS), 'KUBERNETES rocks')
        self.assertEqual(snap_to_words('a  kubernetis\n\nb', WORDS), 'a  Kubernetes\n\nb')

    def test_hebrew_gets_edit_distance_only(self):
        self.assertEqual(snap_to_words('שלום קולי', ['Koli']), 'שלום קולי')


class PipelineTests(unittest.TestCase):
    def test_the_pipeline_snaps_and_says_so(self):
        trace = []
        out = postprocess('deploy to kubernetis', {'snap_words': WORDS}, trace=trace)
        self.assertEqual(out, 'deploy to Kubernetes')
        self.assertIn('snap_to_words', trace)

    def test_it_can_be_turned_off(self):
        out = postprocess('deploy to kubernetis', {'snap_words': WORDS, 'snap_to_words': False})
        self.assertEqual(out, 'deploy to kubernetis')


if __name__ == '__main__':
    unittest.main()
