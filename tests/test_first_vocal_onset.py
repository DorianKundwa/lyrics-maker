"""The first lyric line starts at the vocal onset, not at a late aligner guess."""
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aligners.audio_utils import detect_first_vocal_onset  # noqa: E402
from processor import _anchor_to_vocal_onset  # noqa: E402


def _write_wav(path: Path, samples: np.ndarray, sr: int = 16000) -> None:
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def test_onset_found_at_vocal_start(tmp_path):
    sr = 16000
    t = np.arange(int(sr * 8)) / sr
    tone = 0.5 * np.sin(2 * np.pi * 220 * t)
    stem = np.where(t >= 3.0, tone, 0.0)  # silent intro, vocals from 3.0 s
    wav = tmp_path / "vocals.wav"
    _write_wav(wav, stem, sr)
    onset = detect_first_vocal_onset(str(wav))
    assert onset is not None
    assert abs(onset - 3.0) < 0.1


def test_late_first_line_moves_back_to_onset():
    timed = [
        {"start": 5.6, "end": 8.0, "text": "Ay, ay-ay", "words": [
            {"text": "Ay", "start": 5.6, "end": 6.0},
        ]},
        {"start": 8.0, "end": 10.0, "text": "next", "words": []},
    ]
    out = _anchor_to_vocal_onset(timed, 3.0)
    assert abs(out[0]["start"] - 3.0) < 1e-9
    assert abs(out[0]["words"][0]["start"] - 3.0) < 1e-9
    assert abs(out[0]["words"][0]["end"] - 3.4) < 1e-9
    # The whole timeline moves by the same amount, so later lines stay in sync.
    assert abs(out[1]["start"] - 5.4) < 1e-9
    assert abs(out[1]["end"] - 7.4) < 1e-9


def test_early_or_close_line_is_left_alone():
    timed = [{"start": 3.2, "end": 5.0, "text": "a", "words": []}]
    assert _anchor_to_vocal_onset(timed, 3.0) == timed


def test_onset_far_before_first_line_is_not_trusted():
    # A breath or backing vocal early in the track can report an onset many
    # seconds before the singing; shifting the whole song back by that much
    # would destroy a correct alignment, so it is refused.
    timed = [{"start": 10.855, "end": 11.3, "text": "Ay", "words": []}]
    out = _anchor_to_vocal_onset(timed, 0.05)
    assert out == timed
    assert out[0]["start"] == 10.855


def test_shift_is_capped_at_max_shift():
    timed = [{"start": 6.0, "end": 8.0, "text": "a", "words": []}]
    assert _anchor_to_vocal_onset(timed, 1.0) == timed       # -5 s shift, refused
    assert _anchor_to_vocal_onset(timed, 3.5, max_shift=3.0) != timed  # -2.5 s, allowed
