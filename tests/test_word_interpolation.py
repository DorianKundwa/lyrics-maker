"""Unmatched lyric words are spread between the matched words around them."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aligners.alignment_whisperx import _fill_word_gaps, _line_bounds_from_anchors  # noqa: E402


def _words(*specs):
    """specs: (text, start, end, matched) -> list of word dicts + matched flags."""
    out = []
    flags = []
    for text, start, end, matched in specs:
        out.append({"text": text, "start": start, "end": end})
        flags.append(matched)
    return out, flags


def test_unmatched_chunk_fills_the_gap_between_anchors():
    words, flags = _words(
        ("a", 1.0, 1.3, True),
        ("b", 0.0, 0.0, False),
        ("c", 0.0, 0.0, False),
        ("d", 4.0, 4.3, True),
    )
    _fill_word_gaps(words, 0.5, 5.0, flags)

    # The two unmatched words must spread out towards the next matched word,
    # not bunch up just after the previous one.
    assert words[1]["start"] >= words[0]["end"]
    assert words[2]["start"] > words[1]["end"] - 0.05
    assert words[2]["end"] > 3.0
    assert words[2]["end"] <= words[3]["start"]


def test_leading_unmatched_chunk_starts_at_line_begin():
    words, flags = _words(
        ("a", 0.0, 0.0, False),
        ("b", 0.0, 0.0, False),
        ("c", 2.0, 2.3, True),
    )
    _fill_word_gaps(words, 0.5, 3.0, flags)

    assert abs(words[0]["start"] - 0.5) < 1e-6
    assert words[1]["end"] <= words[2]["start"]
    assert words[1]["start"] > 1.0


def test_repeated_hook_words_do_not_stretch_the_line():
    # The same four words matched again 15 s later (an intro chant). The line
    # must stay short instead of covering the whole intro.
    starts = [1.0, 6.0, 11.0, 16.0]
    ends = [1.3, 6.3, 11.3, 16.3]
    begin, end = _line_bounds_from_anchors(starts, ends, 0.0, 20.0, 60.0, 4)
    assert end - begin < 4.0


def test_a_line_sung_in_one_breath_keeps_its_span():
    starts = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5]
    ends = [1.3, 1.8, 2.3, 2.8, 3.3, 3.8]
    begin, end = _line_bounds_from_anchors(starts, ends, 0.0, 5.0, 60.0, 6)
    assert abs(end - 3.83) < 0.05


def test_unmatched_line_does_not_stretch_to_the_next_line():
    # No word matched at all; the next line starts 14.5 s later. The line must
    # last its own singing time, not the whole gap.
    begin, end = _line_bounds_from_anchors([None] * 3, [None] * 3, 0.0, 14.489, 300.0, 3)
    assert end - begin < 4.0


def test_fully_unmatched_line_uses_whole_fragment():
    words, flags = _words(
        ("a", 0.0, 0.0, False),
        ("b", 0.0, 0.0, False),
        ("c", 0.0, 0.0, False),
    )
    _fill_word_gaps(words, 1.0, 4.0, flags)

    assert abs(words[0]["start"] - 1.0) < 1e-6
    assert abs(words[2]["start"] - 3.0) < 1e-6
    for prev, nxt in zip(words, words[1:]):
        assert nxt["start"] >= prev["end"] - 1e-6
