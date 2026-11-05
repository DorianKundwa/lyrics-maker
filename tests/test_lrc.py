"""LRC timing: fractions of any length, multiple tags, offset, ASS carry."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from processor import parse_lyrics, seconds_to_ass  # noqa: E402


def _write(tmp_path, body):
    path = tmp_path / "lyrics.lrc"
    path.write_text(body, encoding="utf-8")
    return path


def test_fraction_digits_are_decimal_seconds(tmp_path):
    path = _write(tmp_path, "[00:05.5]a\n[00:10.50]b\n[00:15.500]c\n")
    assert parse_lyrics(path) == [(5.5, "a"), (10.5, "b"), (15.5, "c")]


def test_millisecond_fraction_does_not_become_seconds(tmp_path):
    # The old parser read .500 as 500 centiseconds, adding five whole seconds.
    path = _write(tmp_path, "[00:05.500]hello\n[00:07.500]world\n")
    assert [start for start, _ in parse_lyrics(path)] == [5.5, 7.5]


def test_several_timestamps_on_one_line(tmp_path):
    path = _write(tmp_path, "[00:10][01:02.25]chorus\n")
    assert parse_lyrics(path) == [(10.0, "chorus"), (62.25, "chorus")]


def test_offset_tag_shifts_every_line(tmp_path):
    path = _write(tmp_path, "[offset:-500]\n[00:10.00]late\n")
    assert parse_lyrics(path) == [(9.5, "late")]


def test_positive_offset_delays(tmp_path):
    path = _write(tmp_path, "[offset:+250]\n[00:10.00]early\n")
    assert parse_lyrics(path) == [(10.25, "early")]


def test_metadata_tags_are_ignored(tmp_path):
    path = _write(tmp_path, "[ar:Artist]\n[ti:Title]\n[by:Me]\n[00:01.00]line\n")
    assert parse_lyrics(path) == [(1.0, "line")]


def test_negative_times_are_clamped(tmp_path):
    path = _write(tmp_path, "[offset:-5000]\n[00:01.00]line\n")
    assert parse_lyrics(path) == [(0.0, "line")]


def test_lrc_section_headers_and_annotations_are_stripped(tmp_path):
    path = _write(tmp_path, "[00:01.00][Chorus] (x2) sing it\n")
    assert parse_lyrics(path) == [(1.0, "sing it")]


def test_plain_text_falls_back_and_drops_section_headers(tmp_path):
    path = _write(tmp_path, "[Verse 1]\nFirst line\n\nSecond line\n")
    assert parse_lyrics(path) == [(None, "First line"), (None, "Second line")]


def test_seconds_to_ass_carries_into_the_next_second():
    assert seconds_to_ass(5.995) == "0:00:06.00"
    assert seconds_to_ass(0.004) == "0:00:00.00"
    assert seconds_to_ass(3599.999) == "1:00:00.00"
