"""Job slots, the split stem/alignment gates, and the queue-position payload."""
import asyncio
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import concurrency  # noqa: E402
import processor  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402


def _hold(kind, held, release, entered):
    """Acquire one stage slot, signal `entered` once inside, then wait."""
    stage = concurrency.stem_stage if kind == "stem" else concurrency.align_stage
    with stage():
        entered.set()
        release.wait(5)


def test_align_does_not_wait_for_a_stem_slot(monkeypatch):
    """One song can align while another separates stems."""
    monkeypatch.setenv("LYRIC_STEM_CONCURRENCY", "1")
    monkeypatch.setenv("LYRIC_ALIGN_CONCURRENCY", "1")
    concurrency._reset_slots()
    try:
        stem_in, release = threading.Event(), threading.Event()
        align_in = threading.Event()
        stem_thread = threading.Thread(target=_hold, args=("stem", stem_in, release, stem_in))
        stem_thread.start()
        assert stem_in.wait(5), "stem slot was never acquired"

        align_thread = threading.Thread(target=_hold, args=("align", align_in, release, align_in))
        align_thread.start()
        assert align_in.wait(2), "alignment must not queue behind the stem gate"

        release.set()
        stem_thread.join(timeout=5)
        align_thread.join(timeout=5)
    finally:
        concurrency._reset_slots()


def test_stem_slot_is_exclusive(monkeypatch):
    monkeypatch.setenv("LYRIC_STEM_CONCURRENCY", "1")
    monkeypatch.delenv("LYRIC_HEAVY_CONCURRENCY", raising=False)
    concurrency._reset_slots()
    try:
        first_in, release = threading.Event(), threading.Event()
        second_in = threading.Event()
        t1 = threading.Thread(target=_hold, args=("stem", first_in, release, first_in))
        t1.start()
        assert first_in.wait(5)

        t2 = threading.Thread(target=_hold, args=("stem", second_in, release, second_in))
        t2.start()
        assert not second_in.wait(0.3), "a single stem slot must not admit two separations"

        release.set()
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert second_in.is_set(), "the waiting stage never got the slot back"
    finally:
        concurrency._reset_slots()


def test_old_heavy_env_name_still_sets_the_stem_slot(monkeypatch):
    monkeypatch.delenv("LYRIC_STEM_CONCURRENCY", raising=False)
    monkeypatch.setenv("LYRIC_HEAVY_CONCURRENCY", "2")
    assert concurrency.limit("stem") == 2


def test_job_slots_default_to_three_and_clamp(monkeypatch):
    monkeypatch.delenv("LYRIC_JOB_CONCURRENCY", raising=False)
    assert app._job_concurrency() == 3

    monkeypatch.setenv("LYRIC_JOB_CONCURRENCY", "1")
    assert app._job_concurrency() == 1

    monkeypatch.setenv("LYRIC_JOB_CONCURRENCY", "9")
    assert app._job_concurrency() == 4

    monkeypatch.setenv("LYRIC_JOB_CONCURRENCY", "many")
    assert app._job_concurrency() == 3


def test_semaphore_is_built_with_the_configured_slot_count(monkeypatch):
    async def scenario():
        monkeypatch.setenv("LYRIC_JOB_CONCURRENCY", "2")
        monkeypatch.setattr(app, "_semaphore", None)
        return app._get_semaphore()._value

    assert asyncio.run(scenario()) == 2


def test_ffmpeg_threads_are_divided_across_jobs(monkeypatch):
    monkeypatch.setenv("LYRIC_JOB_CONCURRENCY", "3")
    args = processor._video_encode_args()
    assert "-threads" in args
    threads = int(args[args.index("-threads") + 1])
    assert 1 <= threads <= (processor.os.cpu_count() or 4)


def _fake_render_stages(monkeypatch, state, lock, delay=0.2):
    """Replace every heavy stage with a sleep that records overlap."""
    import time

    def enter():
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
            for job in app._jobs.values():
                if isinstance(job, dict) and job.get("slot"):
                    state.setdefault("slots", set()).add(job["slot"])
        time.sleep(delay)
        with lock:
            state["active"] -= 1

    def fake_stem(audio, engine, out_dir, warnings):
        enter()
        return str(Path(out_dir) / "vocals.wav"), str(Path(out_dir) / "inst.wav")

    def fake_thumb(*args, **kwargs):
        enter()

    def fake_instrumental(*args, **kwargs):
        enter()

    def fake_lyrics(*args, **kwargs):
        enter()
        return []

    import stem_separator

    monkeypatch.setattr(stem_separator, "separate_audio", fake_stem)
    monkeypatch.setattr(app.proc, "generate_thumbnail", fake_thumb)
    monkeypatch.setattr(app.proc, "generate_instrumental_video", fake_instrumental)
    monkeypatch.setattr(app.proc, "generate_lyrics_video", fake_lyrics)
    monkeypatch.setattr(app, "_persist_jobs", lambda: None)
    monkeypatch.setattr(app, "_save_jobs_store", lambda: None)
    monkeypatch.setattr(app, "_save_alignment_for_editor", lambda *a, **k: None)


