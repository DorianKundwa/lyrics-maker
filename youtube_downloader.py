"""
Download the audio of a single YouTube video as MP3 using yt-dlp.

Requires FFmpeg on PATH (yt-dlp uses it to convert the audio to MP3).
For personal use only: YouTube's Terms of Service restrict downloading, and
the UI shows a notice to that effect.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.utils import DownloadError

ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}
MAX_DURATION_SECONDS = 15 * 60
MP3_BITRATE = "192"


class YoutubeError(Exception):
    """A user-facing error (bad link, too long, download or conversion failed)."""


def validate_url(url: str) -> str:
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or (parsed.hostname or "") not in ALLOWED_HOSTS:
        raise YoutubeError("Only YouTube links (youtube.com, youtu.be) are supported.")
    return url


def _safe_stem(name: str) -> str:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(" .")
    return stem[:120] or "audio"


def download_mp3(url: str, out_dir: Path) -> dict:
    """
    Download the audio of one video into out_dir as an MP3.

    Returns {"path", "filename", "title", "artist", "duration"}.
    Raises YoutubeError with a message suitable for the UI.
    """
    url = validate_url(url)
    out_dir.mkdir(parents=True, exist_ok=True)

    base_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # The default YouTube web client fails with "page needs to be reloaded" under
        # yt-dlp 2025.10.14 on Python 3.9 (no JS challenge solver available). The
        # android_vr client returns playable formats without one.
        "extractor_args": {"youtube": {"player_client": ["android_vr"]}},
    }
    try:
        with yt_dlp.YoutubeDL({**base_opts, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        raise YoutubeError(_clean_message(exc)) from exc

    if not info or info.get("_type") == "playlist":
        raise YoutubeError("Link must point to a single video, not a playlist.")
    if info.get("is_live"):
        raise YoutubeError("Live streams can't be downloaded.")

    duration = int(info.get("duration") or 0)
    if duration <= 0:
        raise YoutubeError("Could not determine the video length.")
    if duration > MAX_DURATION_SECONDS:
        raise YoutubeError(
            f"Video is {duration // 60} min long. The limit is {MAX_DURATION_SECONDS // 60} min."
        )

    video_id = info["id"]
    opts = {
        **base_opts,
        "format": "bestaudio/best",
        "outtmpl": str(out_dir / f"{video_id}.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": MP3_BITRATE,
        }],
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except DownloadError as exc:
        raise YoutubeError(_clean_message(exc)) from exc

    mp3_path = out_dir / f"{video_id}.mp3"
    if not mp3_path.exists():
        raise YoutubeError("Conversion to MP3 failed. Check that FFmpeg is installed.")

    title = (info.get("title") or "").strip()
    artist = (info.get("artist") or "").strip()
    return {
        "path": mp3_path,
        "filename": _safe_stem(title or video_id) + ".mp3",
        "title": title,
        "artist": artist,
        "duration": duration,
    }


def _clean_message(exc: Exception) -> str:
    msg = str(exc).replace("ERROR: ", "", 1).strip()
    return msg[:300] or "Download failed."
