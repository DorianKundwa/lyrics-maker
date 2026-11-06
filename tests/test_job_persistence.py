"""Persistent job records and resuming failed or interrupted jobs."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
from starlette.background import BackgroundTasks  # noqa: E402


def _make_job(tmp_path, *, status="error", resumable=True):
    upload_dir = tmp_path / "uploads" / "job1"
    upload_dir.mkdir(parents=True)
    audio = upload_dir / "audio.mp3"
    lyrics = upload_dir / "lyrics.txt"
    background = upload_dir / "background.jpg"
    audio.write_bytes(b"audio")
    lyrics.write_text("one two", encoding="utf-8")
    background.write_bytes(b"image")

    out_dir = tmp_path / "outputs" / "job1"
    out_dir.mkdir(parents=True)

    return {
        "status": status,
        "step": "",
        "progress": 0,
        "error": None,
        "attempts": 1,
        "resumable": resumable,
        "params": {
            "audio_path": str(audio),
            "lyrics_path": str(lyrics),
            "bg_path": str(background),
            "outro_path": None,
            "out_dir": str(out_dir),
            "title": "Song",
            "artist": "Artist",
            "font_name": "Arial",
            "font_size": 72,
            "stem_engine": "demucs",
            "word_highlight": True,
            "language": "en",
            "active_color": "#FFFFFF",
            "upcoming_color": "#FF0000",
            "sung_color": "#FFFFFF",
            "lyric_style": "classic",
            "outro_text": "Thanks for watching!",
        },
    }


def test_restart_marks_running_job_interrupted_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "JOBS_STORE", tmp_path / "jobs.json")
    monkeypatch.setattr(app, "_jobs", {})

    job = _make_job(tmp_path, status="running", resumable=False)
    app.JOBS_STORE.write_text(json.dumps({"job1": job}), encoding="utf-8")

    app._load_jobs_store()

    loaded = app._jobs["job1"]
    assert loaded["status"] == "error"
    assert loaded["step"] == "Interrupted"
    assert loaded["error_code"] == "interrupted"
    assert loaded["resumable"] is True

    persisted = json.loads(app.JOBS_STORE.read_text(encoding="utf-8"))
    assert persisted["job1"]["status"] == "error"
    assert persisted["job1"]["resumable"] is True


def test_resume_rejects_missing_inputs_and_clears_resumable(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "JOBS_STORE", tmp_path / "jobs.json")
    job = _make_job(tmp_path)
    monkeypatch.setattr(app, "_jobs", {"job1": job})

    Path(job["params"]["audio_path"]).unlink()

    response = app.resume_job("job1", BackgroundTasks())

    assert response.status_code == 410
    assert job["resumable"] is False


def test_resume_requeues_job_with_saved_params(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "JOBS_STORE", tmp_path / "jobs.json")
    job = _make_job(tmp_path)
    monkeypatch.setattr(app, "_jobs", {"job1": job})

    background_tasks = BackgroundTasks()
    response = app.resume_job("job1", background_tasks)

    assert response["status"] == "queued"
    assert job["status"] == "queued"
    assert job["attempts"] == 2
    assert job["resumable"] is False

    task = background_tasks.tasks[0]
    assert task.func is app._run_job
    assert task.args == ("job1",)
    assert task.kwargs["resume"] is True
    assert task.kwargs["audio_path"] == job["params"]["audio_path"]


def test_completed_job_cannot_be_resumed(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "JOBS_STORE", tmp_path / "jobs.json")
    job = _make_job(tmp_path, status="complete", resumable=False)
    monkeypatch.setattr(app, "_jobs", {"job1": job})

    response = app.resume_job("job1", BackgroundTasks())

    assert response.status_code == 409


def test_download_all_bundles_files_into_zip(tmp_path, monkeypatch):
    out_dir = tmp_path / "outputs" / "job1"
    out_dir.mkdir(parents=True)
    video = out_dir / "lyrics_video.mp4"
    video.write_bytes(b"fake_video")
    thumb = out_dir / "thumbnail.jpg"
    thumb.write_bytes(b"fake_thumb")

    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path / "outputs")
    monkeypatch.setattr(app, "_jobs", {
        "job1": {
            "status": "complete",
            "title": "My Song",
            "artist": "My Artist",
            "files": {
                "lyrics_video": str(video),
                "thumbnail": str(thumb),
            },
        }
    })

    response = app.download_all("job1")
    assert response.status_code == 200
    assert response.media_type == "application/zip"
    cd = response.headers.get("content-disposition", "")
    assert "My" in cd and "Song.zip" in cd
    zip_path = Path(response.path)
    assert zip_path.exists()

    import zipfile
    with zipfile.ZipFile(zip_path, "r") as zf:
        namelist = zf.namelist()
        assert "lyrics_video.mp4" in namelist
        assert "thumbnail.jpg" in namelist


def test_generate_thumbnail_without_title(tmp_path):
    from PIL import Image
    import processor as proc

    bg = tmp_path / "bg.jpg"
    img = Image.new("RGB", (1920, 1080), "#112233")
    img.save(bg, "JPEG")
    thumb = tmp_path / "thumb.jpg"

    # Must not raise UnboundLocalError when title is empty
    proc.generate_thumbnail(bg, thumb, title="", artist="Solo Artist")
    assert thumb.exists()
    assert thumb.stat().st_size > 0
