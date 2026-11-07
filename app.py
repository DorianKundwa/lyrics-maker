"""
Lyrics Video Maker — FastAPI Backend
"""

import asyncio
import json
import shutil
import uuid
import re
import time
import zipfile
from pathlib import Path
from typing import Optional

from concurrency import run_heavy
from fastapi import BackgroundTasks, FastAPI, File, Form, UploadFile, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import processor as proc
import lyrics_fetcher as lf
import youtube_downloader as yt
from dotenv import load_dotenv

# ─── Setup ───────────────────────────────────────────────────────────────────

BASE_DIR   = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
STATIC_DIR = BASE_DIR / "static"
YT_DIR     = BASE_DIR / "youtube_downloads"

for d in (UPLOAD_DIR, OUTPUT_DIR, STATIC_DIR):
    d.mkdir(exist_ok=True)

app = FastAPI(title="Lyrics Video Maker", version="1.0.0")

# In-memory job store  {job_id: {...}}
_jobs: dict[str, dict] = {}

# ─── Alignment / Editor ──────────────────────────────────────────────────────

ALIGN_DIR  = Path(__file__).parent / "alignments"
ALIGN_DIR.mkdir(exist_ok=True)
JOBS_STORE = ALIGN_DIR / "_jobs_store.json"

# Editor audio/background copies and engine JSONs are kept this long so old
# jobs can be re-rendered; after that they are pruned on the next submission.
ALIGNMENT_RETENTION_SECONDS = 7 * 24 * 60 * 60
# Inputs for failed/interrupted jobs are kept this long so they can be resumed.
RESUMABLE_RETENTION_SECONDS = 7 * 24 * 60 * 60

_rerender_jobs: dict[str, dict] = {}


def _job_input_paths(job: dict) -> dict[str, Path]:
    params = job.get("params") or {}
    paths: dict[str, Path] = {}
    for key in ("audio_path", "lyrics_path", "bg_path"):
        value = params.get(key)
        if value:
            paths[key] = Path(value)
    outro = params.get("outro_path")
    if outro:
        paths["outro_path"] = Path(outro)
    return paths


def _missing_job_inputs(job: dict) -> list[str]:
    return [str(path) for path in _job_input_paths(job).values() if not path.exists()]


def _can_resume(job: dict) -> bool:
    return bool(job.get("params")) and not _missing_job_inputs(job)


def _persist_jobs() -> None:
    """Atomically write every job record, including failed/resumable ones."""
    try:
        tmp = JOBS_STORE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(_jobs, indent=2, default=str))
        tmp.replace(JOBS_STORE)
    except Exception:
        import traceback
        traceback.print_exc()


def _load_jobs_store() -> None:
    """Restore jobs and mark unfinished work as interrupted after a restart.

    A queued/running record cannot still be running in a fresh process, so it is
    surfaced as a resumable error instead of staying stuck at its last stage.
    """
    if not JOBS_STORE.exists():
        return
    try:
        stored = json.loads(JOBS_STORE.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(stored, dict):
        return
    now = time.time()
    changed = False
    for jid, data in stored.items():
        if jid in _jobs or not isinstance(data, dict):
            continue
        if data.get("status") in ("queued", "running"):
            resumable = _can_resume(data)
            data.update(
                status="error",
                step="Interrupted",
                error=(
                    "Lyrics Video Maker stopped before this job finished. Resume it to continue."
                    if resumable
                    else "Lyrics Video Maker stopped before this job finished, and its input files are no longer available."
                ),
                error_code="interrupted",
                finished_at=now,
                last_update_at=now,
                resumable=resumable,
            )
            changed = True
        elif "resumable" not in data:
            # Keep legacy completed records readable without rewriting the store
            # just because this field was added later.
            data["resumable"] = False
        # Backfill top-level title/artist from params for records that pre-date
        # the top-level field (they only stored it inside params).
        if "title" not in data and isinstance(data.get("params"), dict):
            data["title"] = data["params"].get("title", "")
        if "artist" not in data and isinstance(data.get("params"), dict):
            data["artist"] = data["params"].get("artist", "")
        _jobs[jid] = data
    if changed:
        _persist_jobs()


_load_jobs_store()


def _save_jobs_store() -> None:
    """Compatibility wrapper for older call sites."""
    _persist_jobs()


def _srt_time(ts: str) -> float:
    """Parse SRT timestamp 'HH:MM:SS,mmm' to seconds."""
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def _parse_srt(srt_path: Path) -> list:
    """Parse an SRT file into a list of Lyric Editor segment dicts."""
    text = srt_path.read_text(encoding="utf-8", errors="replace")
    segments = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
        if len(lines) < 3:
            continue
        m = re.match(r"(\d+:\d+:\d+,\d+)\s*-->\s*(\d+:\d+:\d+,\d+)", lines[1])
        if not m:
            continue
        segments.append({
            "index":    len(segments),
            "start":    round(_srt_time(m.group(1)), 3),
            "end":      round(_srt_time(m.group(2)), 3),
            "text":     " ".join(lines[2:]),
            "y_offset": 0,
            "words":    [],
        })
    return segments


def _segments_from_timed(timed: Optional[list]) -> list:
    """Convert the renderer's timed segments into Lyric Editor segments.

    Keeping the word arrays is what lets the editor re-render with the original
    karaoke highlighting instead of falling back to plain per-line text.
    """
    segments = []
    for index, item in enumerate(timed or []):
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError):
            continue
        segments.append({
            "index":      index,
            "start":      round(start, 3),
            "end":        round(end, 3),
            "text":       str(item.get("text", "")),
            "y_offset":   0,
            "words":      item.get("words") or [],
            "orig_start": round(start, 3),
            "orig_end":   round(end, 3),
        })
    return segments


