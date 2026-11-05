# 🎵 LyricForge — Lyrics Video Maker

A self-hosted web app that takes your audio, lyrics, background image, and optional outro — and automatically renders:

- **🎬 Lyrics Video** — 1920×1080 MP4 with synced subtitles
- **🎵 Instrumental Video** — Same as above, lyrics-free
- **🖼 YouTube Thumbnail** — 1280×720 JPEG, print-ready

---

## Requirements

| Tool | Version |
|------|---------|
| Python | 3.9+ |
| FFmpeg | 5.0+ (with libass, libx264) |

### Install FFmpeg

```bash
# Ubuntu / Debian
sudo apt install ffmpeg

# macOS (Homebrew)
brew install ffmpeg

# Windows — download from https://ffmpeg.org/download.html
```

---

## Installation

```bash
cd lyrics-video-maker
pip install -r requirements.txt
```

The alignment and stem dependencies are large: torch/torchaudio plus model
weights on first use (MMS_FA ~1.2 GB, faster-whisper `small` ~500 MB, Demucs
~1.3 GB). They are what make the accurate alignment path work; without them the
app falls back to Aeneas timing and FFmpeg phase cancellation.

### Optional: lyrics provider token

Lyrics search works without configuration (LRCLIB). Genius needs an access
token, which goes in a `.env` file in the project root:

```
GENIUS_ACCESS_TOKEN=your_token_here
```

Only `GENIUS_ACCESS_TOKEN` is read; the other Genius values are ignored.

---

## Running the Server