def test_three_jobs_render_at_once_and_the_fourth_waits(monkeypatch, tmp_path):
    monkeypatch.setenv("LYRIC_JOB_CONCURRENCY", "3")
    monkeypatch.setenv("LYRIC_STEM_CONCURRENCY", "3")
    monkeypatch.setenv("LYRIC_ALIGN_CONCURRENCY", "3")
    monkeypatch.setattr(app, "_semaphore", None)
    concurrency._reset_slots()
    try:
        state = {"active": 0, "max": 0}
        lock = threading.Lock()
        _fake_render_stages(monkeypatch, state, lock)

        jobs = {}
        for i in range(4):
            jid = f"slot-test-{i}"
            (tmp_path / jid).mkdir()
            jobs[jid] = {
                "status": "queued", "step": "Queued", "progress": 0,
                "params": {}, "files": {}, "queued_at": 1.0, "attempts": 1,
            }
        monkeypatch.setattr(app, "_jobs", jobs)

        async def run_all():
            tasks = [
                asyncio.create_task(app._run_job(
                    jid,
                    str(tmp_path / f"{jid}.mp3"),
                    str(tmp_path / f"{jid}.txt"),
                    str(tmp_path / f"{jid}.jpg"),
                    None,
                    tmp_path / jid,
                    "Song",
                    "Artist",
                ))
                for jid in jobs
            ]
            await asyncio.gather(*tasks)

        asyncio.run(run_all())

        assert state["max"] == 3, f"expected 3 concurrent songs, saw {state['max']}"
        assert state.get("slots") == {1, 2, 3}, f"slot ids seen: {state.get('slots')}"
        assert all(job["status"] == "complete" for job in jobs.values())
        # Slots go back to the pool, so a finished job never keeps a slot number.
        assert all("slot" not in job for job in jobs.values())
    finally:
        concurrency._reset_slots()


def test_slot_ids_are_returned_to_the_pool(monkeypatch):
    async def scenario():
        monkeypatch.setenv("LYRIC_JOB_CONCURRENCY", "3")
        monkeypatch.setattr(app, "_slot_ids", None)
        pool = app._get_slot_ids()
        taken = [pool.get_nowait() for _ in range(3)]
        drained = pool.empty()
        pool.put_nowait(taken[0])
        return sorted(taken), drained, pool.get_nowait()

    assert asyncio.run(scenario()) == ([1, 2, 3], True, 1)


def test_jobs_payload_exposes_slot_and_timestamps(monkeypatch):
    jobs = {
        "live": {
            "status": "running", "slot": 2, "progress": 40,
            "started_at": 100.0, "queued_at": 90.0,
        },
        "wait": {"status": "queued", "queued_at": 95.0},
    }
    monkeypatch.setattr(app, "_jobs", jobs)

    data = TestClient(app.app).get("/api/jobs").json()

    assert data["jobs"]["live"]["slot"] == 2
    assert data["jobs"]["live"]["started_at"] == 100.0
    assert data["jobs"]["wait"]["queued_at"] == 95.0
    assert data["jobs"]["wait"]["queue_position"] == 1


def test_jobs_endpoint_reports_slots_and_queue_positions(monkeypatch):
    jobs = {
        "later":  {"status": "queued", "queued_at": 200.0},
        "first":  {"status": "queued", "queued_at": 100.0},
        "active": {"status": "running", "queued_at": 50.0, "progress": 40},
    }
    monkeypatch.setattr(app, "_jobs", jobs)
    monkeypatch.delenv("LYRIC_JOB_CONCURRENCY", raising=False)

    data = TestClient(app.app).get("/api/jobs").json()

    assert data["slots"] == {"limit": 3, "running": 1, "queued": 2}
    assert data["jobs"]["first"]["queue_position"] == 1
    assert data["jobs"]["later"]["queue_position"] == 2
    assert "queue_position" not in data["jobs"]["active"]
