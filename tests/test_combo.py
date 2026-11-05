"""Forced alignment with Whisper repair: only flagged lines are re-checked."""
import json
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aligners  # noqa: E402
import aligners.alignment_combo as combo  # noqa: E402


def _fragment(begin, end, text, suspicious=False):
    words = text.split()
    step = (end - begin) / max(1, len(words))
    return {
        "begin": f"{begin:.3f}",
        "end": f"{end:.3f}",
        "lines": [text],
        "words": [
            {"text": word, "start": begin + i * step, "end": begin + (i + 1) * step}
            for i, word in enumerate(words)
        ],
        "confidence": 0.9,
        "suspicious": suspicious,
        "reasons": ["implausible pace for the line"] if suspicious else [],
    }


def _lyrics(tmp_path, text):
    path = tmp_path / "lyrics.txt"
    path.write_text(text, encoding="utf-8")
    return path


def _asr(words, start=2.0, step=0.5):
    return [
        {"word": word, "start": start + i * step, "end": start + i * step + 0.4}
        for i, word in enumerate(words)
    ]


def test_a_flagged_line_is_replaced_by_transcript_times(tmp_path):
    lines = "one two three\nfour five\n"
    fragments = [
        _fragment(1.0, 20.0, "one two three", suspicious=True),
        _fragment(30.0, 32.0, "four five"),
    ]
    out = tmp_path / "alignment.json"

    combo.align_lyrics(
        "ignored.wav",
        str(_lyrics(tmp_path, lines)),
        str(out),
        forced_fn=lambda: fragments,
        transcriber=lambda lo, hi: _asr(["one", "two", "three"]),
        duration=40.0,
    )

    data = json.loads(out.read_text(encoding="utf-8"))
    first = data["fragments"][0]
    assert data["engine"] == "forced_whisper_combo"
    assert data["whisper_repairs"] == 1
    assert [word["text"] for word in first["words"]] == ["one", "two", "three"]
    assert abs(first["words"][0]["start"] - 2.0) < 1e-6
    assert float(first["begin"]) == 2.0
    assert first["suspicious"] is False
    assert first["repaired_by"] == "whisper"
    # The clean line must not have been touched.
    assert [word["start"] for word in data["fragments"][1]["words"]] == [30.0, 31.0]


def test_clean_lines_never_call_whisper(tmp_path):
    fragments = [_fragment(1.0, 3.0, "one two")]

    def forbidden(lo, hi):
        raise AssertionError("Whisper must not run when no line is flagged")

    out = tmp_path / "alignment.json"
    combo.align_lyrics(
        "ignored.wav",
        str(_lyrics(tmp_path, "one two\n")),
        str(out),
        forced_fn=lambda: fragments,
        transcriber=forbidden,
        duration=10.0,
    )
    assert json.loads(out.read_text(encoding="utf-8"))["whisper_repairs"] == 0


def test_a_transcript_that_does_not_match_is_rejected(tmp_path):
    fragments = [_fragment(1.0, 20.0, "one two three", suspicious=True)]
    out = tmp_path / "alignment.json"

    combo.align_lyrics(
        "ignored.wav",
        str(_lyrics(tmp_path, "one two three\n")),
        str(out),
        forced_fn=lambda: fragments,
        transcriber=lambda lo, hi: _asr(["zzz", "qqq", "xxx"]),
        duration=25.0,
    )

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["whisper_repairs"] == 0
    assert data["fragments"][0]["words"][0]["start"] == 1.0
    assert data["fragments"][0]["suspicious"] is True


def test_a_broken_transcriber_keeps_the_forced_result(tmp_path):
    fragments = [_fragment(1.0, 20.0, "one two three", suspicious=True)]

    def broken(lo, hi):
        raise RuntimeError("model not available")

    out = tmp_path / "alignment.json"
    combo.align_lyrics(
        "ignored.wav",
        str(_lyrics(tmp_path, "one two three\n")),
        str(out),
        forced_fn=lambda: fragments,
        transcriber=broken,
        duration=25.0,
    )

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["whisper_repairs"] == 0
    assert float(data["fragments"][0]["begin"]) == 1.0


