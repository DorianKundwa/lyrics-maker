"""
Lyrics Video Maker — FastAPI Backend
"""

import asyncio
import shutil
import uuid
import re
import time
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import processor as proc

# ─── Setup ───────────────────────────────────────────────────────────────────

BASE_DIR   = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
STATIC_DIR = BASE_DIR / "static"

for d in (UPLOAD_DIR, OUTPUT_DIR, STATIC_DIR):
    d.mkdir(exist_ok=True)

app = FastAPI(title="Lyrics Video Maker", version="1.0.0")

# In-memory job store  {job_id: {...}}
_jobs: dict[str, dict] = {}


# ─── Static / Index ──────────────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


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


_semaphore = asyncio.Semaphore(2)

def _cleanup_old_outputs():
    now = time.time()
    for d in OUTPUT_DIR.iterdir():
        if d.is_dir():
            if now - d.stat().st_mtime > 3600:
                shutil.rmtree(d, ignore_errors=True)

@app.post("/api/process")
async def start_process(
    background_tasks: BackgroundTasks,
    audio:       UploadFile = File(...),
    background:  Optional[UploadFile] = File(None),
    lyrics:      Optional[UploadFile] = File(None),
    lyrics_text: Optional[str] = Form(None),
    outro:       Optional[UploadFile] = File(None),
    title:       str = Form(""),
    artist:      str = Form(""),
    font_name:   str = Form("Arial"),
    font_size:   int = Form(72),
    bg_color:    str = Form("#000000"),
    stem_engine: str = Form("demucs"),
    word_highlight: bool = Form(True),
):
    if artist and title:
        job_id = f"{_safe(artist, 'artist')}_{_safe(title, 'song')}"
    elif artist or title:
        job_id = _safe(artist or title, "job")
    else:
        job_id = str(uuid.uuid4())

    _cleanup_old_outputs()
    job_dir = UPLOAD_DIR / job_id
    out_dir = OUTPUT_DIR / job_id
    
    # Ensure empty directory if reusing name
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
        
    job_dir.mkdir(parents=True)
    out_dir.mkdir(parents=True)

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
            
    except ValueError as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        return JSONResponse({"error": str(e)}, status_code=400)

    _jobs[job_id] = {"status": "queued", "step": "", "progress": 0, "error": None}

    background_tasks.add_task(
        _run_job, job_id, audio_path, lyrics_path, bg_path, outro_path, out_dir, title, artist, font_name, font_size, stem_engine, word_highlight
    )
    return {"job_id": job_id}


# ─── Job Runner ──────────────────────────────────────────────────────────────

def _set(job_id, *, step="", progress=0):
    _jobs[job_id].update(status="running", step=step, progress=progress)


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
):
    async with _semaphore:
        job = _jobs[job_id]
        loop = asyncio.get_running_loop()

    def run(fn, *args, **kwargs):
        return loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    def check_cancelled():
        if _jobs.get(job_id, {}).get("status") == "cancelled":
            raise InterruptedError("Job cancelled by user")

    try:
        check_cancelled()
        # 1 — Thumbnail
        _set(job_id, step="Generating YouTube thumbnail…", progress=5)
        thumb_path = out_dir / "thumbnail.jpg"
        await run(proc.generate_thumbnail, bg_path, thumb_path, title, artist, (1280, 720), font_name)

        check_cancelled()
        # 1.5 — Stem Separation
        _set(job_id, step="Separating stems (this may take a few minutes)…", progress=15)
        from stem_separator import separate_audio
        vocals_path, inst_audio_path = await run(separate_audio, audio_path, stem_engine, str(out_dir))

        check_cancelled()
        # 2 — Instrumental video
        _set(job_id, step="Rendering instrumental video…", progress=35)
        inst_path = out_dir / "instrumental.mp4"
        await run(
            proc.generate_instrumental_video,
            bg_path, inst_audio_path, inst_path, outro_path, title, artist,
        )

        check_cancelled()
        # 3 — Lyrics video
        _set(job_id, step="Rendering lyrics video…", progress=65)
        lv_path = out_dir / "lyrics_video.mp4"
        await run(
            proc.generate_lyrics_video,
            bg_path, audio_path, vocals_path, lyrics_path, lv_path, outro_path, title, artist, font_name, font_size, word_highlight
        )

        job.update(
            status="complete",
            step="All files ready!",
            progress=100,
            files={
                "lyrics_video":  str(lv_path),
                "instrumental":  str(inst_path),
                "thumbnail":     str(thumb_path),
            },
        )
        
        # Cleanup intermediate files in out_dir
        keep_files = {lv_path.name, inst_path.name, thumb_path.name}
        for f in out_dir.iterdir():
            if f.is_file() and f.name not in keep_files:
                try:
                    f.unlink()
                except Exception:
                    pass

    except InterruptedError:
        job.update(status="error", step="Cancelled", error="Job was cancelled by the user.")
    except Exception as exc:
        import traceback
        traceback.print_exc()
        job.update(status="error", step="Failed", error="An internal error occurred during processing. Please try again.")
    finally:
        job_dir = UPLOAD_DIR / job_id
        shutil.rmtree(job_dir, ignore_errors=True)


# ─── Status & Download ───────────────────────────────────────────────────────

@app.get("/api/jobs")
def get_jobs():
    return _jobs

@app.delete("/api/jobs/{job_id}")
def cancel_job(job_id: str):
    if job_id in _jobs:
        _jobs[job_id]["status"] = "cancelled"
    return {"success": True}


@app.get("/api/status/{job_id}")
def get_status(job_id: str):
    if job_id not in _jobs:
        return JSONResponse({"error": "Not found"}, status_code=404)
    j = _jobs[job_id]
    resp = {k: v for k, v in j.items() if k != "files"}
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
    media = "video/mp4" if path.suffix == ".mp4" else "image/jpeg"
    return FileResponse(path, media_type=media, filename=path.name)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _safe(name: Optional[str], fallback: str) -> str:
    if not name:
        return fallback
    # keep only the last part and sanitise
    stem = Path(name).name
    return re.sub(r"[^\w.\-]", "_", stem) or fallback
