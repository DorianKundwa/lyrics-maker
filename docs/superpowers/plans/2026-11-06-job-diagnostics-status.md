# Job Diagnostics and Status Updates Implementation Plan

> **For agentic workers:** Implement this plan task-by-task and check each item before moving on. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make LyricForge render jobs observable and diagnosable while reporting safe, useful status to users.

**Architecture:** Add a rotating local diagnostic logger; run render FFmpeg processes with machine-readable progress output and callbacks; merge the progress and liveness data into the existing in-memory job status response; keep user-facing errors concise and include the job ID while logging tracebacks and FFmpeg stderr locally. The existing browser poller will display percentage, stage, elapsed/last-update information, and a warning once FFmpeg output progress has been idle for three minutes without cancelling the render.

**Tech Stack:** Python, FastAPI, subprocess/FFmpeg, existing plain JavaScript UI and pytest tests.

**Spec:** Approved conversation design summarized in task context; no separate design file was created.

## Global Constraints

- Preserve existing routes and job status fields; extend status data rather than changing route shape.
- Show plain-language errors and job IDs to users; never return tracebacks or FFmpeg diagnostics to the public UI.
- Use rotating local logs for diagnostics; do not log secrets.
- A render is only considered stalled after 180 seconds without FFmpeg progress; display when progress last updated and continue rendering.
- Include stage, progress, last update, elapsed time, and warnings in job status.
- Do not add dependencies.

## Review Focus

- FFmpeg exits unsuccessfully or is missing: user sees a safe job-ID error; diagnostics go to local logs.
- FFmpeg emits progress keys in alternate timestamp formats or malformed values: parser tolerates them without failing the job.
- FFmpeg runs longer than 180 seconds without progress: one visible warning includes last update time; it does not terminate the process.
- Cancellation occurs during FFmpeg: process terminates and job is reported cancelled.
- Non-render stage (upload, stem separation, alignment) takes a long time: stage and elapsed/last update remain meaningful; FFmpeg-specific stall warning is not falsely claimed.

---

### Task 1: Add rotating diagnostics and FFmpeg progress runner

**Files:**
- Create: `logging_setup.py`
- Modify: `processor.py`
- Create: `tests/test_job_diagnostics.py`

**Interfaces:**
- Produces `configure_logging(base_dir: Path | str) -> logging.Logger`.
- Produces `parse_ffmpeg_progress_line(line: str) -> tuple[str, float | str | None]` and `_run_ffmpeg(cmd, *, duration, on_progress=None, is_cancelled=None) -> None`.
- `FFmpegRenderError` carries return code and stderr details for local logging only.

- [ ] Add tests for FFmpeg progress timestamp parsing, malformed input, successful callback, nonzero exit with retained diagnostic tail, missing executable, and cancellation.
- [ ] Run the focused tests and confirm expected failures before implementation.
- [ ] Implement the rotating logger (5 MiB per file, three backups) and FFmpeg progress runner. Keep stderr in a temporary file, parse `-progress pipe:1`, and terminate only when the supplied cancellation predicate is true.
- [ ] Replace both render functions' capture-output subprocess calls with `_run_ffmpeg`; use audio duration for progress normalization and preserve existing exceptions for callers.
- [ ] Run `pytest tests/test_job_diagnostics.py -q` and confirm it passes.

### Task 2: Publish live job metadata and safe failures

**Files:**
- Modify: `app.py`
- Modify: `tests/test_job_diagnostics.py`

**Interfaces:**
- Job dictionary adds `started_at`, `updated_at`, `last_progress_at`, `elapsed_seconds`, `last_update`, and `stall_warning` fields.
- `_set(job_id, *, step="", progress=0, **metadata)` initializes stage/progress liveness timestamps.
- A render callback updates stage progress continuously, retaining stage ranges 35–65 for instrumental and 65–95 for lyrics rendering.
- `/api/status/{job_id}` exposes derived elapsed time and warning state without file paths or internal diagnostics.

- [ ] Add focused tests for status metadata, elapsed-time derivation, the 180-second stall warning and its clear/non-render behavior, and safe errors that include a job ID but exclude traceback/FFmpeg details.
- [ ] Run those tests and confirm expected failures before implementation.
- [ ] Configure logger during app setup; initialize timing/status fields at submission and each stage transition; log job lifecycle, warnings, exceptions with traceback, and FFmpeg stderr, excluding secrets.
- [ ] Wire a thread-safe render callback and cancellation predicate to processor functions through their existing executor calls. Update status without regressing monotonically within the current stage.
- [ ] Convert unexpected failures to a concise user-facing message with the job ID, while preserving cancellation semantics; keep detailed diagnostics local.
- [ ] Run `pytest tests/test_job_diagnostics.py -q` and confirm it passes.

### Task 3: Show live render progress and non-fatal stall warnings

**Files:**
- Modify: `static/index.html`

**Interfaces:**
- Existing `/api/status/{job_id}` poll loop remains at 1.5-second cadence.
- Poll rendering percentage, stage text, elapsed duration, last progress update, and optional `stall_warning`.

- [ ] Add a compact status line to the existing progress panel.
- [ ] Update polling to render elapsed and last-update details and to show a non-blocking 3-minute warning without stopping polling or enabling cancellation automatically.
- [ ] Ensure terminal errors still show the safe backend message and job ID through the existing error banner.

### Task 4: Verify integrated behavior

**Files:**
- Verify: `app.py`, `processor.py`, `logging_setup.py`, `static/index.html`, `tests/test_job_diagnostics.py`

- [ ] Run `pytest tests/test_job_diagnostics.py -q` and verify all focused cases pass.
- [ ] Run the existing pytest suite once and verify no regressions.
- [ ] Run `git diff --check` and inspect the final diff for secrets or diagnostics exposed in the status/UI response.
