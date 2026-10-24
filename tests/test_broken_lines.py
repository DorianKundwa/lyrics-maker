"""Broken-sentence guards: contractions, tail removal and the final line gate.

A clause-final auxiliary is never contracted, "have to" is never contracted, a tail removal
that leaves a dangling word is refused, and the final gate drops a broken line and lists it.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# These guards live in a separate backend package that is not part of this
# project, so the checks cannot run here. Skipping keeps the suite green and
# reports why instead of failing at import.
pytest.importorskip(
    "backend.pipeline.ai.script_critic",
    reason="backend package (script critic / caption echo) is not part of this project",
)
pytest.importorskip(
    "backend.utils.caption_echo",
    reason="backend package (script critic / caption echo) is not part of this project",
)

from backend.pipeline.ai.script_critic import (  # noqa: E402
    _contract_stiff_forms,
    _drop_broken_lines,
)
from backend.utils.caption_echo import (  # noqa: E402
    broken_line_reason,
    dangling_ending,
    repair_caption_tails,
)


@pytest.mark.parametrize("line", [
    "It stays exactly where it is.",
    "Say what they are.",
    "That is whose it is.",
    "We have to go.",
    "They have to move the crates.",
])
def test_clause_final_auxiliaries_and_have_to_stay_uncontracted(line):
    assert _contract_stiff_forms(line)[0] == line


def test_a_subject_auxiliary_mid_clause_is_still_contracted():
    assert _contract_stiff_forms("He is tired.")[0] == "He's tired."


def test_tail_removal_keeps_the_place_phrase_when_it_would_end_on_a_dangling_word():
    line = "Nobody answers, because the answer is already in the next room."
    result, removed = repair_caption_tails(line, "a woman waits in the next room", ())
    assert result == line
    assert removed == 0


def test_a_tail_that_ends_on_still_is_a_dangling_ending():
    assert dangling_ending("like a man trying to prove he is still.")
    assert not dangling_ending("like a man trying to prove he is alive.")


def test_a_negation_only_ending_is_dangling():
    assert dangling_ending("Nothing about it isn't,")


def test_the_final_gate_drops_a_dangling_line_and_lists_it():
    sections = [{"section_id": 4, "narration": "Nobody answers. He is still."}]
    dropped = _drop_broken_lines(sections)
    assert sections[0]["narration"] == "Nobody answers."
    assert len(dropped) == 1
    assert dropped[0]["section_id"] == 4
    assert dropped[0]["sentence"] == "He is still."


def test_a_deliberate_short_beat_with_a_finite_verb_is_kept():
    assert broken_line_reason("It isn't over.") is None
    sections = [{"section_id": 5, "narration": "He doesn't answer her. It isn't over."}]
    assert _drop_broken_lines(sections) == []
    assert sections[0]["narration"] == "He doesn't answer her. It isn't over."


def test_a_verbless_fragment_is_dropped():
    assert broken_line_reason("Not him.") == "fragment of fewer than four words with no verb"
