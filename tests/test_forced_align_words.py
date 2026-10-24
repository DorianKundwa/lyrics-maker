"""The forced-alignment engine: text prep, gap filling and the shared schema."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import aligners.forced_align_mms as fa  # noqa: E402


def test_normalize_word_strips_accents_and_punctuation():
    assert fa.normalize_word("Cógelo") == "cogelo"
    assert fa.normalize_word("¡Breyco!") == "breyco"
    assert fa.normalize_word("pa'entro") == "pa'entro"
    assert fa.normalize_word("Tú") == "tu"


def test_digits_and_symbols_normalize_away():
    assert fa.normalize_word("47") == ""
    assert fa.normalize_word("...") == ""


def test_read_lyric_lines_drops_blank_lines(tmp_path):
    p = tmp_path / "l.txt"
    p.write_text("Ay, ay-ay\n\n  Cógelo ahí  \n\n", encoding="utf-8")
    assert fa.read_lyric_lines(p) == ["Ay, ay-ay", "Cógelo ahí"]


def test_fill_missing_brackets_known_words():
    times = [None, [1.0, 1.4], None, [3.0, 3.4], None]
    out = fa._fill_missing(times, 0.5, 4.0)
    assert len(out) == 5
    assert out[1] == [1.0, 1.4]
    assert out[3] == [3.0, 3.4]
    # Unknown words sit between their neighbours, in order.
    assert 1.4 <= out[2][0] <= 3.0
    assert out[0][1] <= out[1][0] + 1e-6
    assert out[4][0] >= out[3][1] - 1e-6
    for prev, nxt in zip(out, out[1:]):
        assert nxt[0] >= prev[1] - 1e-6


def test_fill_missing_handles_line_with_no_tokens():
    out = fa._fill_missing([None, None, None], 2.0, 5.0)
    assert out[0][0] == 2.0
    assert out[-1][1] > out[0][1]
    for prev, nxt in zip(out, out[1:]):
        assert nxt[0] >= prev[0]


def test_align_words_emits_the_shared_schema(monkeypatch):
    lines = ["Ay, ay-ay", "Cógelo ahí"]
    fake = (
        [["Ay,", "ay-ay"], ["Cógelo", "ahí"]],
        [[0.5, 0.9], [1.0, 1.4], [2.0, 2.4], [2.5, 2.9]],
        [0.9, 0.8, 0.2, 0.1],
        10.0,
        [],
    )
    monkeypatch.setattr(fa, "_align_all", lambda audio, l: fake)

    frags = fa.align_words("ignored.wav", lines)

    assert [f["lines"][0] for f in frags] == lines
    for f in frags:
        assert set(f) >= {"begin", "end", "lines", "words", "confidence"}
        assert float(f["begin"]) <= float(f["end"])
        starts = [w["start"] for w in f["words"]]
        assert starts == sorted(starts)
    assert frags[0]["suspicious"] is False
    assert frags[1]["suspicious"] is False
    assert frags[0]["confidence"] > frags[1]["confidence"]
