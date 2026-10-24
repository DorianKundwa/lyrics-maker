"""Signals that a forced-aligned line cannot be right: silences and repeats."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import torch  # noqa: E402

from aligners.forced_align_mms import detect_silences, flag_fragments  # noqa: E402


def _fragment(begin, end, text, n_words=4):
    return {
        "begin": f"{begin:.3f}",
        "end": f"{end:.3f}",
        "lines": [text],
        "words": [{"text": "w", "start": begin, "end": begin + 0.2} for _ in range(n_words)],
    }


def test_detect_silences_finds_the_gap():
    sr = 16000
    tone = 0.5 * torch.sin(2 * torch.pi * 220 * torch.arange(sr * 6) / sr)
    gap = torch.zeros(sr * 2)
    wav = torch.cat([tone[: sr * 2], gap, tone[: sr * 2]]).unsqueeze(0)
    breaks = detect_silences(wav, sr, min_sec=1.2)
    assert breaks, "expected the 2 s gap to be detected"
    start, end = breaks[0]
    assert abs(start - 2.0) < 0.2
    assert abs(end - 4.0) < 0.2


def test_a_line_straddling_a_silence_is_flagged():
    frag = _fragment(1.0, 6.0, "held across a gap")
    n = flag_fragments([frag], [(2.0, 4.0)])
    assert n == 1
    assert frag["suspicious"] is True
    assert any("silence" in r for r in frag["reasons"])


def test_a_normal_line_is_not_flagged():
    frag = _fragment(1.0, 3.0, "a normal line")
    assert flag_fragments([frag], [(5.0, 7.0)]) == 0
    assert frag["suspicious"] is False


def test_impossible_pace_is_flagged():
    frag = _fragment(1.0, 20.0, "a line that lasts forever", n_words=4)
    assert flag_fragments([frag], []) == 1
    assert any("pace" in r for r in frag["reasons"])


def test_repeated_line_placed_twice_at_once_is_flagged():
    a = _fragment(10.0, 12.0, "la linea hay que cruzar")
    b = _fragment(10.2, 12.2, "la linea hay que cruzar")
    assert flag_fragments([a, b], []) == 2
    assert all("same line" in r for f in (a, b) for r in f["reasons"])


def test_a_genuine_repeat_later_in_the_song_is_not_flagged():
    a = _fragment(10.0, 12.0, "la linea hay que cruzar")
    b = _fragment(40.0, 42.0, "la linea hay que cruzar")
    assert flag_fragments([a, b], []) == 0
