"""Editor round-trip keeps word timings, and old alignment assets are pruned."""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402
from processor import _retime_segment_words  # noqa: E402


def test_editor_segments_keep_word_timings_and_original_span():
    timed = [{
        "start": 1.0,
        "end": 2.5,
        "text": "one two",
        "words": [{"text": "one", "start": 1.0, "end": 1.2},
                  {"text": "two", "start": 1.4, "end": 2.0}],
    }]
    segment = app_module._segments_from_timed(timed)[0]
    assert segment["words"] == timed[0]["words"]
    assert segment["orig_start"] == 1.0
    assert segment["orig_end"] == 2.5


def test_edited_line_rescales_its_words():
    segment = {
        "start": 2.0,
        "end": 5.0,
        "orig_start": 1.0,
        "orig_end": 3.0,
        "text": "one two",
        "words": [{"text": "one", "start": 1.0, "end": 1.5},
                  {"text": "two", "start": 2.0, "end": 2.5}],
    }
    retimed = _retime_segment_words(segment)
    assert retimed["words"][0]["start"] == 2.0
    assert retimed["words"][1]["start"] == 3.5
    # The original segment is not mutated.
    assert segment["words"][0]["start"] == 1.0


def _make_old(path: Path, days: int = 8) -> None:
    stamp = time.time() - days * 24 * 3600
    os.utime(path, (stamp, stamp))


def test_old_alignment_assets_are_pruned_but_recent_ones_survive(tmp_path, monkeypatch):
    align_dir = tmp_path / "alignments"
    align_dir.mkdir()
    base_dir = tmp_path / "base"
    engine_dir = base_dir / "aligners" / "alignments"
    engine_dir.mkdir(parents=True)

    old_job = align_dir / "job_old"
    old_job.mkdir()
    (old_job / "audio.m4a").write_text("x", encoding="utf-8")
    old_json = align_dir / "job_old_alignment.json"
    old_json.write_text("{}", encoding="utf-8")
    new_job = align_dir / "job_new"
    new_job.mkdir()
    store = align_dir / "_jobs_store.json"
    store.write_text("{}", encoding="utf-8")
    engine_json = engine_dir / "old.json"
    engine_json.write_text("{}", encoding="utf-8")

    for path in (old_job, old_json, engine_json):
        _make_old(path)

    monkeypatch.setattr(app_module, "ALIGN_DIR", align_dir)
    monkeypatch.setattr(app_module, "BASE_DIR", base_dir)
    app_module._cleanup_old_alignments()

    assert not old_job.exists()
    assert not old_json.exists()
    assert not engine_json.exists()
    assert new_job.exists()
    assert store.exists()


def test_a_running_jobs_alignment_is_never_pruned(tmp_path, monkeypatch):
    align_dir = tmp_path / "alignments"
    align_dir.mkdir()
    running = align_dir / "job_running"
    running.mkdir()
    _make_old(running)

    monkeypatch.setattr(app_module, "ALIGN_DIR", align_dir)
    monkeypatch.setattr(app_module, "BASE_DIR", tmp_path / "base")
    monkeypatch.setitem(app_module._jobs, "job_running", {"status": "running"})
    try:
        app_module._cleanup_old_alignments()
        assert running.exists()
    finally:
        app_module._jobs.pop("job_running", None)
