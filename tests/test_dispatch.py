"""Engine dispatch: WhisperX by default, forced/combo when selected or as fallback."""
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aligners  # noqa: E402
import aligners.alignment_combo  # noqa: E402,F401
import aligners.alignment_whisperx as whisperx_mod  # noqa: E402
import aligners.forced_align_mms as forced_mod  # noqa: E402

FRAGMENTS = '{"fragments": [{"begin": "1.000", "end": "2.000", "lines": ["a"], "words": []}]}'


def _writer(path):
    def fake(audio, lyrics, output_json=None, language=None, **kwargs):
        Path(output_json).write_text(FRAGMENTS, encoding="utf-8")
        return str(output_json)

    return fake


def _forbidden(label):
    def refuse(*a, **k):
        raise AssertionError(f"{label} must not run")

    return refuse


def test_whisperx_is_the_default_engine(monkeypatch, tmp_path):
    monkeypatch.delenv("LYRIC_ALIGN_ENGINE", raising=False)
    monkeypatch.setitem(sys.modules, "whisperx", types.ModuleType("whisperx"))
    out = tmp_path / "whisperx.json"
    monkeypatch.setattr(whisperx_mod, "align", _writer(out))
    monkeypatch.setattr(aligners.alignment_combo, "align_lyrics", _forbidden("combo"))
    monkeypatch.setattr(forced_mod, "align_lyrics", _forbidden("forced"))

    path = aligners.align("song.wav", "lyrics.txt", str(out))
    assert path == str(out)


def test_forced_engine_starts_with_forced_alignment(monkeypatch, tmp_path):
    monkeypatch.setenv("LYRIC_ALIGN_ENGINE", "forced")
    out = tmp_path / "forced.json"
    monkeypatch.setattr(forced_mod, "align_lyrics", _writer(out))
    monkeypatch.setattr(whisperx_mod, "align", _forbidden("WhisperX"))
    monkeypatch.setattr(aligners.alignment_combo, "align_lyrics", _forbidden("combo"))

    path = aligners.align("song.wav", "lyrics.txt", str(out))
    assert path == str(out)


def test_combo_engine_starts_with_the_forced_repair(monkeypatch, tmp_path):
    monkeypatch.setenv("LYRIC_ALIGN_ENGINE", "combo")
    out = tmp_path / "combo.json"
    monkeypatch.setattr(aligners.alignment_combo, "align_lyrics", _writer(out))
    monkeypatch.setattr(whisperx_mod, "align", _forbidden("WhisperX"))

    path = aligners.align("song.wav", "lyrics.txt", str(out))
    assert path == str(out)


def test_whisperx_failure_falls_back_to_combo(monkeypatch, tmp_path):
    monkeypatch.delenv("LYRIC_ALIGN_ENGINE", raising=False)
    monkeypatch.setitem(sys.modules, "whisperx", types.ModuleType("whisperx"))
    out = tmp_path / "fallback.json"

    def broken_whisperx(*a, **k):
        raise RuntimeError("whisperx unavailable")

    monkeypatch.setattr(whisperx_mod, "align", broken_whisperx)
    monkeypatch.setattr(aligners.alignment_combo, "align_lyrics", _writer(out))
    monkeypatch.setattr(forced_mod, "align_lyrics", _forbidden("forced"))

    path = aligners.align("song.wav", "lyrics.txt", str(out))
    assert path == str(out)
