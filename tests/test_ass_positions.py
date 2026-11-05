"""Per-segment offsets and stale karaoke words in the ASS generator."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from processor import generate_ass  # noqa: E402


def _segment(**overrides):
    segment = {
        "start": 1.0,
        "end": 3.0,
        "text": "one two",
        "words": [
            {"text": "one", "start": 1.0, "end": 1.4},
            {"text": "two", "start": 1.6, "end": 2.2},
        ],
        "y_offset": 0,
    }
    segment.update(overrides)
    return segment


def _dialogue(ass):
    return [line for line in ass.splitlines() if line.startswith("Dialogue:")]


def test_y_offset_moves_every_position_up():
    line = _dialogue(generate_ass([_segment(y_offset=50)], lyric_style="classic"))[0]
    assert "\\move(960,505,960,490,0,500)" in line


def test_x_offset_shifts_position_right():
    line = _dialogue(generate_ass([_segment(x_offset=30)], lyric_style="minimal"))[0]
    assert "\\pos(990,900)" in line


def test_two_line_style_shifts_both_rows():
    lines = _dialogue(generate_ass(
        [_segment(start=1.0, end=2.0, y_offset=40),
         _segment(start=2.0, end=3.0, text="three four")],
        lyric_style="two_line",
    ))
    # The first segment's current line and its "next line" preview both move up
    # (rows sit at 470 and 552 for this font size, minus the 40 px offset).
    assert "\\pos(960,430)" in lines[0]
    assert "\\pos(960,512)" in lines[1]


def test_karaoke_is_kept_when_words_still_spell_the_line():
    line = _dialogue(generate_ass([_segment()], lyric_style="classic"))[0]
    assert "\\k" in line


def test_karaoke_is_dropped_when_the_text_was_edited():
    edited = _segment(text="a different line")
    line = _dialogue(generate_ass([edited], lyric_style="classic"))[0]
    assert "\\k" not in line
    assert "a different line" in line