def _save_alignment_for_editor(
    job_id: str,
    audio_path,
    bg_path,
    out_dir: Path,
    title: str,
    artist: str,
    font_name: str,
    font_size: int,
    word_highlight: bool,
    language: str = "en",
    active_color: str = "#FFFFFF",
    upcoming_color: str = "#FF0000",
    sung_color: str = "#FFFFFF",
    lyric_style: str = "classic",
    timed: Optional[list] = None,
) -> None:
    """
    Called after a successful render (before cleanup).
    • Parses the SRT written by the processor into editor segments.
    • Copies source audio + background to ALIGN_DIR/<job_id>/ for re-renders.
    • Saves <job_id>_alignment.json consumed by the Lyric Editor.
    """
    segments = _segments_from_timed(timed)
    if not segments:
        # Older jobs (and any render that could not return its timings) still
        # fall back to the line-level SRT.
        srt_path = out_dir / "lyrics.srt"
        if not srt_path.exists():
            return
        segments = _parse_srt(srt_path)
    total_duration = 0.0
    lv             = out_dir / "lyrics_video.mp4"
    if lv.exists():
        try:
            total_duration = proc.get_media_duration(str(lv))
        except Exception as exc:
            print(f"Could not read lyrics video duration for the editor: {exc}")

    # Persist source files for future re-renders
    align_job_dir = ALIGN_DIR / job_id
    align_job_dir.mkdir(exist_ok=True)

    # Prefer already-concatenated audio (includes outro) if available
    combined = out_dir / "_combined.m4a"
    if combined.exists():
        audio_dest = align_job_dir / "audio_combined.m4a"
        shutil.copy2(str(combined), str(audio_dest))
    else:
        audio_dest = align_job_dir / f"audio{Path(str(audio_path)).suffix}"
        shutil.copy2(str(audio_path), str(audio_dest))

    bg_dest = align_job_dir / f"bg{Path(str(bg_path)).suffix}"
    shutil.copy2(str(bg_path), str(bg_dest))

    alignment = {
        "job_id":         job_id,
        "segments":       segments,
        "total_duration": total_duration,
        "font_name":      font_name,
        "font_size":      font_size,
        "word_highlight": word_highlight,
        "language":       language,
        "active_color":   active_color,
        "upcoming_color": upcoming_color,
        "sung_color":     sung_color,
        "lyric_style":    lyric_style,
        "audio_path":     str(audio_dest),
        "bg_path":        str(bg_dest),
        "out_dir":        str(out_dir),
        "title":          title,
        "artist":         artist,
    }

    align_path = ALIGN_DIR / f"{job_id}_alignment.json"
    align_path.write_text(json.dumps(alignment, indent=2), encoding="utf-8")
    _jobs[job_id]["alignment_path"] = str(align_path)


# ─── Static / Index ──────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/editor")
def editor():
    return FileResponse(STATIC_DIR / "editor.html")


# ─── Upload & Process ────────────────────────────────────────────────────────

async def _save(upload: UploadFile, dest: Path) -> Path:
    max_size = 500 * 1024 * 1024 # 500 MB
    bytes_read = 0
    with open(dest, "wb") as f:
        while chunk := await upload.read(1024 * 256):
            bytes_read += len(chunk)
            if bytes_read > max_size:
                raise ValueError("File exceeds maximum allowed size (500MB).")
            f.write(chunk)
    return dest


_semaphore: Optional[asyncio.Semaphore] = None


def _get_semaphore() -> asyncio.Semaphore:
    # Created on first use from inside the running event loop. Creating it at
    # import time can bind it to the wrong loop on Python 3.9.
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(2)
    return _semaphore


# Strong references to the running job tasks, so they are not garbage
# collected while the semaphore still has them queued.
_job_tasks: set[asyncio.Task] = set()


def _schedule_job(job_id: str, **params) -> None:
    """Run a queued job as its own task, letting the semaphore do the scheduling.

    Starlette awaits BackgroundTasks one after another, so a batch queued that
    way would run song by song and never use the second job slot.
    """
    task = asyncio.create_task(_run_job(job_id, **params))
    _job_tasks.add(task)
    task.add_done_callback(_job_tasks.discard)


def _cleanup_old_outputs():
    now = time.time()
    changed = False
    for base_dir in (OUTPUT_DIR, UPLOAD_DIR):
        if not base_dir.exists():
            continue
        for d in base_dir.iterdir():
            if not d.is_dir():
                continue
            job = _jobs.get(d.name, {})
            # Never delete files of a job that is still waiting or running.
            if job.get("status") in ("queued", "running"):
                continue
            if job.get("resumable"):
                finished = (
                    job.get("finished_at")
                    or job.get("last_update_at")
                    or job.get("queued_at")
                    or now
                )
                if now - finished <= RESUMABLE_RETENTION_SECONDS:
                    continue
                job["resumable"] = False
                changed = True
            if now - d.stat().st_mtime <= 3600:
                continue
            shutil.rmtree(d, ignore_errors=True)
    if changed:
        _persist_jobs()