```bash
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Then open **http://localhost:8000** in your browser.

The server listens on `127.0.0.1` only, so other devices on your network can't reach it. To allow access from other machines, change the host to `0.0.0.0`, but only on a trusted network: the app has no login.

---

## Usage

1. **Fill in song info** — Title and artist name (used in thumbnail and title watermark)
2. **Upload Main Audio** — MP3, WAV, M4A, OGG, or any format FFmpeg supports
3. **Upload Outro Audio** *(optional)* — Appended to the end of main audio before rendering
4. **Upload Background Image** — JPG, PNG, WEBP in any resolution (auto-cropped to 16:9)
5. **Upload Lyrics File** — see format options below
6. Click **⚡ Generate** and wait ~1–3 minutes depending on song length
7. Download your three output files

---

## Lyrics File Formats

### LRC (recommended — timestamps included)
```
[00:05.00]First line of the song
[00:10.50]Second line here
[01:23.40]Chorus line
```

### Plain text (auto-timed — lines evenly distributed)
```
First line of the song
Second line here
Chorus line
```

LRC files can be downloaded from sites like **lrclib.net**, **kugou.com**, or created with any text editor.

---

## Output Files

| File | Format | Resolution | Notes |
|------|--------|-----------|-------|
| `lyrics_video.mp4` | H.264 MP4 | 1920×1080 | Synced subtitles, ASS format |
| `instrumental.mp4` | H.264 MP4 | 1920×1080 | No lyrics, title watermark only |
| `thumbnail.jpg` | JPEG | 1280×720 | Ready for YouTube upload |

All videos are YouTube-ready (yuv420p, AAC 192k, faststart flag).

---

## Project Structure

```
lyrics-video-maker/
├── app.py                 ← FastAPI server, job queue, Lyric Editor endpoints
├── processor.py           ← Lyrics parsing, ASS styles, FFmpeg, thumbnail
├── stem_separator.py      ← Demucs vocals/instrumental separation
├── lyrics_fetcher.py      ← LRCLIB / Genius / AZLyrics lookup
├── youtube_downloader.py  ← YouTube → MP3 (yt-dlp)
├── aligners/
│   ├── __init__.py        ← Engine cascade (combo → forced → WhisperX → Aeneas)
│   ├── alignment_combo.py ← Forced alignment + Whisper repair of flagged lines
│   ├── forced_align_mms.py← MMS_FA CTC forced alignment
│   ├── alignment_whisperx.py
│   └── alignment_aeneas.py
├── static/                ← Web UI and Lyric Editor modules
├── tests/                 ← pytest suite
├── tools/eval_alignment.py← JamendoLyrics accuracy harness
├── uploads/               ← Temporary uploads (auto-created)
├── outputs/               ← Rendered files (auto-created, pruned after 1 h)
└── alignments/            ← Editor audio/background copies (pruned after 7 days)
```

---

## How It Works

1. Uploaded files are saved to `uploads/<job-id>/`
2. A background task is queued immediately
3. The processor:
   - Parses LRC/TXT lyrics into timed segments
   - Generates an ASS subtitle file for FFmpeg
   - Concatenates main audio + outro (if provided)
   - Renders the instrumental video with `ffmpeg -loop 1`
   - Renders the lyrics video with `subtitles=` filter
   - Creates the thumbnail with Pillow (PIL)
4. The browser polls `/api/status/<job_id>` every 1.5 seconds
5. Download links appear when all three files are ready

---

## Lyric alignment

When the lyrics carry no timestamps, the engines are tried in this order:

1. **Forced + Whisper (`combo`, default)** — `aligners/alignment_combo.py` still
   places every *known* lyric word on the isolated vocal with MMS_FA, so timing
   comes from the audio rather than a transcript. Only the lines the forced
   aligner itself flags (they span a silence, take far longer than their word
   count can sustain, or put the same line twice at the same moment) get a second
   opinion: a short window around the line is transcribed with faster-whisper and
   replaces the forced times only when the transcript confirms at least 60% of
   that line's words. Clean lines cost no speech recognition at all. Downloads a
   ~1.2 GB MMS_FA checkpoint and a ~500 MB Whisper model on first use; needs
   torchaudio 2.6–2.8 (`torchaudio.functional.forced_align` was removed in 2.9).
2. **MMS_FA only** — set `LYRIC_ALIGN_ENGINE=forced` to skip the repair pass.
3. **WhisperX** — transcribes the vocal, then matches transcript words to the lyrics.
4. **Aeneas / heuristic** — last-resort fallbacks.

| Variable | Default | Effect |
|---|---|---|
| `LYRIC_ALIGN_ENGINE` | `combo` | `combo` (forced + repair), `forced`, or `auto` |
| `LYRIC_WHISPER_MODEL` | `small` | faster-whisper model used for the repair pass |

Measured on five Spanish songs from JamendoLyrics with word-level reference timings,
the forced aligner places **93.7%** of word onsets within ±0.3 s (mean error 0.06–0.22 s
on four of the five tracks) against **30.8%** for WhisperX. The one weak track still
beats the old path (76.3% vs 3.3%); its stalled lines are marked `suspicious` in the
alignment JSON with a reason. On the same five mix tracks the default combo engine
measures **94.3%** (MAE 0.72 s) against 93.7% for forced alone, by repairing three
Caralibro lines (76.3% → 80.6%); the other four tracks have no flagged lines and
cost no speech recognition. On the app's Demucs vocal stem, Caralibro scores
**99.5%** (MAE 0.053 s), so the weak mix number is mostly a mix-audio artifact.
A rejected repair always leaves the forced timing in place.

Re-run the measurement:

```bash
python tools/eval_alignment.py --engine forced
python tools/eval_alignment.py --engine whisperx
```

Runtime on CPU: roughly 1–2 minutes of alignment per song for the forced engine,
several minutes for WhisperX. Details in
[docs/superpowers/specs/2026-10-24-lyrics-alignment-results.md](docs/superpowers/specs/2026-10-24-lyrics-alignment-results.md).

---

## Tips

- For long songs (4+ minutes) rendering takes 2–5 minutes. This is normal — FFmpeg is encoding 1080p video.
- Editor audio/background copies and alignment JSONs are kept for 7 days for re-renders, then pruned on the next submission. Rendered files in `outputs/` are pruned after 1 hour.
- Use a **high-resolution background** (at least 1920×1080) for the sharpest results.
- The **outro audio** is great for fade-out tracks, copyright-free outros, or branding sounds.
- LRC timestamps from the web may need slight adjustment if the song has a long intro.

---

## License

MIT — do whatever you want with it. Attribution appreciated but not required.
