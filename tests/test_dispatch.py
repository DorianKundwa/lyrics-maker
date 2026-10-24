"""Engine dispatch: forced alignment first, WhisperX when it cannot run."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aligners  # noqa: E402
import aligners.alignment_whisperx as whisperx_mod  # noqa: E402
import aligners.forced_align_mms as forced_mod  # noqa: E402

FRAGMENTS = '{"fragments": [{"begin": "1.000", "end": "2.000", "lines": ["a"], "words": []}]}'


def test_forced_alignment_is_used_when_it_works(monkeypatch, tmp_path):
    out = tmp_path / "forced.json"

    def fake_forced(audio, lyrics, output_json=None, language=None):
        Path(output_json).write_text(FRAGMENTS, encoding="utf-8")
        return str(output_json)

    def forbidden(*a, **k):
        raise AssertionError("WhisperX must not run when forced alignment succeeds")

    monkeypatch.setattr(forced_mod, "align_lyrics", fake_forced)
    monkeypatch.setattr(whisperx_mod, "align", forbidden)

    path = aligners.align("song.wav", "lyrics.txt", str(out))
    assert path == str(out)


def test_forced_failure_falls_back_to_whisperx(monkeypatch, tmp_path):
    out = tmp_path / "wx.json"

    def broken_forced(*a, **k):
        raise RuntimeError("torchaudio forced_align unavailable")

    def fake_whisperx(audio, lyrics, output_json=None, language=None):
        Path(output_json).write_text(FRAGMENTS, encoding="utf-8")
        return str(output_json)

    monkeypatch.setattr(forced_mod, "align_lyrics", broken_forced)
    monkeypatch.setattr(whisperx_mod, "align", fake_whisperx)

    path = aligners.align("song.wav", "lyrics.txt", str(out))
    assert path == str(out)
    assert Path(path).exists()
