"""Elisions, hyphenated repeats and near-spellings still match the transcript."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aligners.alignment_whisperx import (  # noqa: E402
    COST_MATCH,
    COST_MISMATCH,
    COST_PARTIAL,
    _dp_match,
    _norm_word,
    _token_similarity,
    _tokenize,
)


def test_hyphenated_repeats_are_split():
    toks = _tokenize("Ay, ay-ay, ja")
    assert [_norm_word(t) for t in toks] == ["ay", "ay", "ay", "ja"]


def test_apostrophe_elision_is_split():
    assert [_norm_word(t) for t in _tokenize("pa'entro")] == ["pa", "entro"]
    # A one-letter tail stays whole: the transcript hears one word.
    assert [_norm_word(t) for t in _tokenize("concentra'o")] == ["concentrao"]


def test_plain_words_are_left_alone():
    assert _tokenize("Dímelo, Bulin, que yo")[:3] == ["Dímelo,", "Bulin,", "que"]


def test_similarity_scores():
    assert _token_similarity("ay", "ay") == COST_MATCH
    assert _token_similarity("dime", "dimelo") == COST_PARTIAL      # prefix
    assert _token_similarity("ermanito", "hermanito") == COST_PARTIAL  # truncation
    assert _token_similarity("casa", "perro") == COST_MISMATCH


def test_dp_matches_near_spellings_instead_of_dropping_them():
    user = _tokenize("Dímelo, Bulin, que yo vine concentra'o")
    asr = [{"word": w} for w in
           ["dimelo", "bulin", "que", "yo", "vine", "concentrao"]]
    mapping = _dp_match(user, asr)
    assert len(mapping) == len(user)
    assert mapping[len(user) - 1] == len(asr) - 1


def test_dp_matches_split_elision_against_two_transcript_words():
    user = _tokenize("voy pa'entro")
    asr = [{"word": w} for w in ["voy", "pa", "entro"]]
    assert len(_dp_match(user, asr)) == 3
