"""Per-segment offsets and stale karaoke words in the ASS generator."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from processor import _clean_word_timings, generate_ass  # noqa: E402


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


def test_implausible_word_gap_moves_the_line_to_its_dense_cluster():
    segment = _segment(
        start=1.0,
        end=6.0,
        text="one two three",
        words=[
            {"text": "one", "start": 1.0, "end": 4.9},
            {"text": "two", "start": 5.0, "end": 5.2},
            {"text": "three", "start": 5.2, "end": 5.4},
        ],
    )
    cleaned = _clean_word_timings(segment)
    assert cleaned["start"] == 5.0
    assert cleaned["end"] == 5.4
    starts = [word["start"] for word in cleaned["words"]]
    assert starts == [5.0, 5.133, 5.267]
    assert [word["end"] for word in cleaned["words"]] == [5.133, 5.267, 5.4]


def test_normal_word_spacing_is_not_changed():
    segment = _segment(
        start=1.0,
        end=3.0,
        text="one two three",
        words=[
            {"text": "one", "start": 1.0, "end": 1.5},
            {"text": "two", "start": 1.8, "end": 2.2},
            {"text": "three", "start": 2.4, "end": 2.9},
        ],
    )
    assert _clean_word_timings(segment) is segment


def test_overlapping_and_out_of_range_word_times_are_clamped():
    segment = _segment(
        start=1.0,
        end=3.0,
        text="one two three",
        words=[
            {"text": "one", "start": 0.5, "end": 1.4},
            {"text": "two", "start": 1.2, "end": 1.4},
            {"text": "three", "start": 2.9, "end": 4.0},
        ],
    )
    cleaned = _clean_word_timings(segment)
    assert [word["start"] for word in cleaned["words"]] == [1.0, 1.2, 2.9]
    assert cleaned["words"][0]["end"] <= cleaned["words"][1]["start"]
    assert cleaned["words"][-1]["end"] == 3.0


def test_hyphenated_words_keep_karaoke_and_original_spelling():
    segment = _segment(
        text="After-party, she comin' with me",
        words=[
            {"text": "After", "start": 1.0, "end": 1.3},
            {"text": "party,", "start": 1.3, "end": 1.6},
            {"text": "she", "start": 1.6, "end": 1.8},
            {"text": "comin'", "start": 1.8, "end": 2.1},
            {"text": "with", "start": 2.1, "end": 2.3},
            {"text": "me", "start": 2.3, "end": 2.6},
        ],
    )
    line = _dialogue(generate_ass([segment], lyric_style="classic"))[0]
    assert "\\k" in line
    assert "After-party," in line


def test_overlapping_word_intervals_do_not_run_past_the_line():
    segment = _segment(
        start=1.0,
        end=3.0,
        text="one two three",
        words=[
            {"text": "one", "start": 1.0, "end": 3.0},
            {"text": "two", "start": 1.1, "end": 1.2},
            {"text": "three", "start": 1.3, "end": 1.4},
        ],
    )
    line = _dialogue(generate_ass([segment], lyric_style="classic"))[0]
    durations = [int(value) for value in re.findall(r"\\k(\d+)", line)]
    assert sum(durations) == 200  # the 1.0-3.0 s line, in centiseconds


def test_character_level_scripts_keep_their_words_together():
    segment = _segment(
        start=1.0,
        end=2.0,
        text="你好 世界",
        words=[
            {"text": "你", "start": 1.0, "end": 1.2},
            {"text": "好", "start": 1.2, "end": 1.4},
            {"text": "世", "start": 1.4, "end": 1.7},
            {"text": "界", "start": 1.7, "end": 2.0},
        ],
    )
    line = _dialogue(generate_ass([segment], lyric_style="classic"))[0]
    assert "\\k" in line
    assert "你好" in line
    assert "世界" in line


def test_teleprompter_gap_does_not_extend_the_last_word_highlight():
    first = _segment(
        start=1.0, end=3.0, text="one two",
        words=[
            {"text": "one", "start": 1.0, "end": 1.5},
            {"text": "two", "start": 1.5, "end": 2.5},
        ],
    )
    second = _segment(
        start=6.0, end=7.0, text="three",
        words=[{"text": "three", "start": 6.0, "end": 6.5}],
    )
    ass = generate_ass([first, second], lyric_style="full_lyrics")
    current = next(
        line for line in _dialogue(ass)
        if line.startswith("Dialogue: 1") and "\\k" in line and "one" in line
    )
    durations = [int(value) for value in re.findall(r"\\k(\d+)", current)]
    assert sum(durations) == 200  # 1.0-3.0 s, not the display gap to 6.0 s


def test_gaps_keep_the_previous_word_highlighted():
    segment = _segment(
        start=1.0,
        end=3.0,
        text="one two",
        words=[
            {"text": "one", "start": 1.0, "end": 1.2},
            {"text": "two", "start": 2.5, "end": 2.8},
        ],
    )
    line = _dialogue(generate_ass([segment], lyric_style="classic"))[0]
    durations = [int(value) for value in re.findall(r"\\k(\d+)", line)]
    assert durations == [150, 50]
    assert not re.search(r"\{\\k\d+\}\{\\k", line)


# --- Long lines in multi-row styles -----------------------------------------

_LONG_LINE = " ".join(["handsome"] * 18)


def _segments(text, nxt="three four"):
    return [
        _segment(start=1.0, end=2.0, text=text, words=[]),
        _segment(start=2.0, end=3.0, text=nxt, words=[]),
    ]


def test_single_position_styles_still_allow_libass_wrapping():
    ass = generate_ass([_segment()], lyric_style="classic")
    assert "WrapStyle: 0" in ass


def test_multi_row_styles_disable_auto_wrapping():
    for style in ("two_line", "full_lyrics"):
        assert "WrapStyle: 2" in generate_ass([_segment()], lyric_style=style)


def test_overlong_line_is_fitted_onto_one_row():
    line = _dialogue(generate_ass(
        _segments(_LONG_LINE), lyric_style="two_line", font_size=110,
    ))[0]
    sizes = [int(value) for value in re.findall(r"{\\fs(\d+)", line)]
    assert sizes, line
    assert sizes[0] < int(110 * 0.85)   # the style's own effective size
    assert "\\N" not in line      # one row, never a wrapped second row


def test_short_line_keeps_the_style_font_size():
    line = _dialogue(generate_ass(
        [_segment()], lyric_style="two_line", font_size=72,
    ))[0]
    assert not re.search(r"{\\fs\d+", line)


def test_single_position_style_keeps_its_font_size():
    line = _dialogue(generate_ass(
        _segments(_LONG_LINE), lyric_style="classic", font_size=110,
    ))[0]
    assert not re.search(r"{\\fs\d+", line)  # wrapping handles it instead


def test_fitting_never_shrinks_below_the_floor():
    huge = " ".join(["extraordinarily"] * 40)
    line = _dialogue(generate_ass(
        _segments(huge), lyric_style="two_line", font_size=100,
    ))[0]
    size = int(re.search(r"{\\fs(\d+)", line).group(1))
    assert size == 51   # 60 % of the 85 px effective size


def test_long_line_does_not_move_the_two_line_rows():
    lines = _dialogue(generate_ass(
        _segments(_LONG_LINE), lyric_style="two_line", font_size=72,
    ))
    assert "\\pos(960,470)" in lines[0]   # current row stays put
    assert "\\pos(960,552)" in lines[1]   # preview row keeps its pitch


def test_teleprompter_neighbours_are_fitted_from_their_own_text():
    lines = _dialogue(generate_ass(
        _segments("short line", _LONG_LINE), lyric_style="full_lyrics", font_size=72,
    ))
    assert any(re.search(r"{\\fs\d+", line) for line in lines)
