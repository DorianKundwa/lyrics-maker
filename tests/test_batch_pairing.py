"""Batch pairing: each audio file gets the lyrics file with the same stem."""
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402


def _upload(name):
    return SimpleNamespace(filename=name)


def _pairs(audios, lyrics):
    pairs, problems = app.pair_batch_files(
        [_upload(name) for name in audios],
        [_upload(name) for name in lyrics],
    )
    return [(a.filename, l.filename) for a, l in pairs], problems


def test_matching_stems_pair_one_to_one():
    pairs, problems = _pairs(
        ["Song One.mp3", "Song Two.wav"],
        ["Song Two.txt", "Song One.lrc"],
    )
    assert problems == []
    assert pairs == [("Song One.mp3", "Song One.lrc"), ("Song Two.wav", "Song Two.txt")]


def test_matching_is_case_insensitive():
    pairs, problems = _pairs(["Midnight Rain.mp3"], ["MIDNIGHT RAIN.txt"])
    assert problems == []
    assert pairs == [("Midnight Rain.mp3", "MIDNIGHT RAIN.txt")]


def test_audio_without_lyrics_is_reported_and_not_paired():
    pairs, problems = _pairs(["Song.mp3"], [])
    assert pairs == []
    assert len(problems) == 1
    assert "Song.mp3" in problems[0]
    assert "song.txt" in problems[0]


def test_two_lyrics_for_one_song_are_ambiguous():
    pairs, problems = _pairs(["song.mp3"], ["song.txt", "song.lrc"])
    assert pairs == []
    assert len(problems) == 1
    assert "more than one lyrics file" in problems[0]
    assert "song.txt" in problems[0] and "song.lrc" in problems[0]


def test_two_audio_files_with_the_same_stem_are_rejected():
    pairs, problems = _pairs(["song.mp3", "song.wav"], ["song.txt"])
    assert pairs == [("song.mp3", "song.txt"), ("song.wav", "song.txt")]
    assert len(problems) == 1
    assert "share the name" in problems[0]


def test_lyrics_matching_no_audio_are_reported():
    pairs, problems = _pairs(["wanted.mp3"], ["wanted.txt", "unused.txt"])
    assert pairs == [("wanted.mp3", "wanted.txt")]
    assert len(problems) == 1
    assert "unused.txt" in problems[0]
    assert "matches no audio file" in problems[0]


def test_uploads_without_a_filename_are_ignored():
    pairs, problems = app.pair_batch_files([_upload(""), _upload("a.mp3")], [_upload("a.txt")])
    assert problems == []
    assert [(a.filename, l.filename) for a, l in pairs] == [("a.mp3", "a.txt")]
