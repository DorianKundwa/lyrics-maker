# 🎵 LyricForge — Lyrics Video Maker

A self-hosted web app that takes your audio, lyrics, background image, and optional outro — and automatically renders:

- **🎬 Lyrics Video** — 1920×1080 MP4 with synced subtitles
- **🎵 Instrumental Video** — Same as above, lyrics-free
- **🖼 YouTube Thumbnail** — 1280×720 JPEG, print-ready

---

## Requirements

| Tool | Version |
|------|---------|
| Python | 3.10+ |
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

---

## Running the Server

```bash
python -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

Then open **http://localhost:8000** in your browser.

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
├── app.py           ← FastAPI web server & job queue
├── processor.py     ← FFmpeg orchestration, ASS subtitles, thumbnail
├── requirements.txt
├── sample.lrc       ← Example LRC file
├── static/
│   └── index.html   ← Web UI
├── uploads/         ← Temporary upload storage (auto-created)
└── outputs/         ← Rendered output files (auto-created)
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

## Tips

- For long songs (4+ minutes) rendering takes 2–5 minutes. This is normal — FFmpeg is encoding 1080p video.
- Use a **high-resolution background** (at least 1920×1080) for the sharpest results.
- The **outro audio** is great for fade-out tracks, copyright-free outros, or branding sounds.
- LRC timestamps from the web may need slight adjustment if the song has a long intro.

---

## License

MIT — do whatever you want with it. Attribution appreciated but not required.
