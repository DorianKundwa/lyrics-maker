# Deploying on Coolify

**Build pack:** Dockerfile
**Port:** 8000

## Persistent storage

Without volume mounts, every Coolify redeploy wipes these and forces a
re-download of multi-GB model weights on the next job:

| Container path             | Why it needs to persist                                                                 |
| --------------------------- | ----------------------------------------------------------------------------------------- |
| `/app/alignments`           | `_jobs_store.json` (completed job history) and the data the Lyric Editor reads/writes.   |
| `/app/.model_cache`         | Demucs' `htdemucs` model (~1.26 GB). Re-downloaded on first job after every redeploy otherwise. |
| `/root/.cache/huggingface`  | WhisperX/faster-whisper model + alignment-model downloads (pulled via huggingface_hub).  |

`/app/uploads` and `/app/outputs` don't need a volume — the app already
self-expires anything older than an hour in both.

## Notes

- `demucs` and `whisperx` were both missing from `requirements.txt` even
  though `stem_separator.py` and `aligners/` call them as the primary
  (non-fallback) engines — added in this branch, along with a CPU-only
  torch install in the Dockerfile to keep the image size reasonable on a
  GPU-less VPS.
- `click==7.1.2` / `typer==0.3.2` are pinned much older than what
  whisperx's dependency tree (transformers, huggingface-hub, etc.)
  typically expects. Untouched here since there's no confirmed conflict,
  but if the Coolify build fails during `pip install`, this pin is the
  first thing to loosen.