def test_repairs_stay_monotonic_across_lines(tmp_path):
    fragments = [
        _fragment(1.0, 3.0, "one two", suspicious=True),
        _fragment(3.2, 4.4, "three four", suspicious=True),
    ]

    def transcribe(lo, hi):
        # Both transcripts start at 2.9 s, so the second repair overlaps the
        # first and has to be pushed back into order.
        text = "one two" if hi <= 4.0 else "three four"
        return _asr(text.split(), start=2.9, step=0.05)

    repaired = combo.repair_fragments(fragments, transcribe, duration=10.0)
    assert repaired == 2

    first_end = fragments[0]["words"][-1]["end"]
    second_start = fragments[1]["words"][0]["start"]
    assert second_start >= first_end - 1e-9
    assert second_start > 2.9  # it was clamped forward, not left overlapping


def _write_wav(path: Path, seconds: float = 2.0, sr: int = 16000) -> None:
    t = np.arange(int(sr * seconds)) / sr
    pcm = (0.4 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sr)
        handle.writeframes(pcm.tobytes())


def test_the_real_transcriber_maps_window_times_to_the_song(tmp_path, monkeypatch):
    wav = tmp_path / "vocals.wav"
    _write_wav(wav)
    seen = {}

    class _Word:
        word, start, end = "hello", 0.5, 0.8

    class _Segment:
        words = [_Word()]

    class _Model:
        def transcribe(self, audio, **kwargs):
            seen["samples"] = len(audio)
            seen["dtype"] = audio.dtype
            return [_Segment()], None

    monkeypatch.setattr(combo, "_whisper_model", lambda: _Model())
    transcribe = combo._build_transcriber(str(wav), None)
    words = transcribe(1.0, 1.5)

    assert seen["dtype"] == np.float32
    assert abs(seen["samples"] - 8000) <= 1
    assert words == [{"word": "hello", "start": 1.5, "end": 1.8}]


def test_repair_language_comes_from_the_lyrics(tmp_path, monkeypatch):
    lines = "tengo cuatro mil amigos soy un tio super popular\n"
    seen = {}

    def fake_build(audio, language):
        seen["language"] = language
        return lambda lo, hi: []

    monkeypatch.setattr(combo, "_build_transcriber", fake_build)
    out = tmp_path / "alignment.json"
    combo.align_lyrics(
        "ignored.wav",
        str(_lyrics(tmp_path, lines)),
        str(out),
        forced_fn=lambda: [_fragment(1.0, 20.0, lines.strip(), suspicious=True)],
        duration=30.0,
    )
    assert seen["language"] == "es"


def test_language_vote_ignores_a_misdetected_intro():
    responses = [("en", 0.41), ("es", 0.44), ("es", 0.63),
                 ("km", 0.48), ("km", 0.28), ("es", 0.68)]

    class _Model:
        def __init__(self):
            self.index = 0

        def detect_language(self, audio):
            language, probability = responses[self.index]
            self.index += 1
            return language, probability, []

    samples = np.zeros(16000 * 180, dtype=np.float32)
    assert combo._detect_repair_language(_Model(), samples, 16000) == "es"


def test_language_vote_without_a_majority_is_refused():
    responses = [("es", 0.6), ("es", 0.6), ("en", 0.6),
                 ("en", 0.6), ("km", 0.6), ("km", 0.6)]

    class _Model:
        def __init__(self):
            self.index = 0

        def detect_language(self, audio):
            language, probability = responses[self.index]
            self.index += 1
            return language, probability, []

    samples = np.zeros(16000 * 180, dtype=np.float32)
    assert combo._detect_repair_language(_Model(), samples, 16000) is None


def test_dispatch_prefers_the_combo_engine(monkeypatch, tmp_path):
    out = tmp_path / "combo.json"

    def fake_combo(audio, lyrics, output_json=None, language=None):
        Path(output_json).write_text('{"fragments": []}', encoding="utf-8")
        return str(output_json)

    monkeypatch.setenv("LYRIC_ALIGN_ENGINE", "combo")
    monkeypatch.setattr(combo, "align_lyrics", fake_combo)
    assert aligners.align("song.wav", "lyrics.txt", str(out)) == str(out)
