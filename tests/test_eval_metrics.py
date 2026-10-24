"""The alignment metric: share of word onsets within tolerance, plus mean error."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.eval_alignment import score_words, summarise  # noqa: E402


def _gt(*onsets):
    return [(o, o + 0.4) for o in onsets]


def test_perfect_alignment_scores_full_marks():
    gt = _gt(1.0, 2.0, 3.0)
    m = score_words([[1.0, 1.4], [2.0, 2.4], [3.0, 3.4]], gt)
    assert m["pco"] == 1.0
    assert m["mae"] == 0.0
    assert m["n"] == 3


def test_one_second_late_fails_the_tolerance():
    gt = _gt(1.0, 2.0, 3.0)
    m = score_words([[2.0, 2.4], [3.0, 3.4], [4.0, 4.4]], gt, tol=0.3)
    assert m["pco"] == 0.0
    assert abs(m["mae"] - 1.0) < 1e-9


def test_tolerance_boundary_is_inclusive():
    gt = _gt(1.0)
    assert score_words([[1.3, 1.7]], gt, tol=0.3)["pco"] == 1.0
    assert score_words([[1.31, 1.7]], gt, tol=0.3)["pco"] == 0.0


def test_unpredicted_words_count_as_misses_in_coverage():
    gt = _gt(1.0, 2.0, 3.0, 4.0)
    m = score_words([[1.0, 1.4], None, [3.0, 3.4], None], gt)
    assert m["n"] == 2
    assert m["coverage"] == 0.5
    assert m["pco"] == 1.0


def test_summary_is_weighted_by_word_count():
    rows = [
        {"n": 90, "pco": 1.0, "mae": 0.1},
        {"n": 10, "pco": 0.0, "mae": 1.0},
    ]
    s = summarise(rows)
    assert abs(s["pco"] - 0.9) < 1e-9
    assert abs(s["mae"] - 0.19) < 1e-9