def _cleanup_old_alignments() -> None:
    """Prune editor assets and engine JSONs older than the retention window.

    Every completed job keeps a copy of its audio and background for later
    re-renders. Without this the folder grows by several MB per song forever.
    """
    now = time.time()
    try:
        entries = list(ALIGN_DIR.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        if entry.name == JOBS_STORE.name:
            continue
        job_id = entry.name.split("_alignment", 1)[0] if entry.is_file() else entry.name
        if _jobs.get(job_id, {}).get("status") in ("queued", "running"):
            continue
        try:
            if now - entry.stat().st_mtime <= ALIGNMENT_RETENTION_SECONDS:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink()
        except OSError:
            pass

    # Engine output is an intermediate cache; nothing reads it after the render.
    engine_dir = BASE_DIR / "aligners" / "alignments"
    if not engine_dir.exists():
        return
    for entry in engine_dir.glob("*.json"):
        try:
            if now - entry.stat().st_mtime > ALIGNMENT_RETENTION_SECONDS:
                entry.unlink()
        except OSError:
            pass


def _new_job_id(artist: str, title: str) -> str:
    """Unique per submission, so two jobs for the same song never share folders."""
    suffix = uuid.uuid4().hex[:8]
    if artist and title:
        return f"{_safe(artist, 'artist')}_{_safe(title, 'song')}_{suffix}"
    if artist or title:
        return f"{_safe(artist or title, 'job')}_{suffix}"
    return str(uuid.uuid4())


async def _store_job(
    *,
    audio: UploadFile,
    background: Optional[UploadFile],
    lyrics: Optional[UploadFile],
    lyrics_text: Optional[str],
    outro: Optional[UploadFile],
    outro_text: str,
    title: str,
    artist: str,
    font_name: str,
    font_size: int,
    bg_color: str,
    stem_engine: str,
    word_highlight: bool,
    language: str,
    active_color: str,
    upcoming_color: str,
    sung_color: str,
    lyric_style: str,
) -> tuple[str, dict]:
    """Save one song's uploads and record its queued job.

    Returns ``(job_id, params)``. Raises ValueError (bad or oversized upload) or
    OSError (disk) after removing both the upload and output folders, so a
    rejected submission leaves no half-made job behind.
    """
    job_id = _new_job_id(artist, title)
    job_dir = UPLOAD_DIR / job_id
    out_dir = OUTPUT_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Save uploads
        audio_path  = await _save(audio,      job_dir / _safe(audio.filename,      "audio"))
        if background and background.filename:
            bg_path = await _save(background, job_dir / _safe(background.filename, "background.jpg"))
        else:
            # Create solid color background
            bg_path = job_dir / "background.jpg"
            from PIL import Image
            img = Image.new("RGB", (1920, 1080), bg_color)
            img.save(bg_path, "JPEG")

        outro_path  = None
        if outro and outro.filename:
            outro_path = await _save(outro, job_dir / _safe(outro.filename, "outro"))

        if lyrics and lyrics.filename:
            lyrics_path = await _save(lyrics, job_dir / _safe(lyrics.filename, "lyrics.txt"))
        elif lyrics_text and lyrics_text.strip():
            lyrics_path = job_dir / "lyrics.txt"
            with open(lyrics_path, "w", encoding="utf-8") as f:
                f.write(lyrics_text)
        else:
            raise ValueError("Lyrics file or text must be provided.")

    except (ValueError, OSError):
        shutil.rmtree(job_dir, ignore_errors=True)
        shutil.rmtree(out_dir, ignore_errors=True)
        raise

    now = time.time()
    params = {
        "audio_path": str(audio_path),
        "lyrics_path": str(lyrics_path),
        "bg_path": str(bg_path),
        "outro_path": str(outro_path) if outro_path else None,
        "out_dir": str(out_dir),
        "title": title,
        "artist": artist,
        "font_name": font_name,
        "font_size": font_size,
        "stem_engine": stem_engine,
        "word_highlight": word_highlight,
        "language": language,
        "active_color": active_color,
        "upcoming_color": upcoming_color,
        "sung_color": sung_color,
        "lyric_style": lyric_style,
        "outro_text": outro_text or "Thanks for watching!",
    }
    _jobs[job_id] = {
        "status": "queued",
        "step": "Queued…",
        "progress": 0,
        "error": None,
        "title": title,
        "artist": artist,
        "created_at": now,
        "queued_at": now,
        "last_update_at": now,
        "attempts": 1,
        "resumable": False,
        "params": params,
    }
    _persist_jobs()
    return job_id, params


def _batch_stem(name: str) -> str:
    """Filename stem used to pair a batch's audio with its lyrics."""
    return Path(name or "").stem.strip().lower()


def pair_batch_files(
    audios: list[UploadFile], lyrics_files: list[UploadFile]
) -> tuple[list[tuple[UploadFile, UploadFile]], list[str]]:
    """Pair each audio upload with the lyrics file that shares its filename stem.

    Returns ``(pairs, problems)``. Matching is case-insensitive: ``Song.mp3``
    pairs with ``song.txt``. Any problem means the whole batch must be rejected
    before a job is queued, so a song never silently renders another song's
    lyrics.
    """
    by_stem: dict[str, list[UploadFile]] = {}
    for lyrics in lyrics_files or []:
        if lyrics and lyrics.filename:
            by_stem.setdefault(_batch_stem(lyrics.filename), []).append(lyrics)

    pairs: list[tuple[UploadFile, UploadFile]] = []
    problems: list[str] = []
    audio_stems: dict[str, list[str]] = {}
    for audio in audios or []:
        if not audio or not audio.filename:
            continue
        stem = _batch_stem(audio.filename)
        audio_stems.setdefault(stem, []).append(audio.filename)
        matches = by_stem.get(stem, [])
        if len(matches) == 1:
            pairs.append((audio, matches[0]))
        elif not matches:
            problems.append(
                f'"{audio.filename}" has no lyrics file named "{stem}.txt" or "{stem}.lrc".'
            )
        else:
            names = ", ".join(sorted(m.filename for m in matches))
            problems.append(
                f'"{audio.filename}" matches more than one lyrics file ({names}); keep one of them.'
            )

    for stem, names in audio_stems.items():
        if len(names) > 1:
            problems.append(
                f'{len(names)} audio files share the name "{stem}" '
                f'({", ".join(names)}); rename them so each song is unique.'
            )

    for stem, files in by_stem.items():
        if stem not in audio_stems:
            names = ", ".join(sorted(f.filename for f in files))
            problems.append(f"Lyrics file {names} matches no audio file.")

    return pairs, problems


@app.post("/api/process")
async def start_process(
    background_tasks: BackgroundTasks,
    audio:       UploadFile = File(...),
    background:  Optional[UploadFile] = File(None),
    lyrics:      Optional[UploadFile] = File(None),
    lyrics_text: Optional[str] = Form(None),
    outro:       Optional[UploadFile] = File(None),
    outro_text:  str = Form("Thanks for watching!"),
    title:       str = Form(""),
    artist:      str = Form(""),
    font_name:   str = Form("Arial"),
    font_size:   int = Form(72),
    bg_color:    str = Form("#000000"),
    stem_engine: str = Form("demucs"),
    word_highlight: bool = Form(True),
    language:    str = Form("en"),
    active_color:   str = Form("#FFFFFF"),
    upcoming_color: str = Form("#FF0000"),
    sung_color:     str = Form("#FFFFFF"),
    lyric_style:    str = Form("classic"),
):
    _cleanup_old_outputs()
    _cleanup_old_alignments()
    try:
        job_id, params = await _store_job(
            audio=audio,
            background=background,
            lyrics=lyrics,
            lyrics_text=lyrics_text,
            outro=outro,
            outro_text=outro_text,
            title=title,
            artist=artist,
            font_name=font_name,
            font_size=font_size,
            bg_color=bg_color,
            stem_engine=stem_engine,
            word_highlight=word_highlight,
            language=language,
            active_color=active_color,
            upcoming_color=upcoming_color,
            sung_color=sung_color,
            lyric_style=lyric_style,
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    background_tasks.add_task(_run_job, job_id, **params)
    return {"job_id": job_id}


@app.post("/api/process/batch")
async def start_batch(
    audios: list[UploadFile] = File(...),
    lyrics_files: Optional[list[UploadFile]] = File(None),
    background: Optional[UploadFile] = File(None),
    bg_color: str = Form("#000000"),
    font_name: str = Form("Arial"),
    font_size: int = Form(72),
    stem_engine: str = Form("demucs"),
    word_highlight: bool = Form(True),
    language: str = Form("en"),
    active_color: str = Form("#FFFFFF"),
    upcoming_color: str = Form("#FF0000"),
    sung_color: str = Form("#FFFFFF"),
    lyric_style: str = Form("classic"),
    outro_text: str = Form("Thanks for watching!"),
    artist: str = Form(""),
):
    """Queue one ordinary job per audio file, paired with its same-named lyrics.

    Every pair is validated before anything is queued: a missing or ambiguous
    match rejects the whole batch with the reasons, so a song never renders with
    another song's lyrics. Each accepted pair becomes a normal job with its own
    folders, history card and resume support.
    """
    audios = [a for a in audios if a and a.filename]
    if not audios:
        return JSONResponse({"error": "No audio files were uploaded."}, status_code=400)

    pairs, problems = pair_batch_files(audios, lyrics_files or [])
    if problems:
        return JSONResponse(
            {"error": "Nothing was queued. " + " ".join(problems), "problems": problems},
            status_code=400,
        )

    _cleanup_old_outputs()
    _cleanup_old_alignments()

    created: list[tuple[str, dict]] = []
    try:
        for audio, lyrics in pairs:
            if background and background.filename:
                # One shared upload feeds every job, so rewind it each time.
                await background.seek(0)
            created.append(
                await _store_job(
                    audio=audio,
                    background=background,
                    lyrics=lyrics,
                    lyrics_text=None,
                    outro=None,
                    outro_text=outro_text,
                    title=Path(audio.filename).stem,
                    artist=artist,
                    font_name=font_name,
                    font_size=font_size,
                    bg_color=bg_color,
                    stem_engine=stem_engine,
                    word_highlight=word_highlight,
                    language=language,
                    active_color=active_color,
                    upcoming_color=upcoming_color,
                    sung_color=sung_color,
                    lyric_style=lyric_style,
                )
            )
    except (ValueError, OSError) as exc:
        for job_id, _params in created:
            _jobs.pop(job_id, None)
            shutil.rmtree(UPLOAD_DIR / job_id, ignore_errors=True)
            shutil.rmtree(OUTPUT_DIR / job_id, ignore_errors=True)
        _persist_jobs()
        return JSONResponse(
            {"error": f"Batch rejected: {exc}", "problems": [str(exc)]},
            status_code=400,
        )

    for job_id, params in created:
        _schedule_job(job_id, **params)

    return {
        "queued": len(created),
        "jobs": [{"job_id": job_id, "title": params["title"]} for job_id, params in created],
    }


# ─── Job Runner ──────────────────────────────────────────────────────────────

def _set(job_id, *, step="", progress=0):
    job = _jobs.get(job_id)
    if job is None:
        return
    job.update(
        status="running",
        step=step,
        progress=progress,
        last_update_at=time.time(),
        error_code=None,
    )
    _persist_jobs()


def _valid_image(path: Path) -> bool:
    try:
        if not path.exists() or path.stat().st_size == 0:
            return False
        from PIL import Image
        with Image.open(path) as img:
            img.verify()
        return True
    except Exception:
        return False


def _valid_video(path: Path) -> bool:
    try:
        return (
            path.exists()
            and path.stat().st_size > 0
            and proc.get_media_duration(str(path)) > 0
        )
    except Exception:
        return False


async def _run_job(
    job_id: str,
    audio_path, lyrics_path, bg_path, outro_path,
    out_dir: Path,
    title: str,
    artist: str,
    font_name: str = "Arial",
    font_size: int = 72,
    stem_engine: str = "demucs",
    word_highlight: bool = True,
    language: str = "en",
    active_color: str = "#FFFFFF",
    upcoming_color: str = "#FF0000",
    sung_color: str = "#FFFFFF",
    lyric_style: str = "classic",
    outro_text: str = "Thanks for watching!",
    resume: bool = False,
):
    audio_path = Path(audio_path)
    lyrics_path = Path(lyrics_path)
    bg_path = Path(bg_path)
    outro_path = Path(outro_path) if outro_path else None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    async with _get_semaphore():
        job = _jobs[job_id]
        job.setdefault("attempts", 1)
        job["resumable"] = False
        loop = asyncio.get_running_loop()

        def run(fn, *args, **kwargs):
            return loop.run_in_executor(None, lambda: fn(*args, **kwargs))

        def check_cancelled():
            if _jobs.get(job_id, {}).get("cancel_requested"):
                raise InterruptedError("Job cancelled by user")

        try:
            check_cancelled()
            # 1 — Thumbnail
            thumb_path = out_dir / "thumbnail.jpg"
            if resume and _valid_image(thumb_path):
                _set(job_id, step="Reusing thumbnail…", progress=5)
            else:
                _set(job_id, step="Generating YouTube thumbnail…", progress=5)
                await run(proc.generate_thumbnail, bg_path, thumb_path, title, artist, (1280, 720), font_name)

            check_cancelled()
            # 1.5 — Stem Separation
            stem_warnings: list = []
            vocals_path = inst_audio_path = None
            if resume:
                stored_stems = job.get("stem_paths") or {}
                stored_vocals = stored_stems.get("vocals")
                stored_inst = stored_stems.get("instrumental")
                if stored_vocals and stored_inst:
                    candidate_vocals = Path(stored_vocals)
                    candidate_inst = Path(stored_inst)
                    if candidate_vocals.exists() and candidate_inst.exists():
                        vocals_path, inst_audio_path = candidate_vocals, candidate_inst
                        _set(job_id, step="Reusing separated stems…", progress=25)
            if vocals_path is None:
                _set(job_id, step="Separating stems (this may take a few minutes)…", progress=15)
                from stem_separator import separate_audio
                # Demucs shares the heavy-stage slot with lyric alignment, so two
                # jobs never hold two sets of model weights at the same time.
                vocals_path, inst_audio_path = await run_heavy(
                    separate_audio, audio_path, stem_engine, str(out_dir), stem_warnings
                )
                job["stem_paths"] = {
                    "vocals": str(vocals_path),
                    "instrumental": str(inst_audio_path),
                }
                if stem_warnings:
                    job.setdefault("warnings", []).extend(stem_warnings)
                _persist_jobs()

            check_cancelled()
            # 2 — Instrumental video
            inst_path = out_dir / "instrumental.mp4"
            if resume and _valid_video(inst_path):
                _set(job_id, step="Reusing instrumental video…", progress=60)
            else:
                _set(job_id, step="Rendering instrumental video…", progress=35)
                await run(
                    proc.generate_instrumental_video,
                    bg_path, inst_audio_path, inst_path, outro_path, title, artist,
                )

            check_cancelled()
            # 3 — Lyrics video
            lv_path = out_dir / "lyrics_video.mp4"
            if lv_path.exists() and not _valid_video(lv_path):
                try:
                    lv_path.unlink()
                except OSError:
                    pass
            _set(job_id, step="Rendering lyrics video…", progress=65)
            timed = await run(
                proc.generate_lyrics_video,
                bg_path, audio_path, vocals_path, lyrics_path, lv_path, outro_path, title, artist, font_name, font_size, word_highlight, language,
                active_color, upcoming_color, sung_color, lyric_style, outro_text,
            )

            # A cancel that arrived during the last stage must win over "complete".
            check_cancelled()
            srt_path = out_dir / "lyrics.srt"
            lrc_path = out_dir / "lyrics.lrc"
            now = time.time()
            job.update(
                status="complete",
                step="All files ready!",
                progress=100,
                error=None,
                error_code=None,
                resumable=False,
                finished_at=now,
                last_update_at=now,
                files={
                    "lyrics_video":  str(lv_path),
                    "instrumental":  str(inst_path),
                    "thumbnail":     str(thumb_path),
                    **({"lyrics_srt": str(srt_path)} if srt_path.exists() else {}),
                    **({"lyrics_lrc": str(lrc_path)} if lrc_path.exists() else {}),
                },
            )
            _persist_jobs()

            # ── Save alignment data for the Lyric Editor (non-fatal) ─────────
            try:
                _save_alignment_for_editor(
                    job_id, audio_path, bg_path, out_dir,
                    title, artist, font_name, font_size, word_highlight, language,
                    active_color, upcoming_color, sung_color, lyric_style,
                    timed=timed,
                )
                _save_jobs_store()
            except Exception as exc:
                import traceback
                traceback.print_exc()
                job.setdefault("warnings", []).append(
                    f"Lyric Editor data could not be saved, so the editor is unavailable for this job ({exc})."
                )

            # Cleanup intermediate files in out_dir (keep SRT and LRC for download)
            keep_files = {lv_path.name, inst_path.name, thumb_path.name, "lyrics.srt", "lyrics.lrc"}
            for f in out_dir.iterdir():
                if f.is_file() and f.name not in keep_files:
                    try:
                        f.unlink()
                    except Exception:
                        pass

        except InterruptedError:
            now = time.time()
            job.update(
                status="error",
                step="Cancelled",
                error="Job was cancelled by the user.",
                error_code="cancelled",
                resumable=_can_resume(job),
                finished_at=now,
                last_update_at=now,
            )
            _persist_jobs()
        except Exception as exc:
            import traceback
            traceback.print_exc()
            now = time.time()
            job.update(
                status="error",
                step="Failed",
                error="An internal error occurred during processing. Please try again.",
                error_code="failed",
                resumable=_can_resume(job),
                finished_at=now,
                last_update_at=now,
            )
            _persist_jobs()
        finally:
            # Keep uploads for failed/interrupted jobs so Resume can use them.
            if job.get("status") == "complete":
                shutil.rmtree(UPLOAD_DIR / job_id, ignore_errors=True)
            _persist_jobs()


# ─── Status & Download ───────────────────────────────────────────────────────

@app.get("/api/jobs")
def get_jobs():
    """History view: every job except internal param/cancel bookkeeping.

    Public fields are chosen so the UI can build a job-history panel without
    ever receiving raw input paths or cancel-request state.
    """
    result: dict[str, dict] = {}
    for jid, job in _jobs.items():
        if not isinstance(job, dict):
            continue
        completed = job.get("status") == "complete"
        # Expose title/artist from either the top-level job record (new jobs)
        # or from params (older persisted records that pre-date the top-level field).
        params = job.get("params") or {}
        exposable: dict[str, object] = {
            "status": job.get("status"),
            "step": job.get("step"),
            "progress": job.get("progress", 0),
            "attempts": job.get("attempts", 1),
            "resumable": bool(job.get("resumable")),
            "error_code": job.get("error_code"),
            "title": job.get("title") or params.get("title") or "",
            "artist": job.get("artist") or params.get("artist") or "",
            "lyric_style": params.get("lyric_style") or "",
        }
        if job.get("error"):
            exposable["error"] = job["error"]
        exposable["created_at"] = job.get("created_at")
        exposable["last_update_at"] = job.get("last_update_at")
        if job.get("resumable"):
            exposable["finished_at"] = job.get("finished_at")
        if completed:
            job_files = job.get("files") or {}
            if isinstance(job_files, dict):
                available = [k for k in ("lyrics_video", "instrumental", "thumbnail")
                             if Path(job_files.get(k, "")).exists()]
                exposable["files"] = list(job_files.keys())
                exposable["available_files"] = available
                exposable["has_downloads"] = bool(available)
            else:
                exposable["files"] = []
                exposable["available_files"] = []
                exposable["has_downloads"] = False
        else:
            exposable["has_downloads"] = False
            exposable["available_files"] = []
        result[jid] = exposable
    return result

@app.delete("/api/jobs/{job_id}")
def cancel_job(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    if job.get("status") in ("complete", "error"):
        return JSONResponse({"error": "Job already finished"}, status_code=409)
    # Stages check this flag between steps; a running ffmpeg/demucs call finishes first.
    job["cancel_requested"] = True
    return {"success": True, "status": "cancelling"}


def _cancelled_but_still_running(job: dict) -> bool:
    """A user hit Stop, but the runner has not yet caught the flag."""
    return job.get("status") == "running" and job.get("cancel_requested")


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str, background_tasks: BackgroundTasks):
    """Re-queue a failed, cancelled, or interrupted job from its saved inputs."""
    job = _jobs.get(job_id)
    if job is None:
        return JSONResponse({"error": "Not found"}, status_code=404)

    if _cancelled_but_still_running(job):
        return JSONResponse(
            {"error": "The job is still cancelling. Resume it once the Stop finishes."},
            status_code=409,
        )
    if job.get("status") not in ("error", "interrupted", "cancelled"):
        return JSONResponse({"error": "Job is not in a resumable state"}, status_code=409)

    if job.get("status") == "cancelled" and not job.get("resumable") and job.get("params"):
        # The runner hasn't written a resumable flag yet for cancelled jobs; infer it
        # from whether the inputs still exist so the user can resume cancelled work.
        job["resumable"] = not _missing_job_inputs(job)

    missing = _missing_job_inputs(job)
    if missing:
        job["resumable"] = False
        _persist_jobs()
        return JSONResponse(
            {"error": "Cannot resume: input files were removed.", "missing": missing},
            status_code=410,
        )
    if not job.get("resumable") or not job.get("params"):
        return JSONResponse({"error": "This job cannot be resumed."}, status_code=409)

    now = time.time()
    job.update(
        status="queued",
        step="Queued to resume…",
        progress=0,
        error=None,
        error_code=None,
        cancel_requested=False,
        resumable=False,
        resumed_at=now,
        last_update_at=now,
        attempts=int(job.get("attempts", 1)) + 1,
    )
    job.pop("finished_at", None)
    _persist_jobs()
    background_tasks.add_task(_run_job, job_id, resume=True, **job["params"])
    return {"job_id": job_id, "status": "queued", "attempts": job["attempts"]}


@app.get("/api/status/{job_id}")
def get_status(job_id: str):
    if job_id not in _jobs:
        return JSONResponse({"error": "Not found"}, status_code=404)
    j = _jobs[job_id]
    resp = {
        k: v for k, v in j.items()
        if k not in ("files", "params", "cancel_requested")
    }
    if j.get("status") == "complete":
        resp["files"] = list(j["files"].keys())
    return resp


@app.get("/api/download/{job_id}/{file_key}")
def download(job_id: str, file_key: str):
    if job_id not in _jobs or _jobs[job_id].get("status") != "complete":
        return JSONResponse({"error": "Not ready"}, status_code=404)
    files = _jobs[job_id].get("files", {})
    if file_key not in files:
        return JSONResponse({"error": "Unknown file"}, status_code=404)
    path = Path(files[file_key])
    if not path.exists():
        # Outputs are removed an hour after the job, but the job record is kept.
        return JSONResponse({"error": "Output expired. Run the job again."}, status_code=410)
    _mime_map = {
        ".mp4": "video/mp4",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".srt": "text/plain",
        ".lrc": "text/plain",
        ".json": "application/json",
    }
    media = _mime_map.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media, filename=path.name)


@app.get("/api/jobs/{job_id}/download-all")
def download_all(job_id: str):
    """Bundle all available rendered files into a single ZIP for one-click download."""
    if job_id not in _jobs or _jobs[job_id].get("status") != "complete":
        return JSONResponse({"error": "Job not found or not complete"}, status_code=404)
    job = _jobs[job_id]
    files = job.get("files", {})
    existing_files: list[tuple[str, Path]] = []
    for key, path_str in files.items():
        p = Path(path_str)
        if p.exists():
            existing_files.append((p.name, p))

    # Also include corrected video if re-rendered
    out_dir = OUTPUT_DIR / job_id
    corrected = out_dir / "lyrics_video_corrected.mp4"
    if corrected.exists() and not any(name == corrected.name for name, _ in existing_files):
        existing_files.append((corrected.name, corrected))

    if not existing_files:
        return JSONResponse(
            {"error": "Outputs expired or not available on disk. Please generate again."},
            status_code=410,
        )

    title = (job.get("title") or "").strip()
    artist = (job.get("artist") or "").strip()
    if artist and title:
        base_name = f"{artist} - {title}"
    elif title or artist:
        base_name = title or artist
    else:
        base_name = f"lyrics_{job_id[:8]}"
    safe_base = re.sub(r'[^\w\s.\-]', '_', base_name).strip() or "lyrics_bundle"
    zip_filename = f"{safe_base}.zip"

    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / "_bundle.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname, p in existing_files:
            zf.write(p, arcname=arcname)

    return FileResponse(zip_path, media_type="application/zip", filename=zip_filename)


@app.get("/api/lyrics")
def fetch_lyrics_endpoint(title: str = "", artist: str = ""):
    title, artist = title.strip(), artist.strip()
    if not title:
        return JSONResponse({"error": "Title is required."}, status_code=400)
    result, errors = lf.fetch_lyrics(title, artist)
    if result is None:
        return JSONResponse({"found": False, "errors": errors}, status_code=404)
    return {"found": True, **result, "errors": errors}


# ─── YouTube → MP3 ───────────────────────────────────────────────────────────

# {job_id: {"status", "step", "error", "title", "artist", "filename", "file"}}
_yt_jobs: dict[str, dict] = {}


def _cleanup_youtube() -> None:
    """Remove YouTube downloads older than an hour, with their job records."""
    if not YT_DIR.exists():
        return
    now = time.time()
    for d in YT_DIR.iterdir():
        if d.is_dir() and now - d.stat().st_mtime > 3600:
            shutil.rmtree(d, ignore_errors=True)
            _yt_jobs.pop(d.name, None)


def _run_youtube_job(job_id: str, url: str) -> None:
    """Background task: download the audio and record the result on the job."""
    job = _yt_jobs[job_id]
    job.update(status="running", step="Downloading audio…")
    try:
        result = yt.download_mp3(url, YT_DIR / job_id)
        job.update(
            status="complete", step="Done!",
            title=result["title"], artist=result["artist"],
            filename=result["filename"], file=str(result["path"]),
        )
    except yt.YoutubeError as exc:
        job.update(status="error", step="Failed", error=str(exc))
    except Exception:
        import traceback; traceback.print_exc()
        job.update(status="error", step="Failed", error="Download failed. Please try again.")


@app.post("/api/youtube")
def start_youtube(background_tasks: BackgroundTasks, url: str = Form(...)):
    try:
        url = yt.validate_url(url)
    except yt.YoutubeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    _cleanup_youtube()
    job_id = uuid.uuid4().hex
    _yt_jobs[job_id] = {"status": "queued", "step": "Queued", "error": None}
    background_tasks.add_task(_run_youtube_job, job_id, url)
    return {"job_id": job_id}


@app.get("/api/youtube/{job_id}")
def get_youtube_status(job_id: str):
    if job_id not in _yt_jobs:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return {k: v for k, v in _yt_jobs[job_id].items() if k != "file"}


@app.get("/api/youtube/{job_id}/file")
def get_youtube_file(job_id: str):
    job = _yt_jobs.get(job_id)
    if job is None or job.get("status") != "complete":
        return JSONResponse({"error": "Not ready"}, status_code=404)
    path = Path(job["file"])
    if not path.exists():
        return JSONResponse({"error": "File missing"}, status_code=404)
    return FileResponse(path, media_type="audio/mpeg", filename=job["filename"])


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _safe(name: Optional[str], fallback: str) -> str:
    if not name:
        return fallback
    # keep only the last part and sanitise
    stem = Path(name).name
    return re.sub(r"[^\w.\-]", "_", stem) or fallback


# ─── Lyric Editor Endpoints ───────────────────────────────────────────────────

@app.get("/alignment/{job_id}")
def get_alignment(job_id: str):
    """Return alignment segments for the Lyric Editor (corrected > original)."""
    corrected = ALIGN_DIR / f"{job_id}_alignment_corrected.json"
    original  = ALIGN_DIR / f"{job_id}_alignment.json"
    path = corrected if corrected.exists() else original
    if not path.exists():
        return JSONResponse({"error": "Alignment not found"}, status_code=404)
    try:
        return JSONResponse(json.loads(path.read_text(encoding="utf-8")))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/alignment_audio/{job_id}")
def get_alignment_audio(job_id: str):
    """Serve the full audio (with vocals) for the editor preview."""
    corrected = ALIGN_DIR / f"{job_id}_alignment_corrected.json"
    original  = ALIGN_DIR / f"{job_id}_alignment.json"
    path = corrected if corrected.exists() else original
    if not path.exists():
        return JSONResponse({"error": "Alignment not found"}, status_code=404)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        audio_path = Path(data.get("audio_path", ""))
        if not audio_path.exists():
            return JSONResponse({"error": "Audio file not found"}, status_code=404)
        _audio_mime = {
            ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
            ".wav": "audio/wav",  ".ogg": "audio/ogg", ".flac": "audio/flac",
        }
        media_type = _audio_mime.get(audio_path.suffix.lower(), "audio/mpeg")
        return FileResponse(audio_path, media_type=media_type)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.patch("/alignment/{job_id}")
async def patch_alignment(job_id: str, request: Request):
    """Save editor-corrected segments. Body: {segments: [...], active_color?, upcoming_color?, sung_color?}."""
    original = ALIGN_DIR / f"{job_id}_alignment.json"
    if not original.exists():
        return JSONResponse({"error": "Original alignment not found"}, status_code=404)
    try:
        body     = await request.json()
        segments = body.get("segments", [])
        base     = json.loads(original.read_text(encoding="utf-8"))
        base["segments"] = segments
        # Persist any color overrides sent from the editor
        for key in ("active_color", "upcoming_color", "sung_color"):
            if key in body:
                base[key] = body[key]
        corrected = ALIGN_DIR / f"{job_id}_alignment_corrected.json"
        corrected.write_text(json.dumps(base, indent=2), encoding="utf-8")
        _save_jobs_store()
        return {"saved": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/rerender/{job_id}")
async def start_rerender(job_id: str, background_tasks: BackgroundTasks):
    """Trigger a re-render using the (possibly corrected) alignment."""
    corrected = ALIGN_DIR / f"{job_id}_alignment_corrected.json"
    original  = ALIGN_DIR / f"{job_id}_alignment.json"
    path      = corrected if corrected.exists() else original
    if not path.exists():
        return JSONResponse({"error": "Alignment not found"}, status_code=404)
    try:
        align_data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    rerender_id = f"{job_id}_rr"
    _rerender_jobs[rerender_id] = {
        "status": "queued", "step": "Queued", "progress": 0, "error": None,
    }
    background_tasks.add_task(_run_rerender, rerender_id, align_data)
    return {"rerender_id": rerender_id}


@app.get("/rerender_status/{rerender_id}")
def get_rerender_status(rerender_id: str):
    if rerender_id not in _rerender_jobs:
        return JSONResponse({"error": "Not found"}, status_code=404)
    return _rerender_jobs[rerender_id]


@app.get("/api/download_corrected/{rerender_id}")
def download_corrected(rerender_id: str):
    if rerender_id not in _rerender_jobs:
        return JSONResponse({"error": "Not found"}, status_code=404)
    j = _rerender_jobs[rerender_id]
    if j.get("status") != "complete":
        return JSONResponse({"error": "Not ready"}, status_code=404)
    path = Path(j["output_path"])
    if not path.exists():
        return JSONResponse({"error": "File missing"}, status_code=404)
    return FileResponse(path, media_type="video/mp4", filename="lyrics_video_corrected.mp4")


async def _run_rerender(rerender_id: str, align_data: dict) -> None:
    """Background task: generate corrected ASS then call FFmpeg."""
    job  = _rerender_jobs[rerender_id]
    loop = asyncio.get_running_loop()

    def run(fn, *args, **kwargs):
        return loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    try:
        job.update(status="running", step="Rendering corrected lyrics video…", progress=10)
        segments       = align_data["segments"]
        out_dir        = Path(align_data["out_dir"])
        bg_path        = align_data["bg_path"]
        audio_path     = align_data["audio_path"]
        font_name      = align_data.get("font_name",      "Arial")
        font_size      = align_data.get("font_size",      72)
        active_color   = align_data.get("active_color",   "#FFFFFF")
        upcoming_color = align_data.get("upcoming_color", "#FF0000")
        sung_color     = align_data.get("sung_color",     "#FFFFFF")
        lyric_style    = align_data.get("lyric_style",    "classic")
        word_highlight = bool(align_data.get("word_highlight", True))
        out_dir.mkdir(parents=True, exist_ok=True)
        output_path = out_dir / "lyrics_video_corrected.mp4"

        job.update(step="Rendering corrected lyrics video…", progress=30)
        await run(
            proc.rerender_lyrics_video,
            bg_path, audio_path, segments, output_path, font_name, font_size,
            active_color, upcoming_color, sung_color, lyric_style, word_highlight,
        )

        job.update(
            status="complete", step="Done!", progress=100,
            download_url=f"/api/download_corrected/{rerender_id}",
            output_path=str(output_path),
        )
    except Exception as exc:
        import traceback; traceback.print_exc()
        job.update(status="error", step="Failed", error=str(exc))
