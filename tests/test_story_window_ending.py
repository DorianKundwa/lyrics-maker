"""
The story window must reach the film's real ending.

Two ways the window used to fall short of the story: a dialogue-heavy tail was
classed as closing credits, and a story-graph beat past the window was ignored.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The credits detector and story-graph code live in a separate backend package
# that is not part of this project. Skip with a reason rather than fail at import.
pytest.importorskip(
    "backend.pipeline.analyzer.credits_detector",
    reason="backend package (credits detection / story graph) is not part of this project",
)

from backend.pipeline.analyzer.credits_detector import (  # noqa: E402
    detect_credits,
    get_story_boundaries,
    graph_scene_refs,
    reconcile_with_story_graph,
    story_end_evidence,
)

TOTAL = 7200.0


def _scene(index, start, end, dialogue="", characters=None, visual=""):
    return {
        "scene_index": index,
        "start": float(start),
        "end": float(end),
        "duration": float(end - start),
        "dialogue": dialogue,
        "on_screen_text": "",
        "visual_description": visual,
        "location": "lab",
        "characters": list(characters or []),
    }


def _story_then_tail(tail_scenes):
    """A film whose first 6000s are ordinary story, then the given tail scenes."""
    scenes = []
    for i in range(600):
        scenes.append(_scene(i, i * 10.0, i * 10.0 + 10.0, characters=["Crawford"],
                             visual="A researcher walks down a corridor"))
    start = 6000.0
    for j, (length, kwargs) in enumerate(tail_scenes):
        index = 600 + j
        scenes.append(_scene(index, start, start + length, **kwargs))
        start += length
    return scenes


def test_a_dialogue_tail_with_short_cuts_stays_in_the_story_window():
    # The last 1200s are 4s cuts of people talking. Dialogue says story: none of it may be
    # classed closing, and the window must run to the last talking cut.
    tail = []
    for k in range(300):
        tail.append((4.0, {
            "dialogue": "We have to get out of the building before the lights come back on",
            "characters": ["Crawford", "Phillips"],
            "visual": "Two people argue in a bright lab",
        }))
    enriched = detect_credits(_story_then_tail(tail), total_movie_duration=TOTAL)

    tail_scenes = enriched[600:]
    assert tail_scenes and not any(s["is_credit"] for s in tail_scenes)
    _, story_end = get_story_boundaries(enriched, TOTAL)
    assert story_end == tail_scenes[-1]["end"]


def test_fixed_length_dialogue_free_tail_is_the_end_credits():
    # Eighteen-second cards with no cast and no dialogue at the very end are the credits.
    tail = [(18.0, {"dialogue": "", "characters": [], "visual": ""}) for _ in range(67)]
    enriched = detect_credits(_story_then_tail(tail), total_movie_duration=TOTAL)

    assert all(s["is_credit"] for s in enriched[600:])
    _, story_end = get_story_boundaries(enriched, TOTAL)
    assert story_end == 6000.0


def test_a_story_graph_beat_past_the_window_extends_it_and_flips_the_evidence():
    tail = [(18.0, {"dialogue": "", "characters": [], "visual": ""}) for _ in range(67)]
    scenes = detect_credits(_story_then_tail(tail), total_movie_duration=TOTAL)
    # The climax is scene 620: a dialogue-free 18s card with no credit text, in the tail.
    graph = {"climax": {"scene_index": 620}, "plot_beats": [{"scene": 620}]}
    assert 620 in graph_scene_refs(graph)

    before = story_end_evidence(
        scenes, *get_story_boundaries(scenes, TOTAL), TOTAL,
        beat_scene_refs=graph_scene_refs(graph),
    )
    assert before["ending_inside_story_range"] is False

    readmitted = reconcile_with_story_graph(scenes, graph)
    assert 620 in readmitted

    story_start, story_end = get_story_boundaries(scenes, TOTAL)
    scene_620 = next(s for s in scenes if s["scene_index"] == 620)
    assert story_end >= scene_620["end"]

    after = story_end_evidence(
        scenes, story_start, story_end, TOTAL, beat_scene_refs=graph_scene_refs(graph),
    )
    assert after["ending_inside_story_range"] is True
