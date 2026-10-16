"""
Generate one test video per lyric style using test_audio.mp3 + test_lyrics.txt
Output: test_out/video_<style>.mp4

Usage:
    python test_videos.py

Requirements: ffmpeg on PATH, PIL installed
"""

import sys
import os
import subprocess
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from processor import (
    get_media_duration,
    parse_lyrics,
    assign_timestamps,
    generate_ass,
    _ass_escape_path,
    _VIDEO_SCALE,
    _VIDEO_ENCODE,
)

# ── Config ─────────────────────────────────────────────────────────────────────
BASE       = Path(__file__).parent
AUDIO      = BASE / "test_audio.mp3"
LYRICS     = BASE / "test_lyrics.txt"
OUT_DIR    = BASE / "test_out"
BG_IMAGE   = OUT_DIR / "_bg_test.png"

STYLES = ["classic", "karaoke_bar", "cinematic", "minimal", "bold", "neon",
          "two_line", "full_lyrics"]

# Colours used for all test videos
ACTIVE_COLOR   = "#FFFFFF"   # white  — sung / active word
UPCOMING_COLOR = "#FF4444"   # red    — upcoming / un-sung word
SUNG_COLOR     = "#888888"   # grey   — past / sung (used in teleprompter styles)

# ── Helpers ────────────────────────────────────────────────────────────────────

def make_background():
    """Create a 1920x1080 gradient PNG background for test videos."""
    from PIL import Image, ImageDraw
    img  = Image.new("RGB", (1920, 1080))
    draw = ImageDraw.Draw(img)
    # Dark-blue-to-black diagonal gradient
    for y in range(1080):
        r = int(10  + (y / 1080) * 5)
        g = int(10  + (y / 1080) * 10)
        b = int(40  + (y / 1080) * 20)
        draw.line([(0, y), (1920, y)], fill=(r, g, b))
    # Subtle style-label area tint at top
    draw.rectangle([0, 0, 1920, 90], fill=(20, 20, 60))
    img.save(BG_IMAGE)
    print(f"  Created background: {BG_IMAGE}")


def build_timed_lyrics(duration: float) -> list[dict]:
    """Parse test_lyrics.txt and assign timestamps evenly across the audio."""
    parsed = parse_lyrics(str(LYRICS))
    timed  = assign_timestamps(parsed, duration, start_offset=0.5)

    # Add synthetic word-level timestamps so word highlighting works
    for item in timed:
        words = item["text"].split()
        n     = max(len(words), 1)
        seg   = (item["end"] - item["start"]) / n
        item["words"] = [
            {
                "text":  w,
                "start": item["start"] + j * seg,
                "end":   item["start"] + (j + 1) * seg,
            }
            for j, w in enumerate(words)
        ]
    return timed


def draw_style_label(style: str, ass_dir: Path) -> str:
    """
    Return a vf filter string that overlays a style name label using drawtext.
    The label is written to a tmp file to avoid special-char issues.
    """
    label_file = ass_dir / "_label.txt"
    label_file.write_text(f"Style: {style.replace('_', ' ').upper()}", encoding="utf-8")
    lf = str(label_file).replace("\\", "/").replace(":", "\\:")
    return (
        f"drawtext=fontsize=48:fontcolor=white@0.9"
        f":bordercolor=black@0.8:borderw=2"
        f":x=50:y=28:textfile='{lf}'"
    )


def render_style(style: str, timed: list[dict], duration: float) -> Path:
    """Generate the ASS, write tmp files, run ffmpeg, return output path."""
    out_path = OUT_DIR / f"video_{style}.mp4"
    work_dir = OUT_DIR / f"_work_{style}"
    work_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Generate ASS ──────────────────────────────────────────────────────
    ass_content = generate_ass(
        timed,
        title="Test Song",
        artist="Style Test",
        total_duration=duration,
        font_name="Arial",
        font_size=72,
        word_highlight=True,
        active_color=ACTIVE_COLOR,
        upcoming_color=UPCOMING_COLOR,
        sung_color=SUNG_COLOR,
        lyric_style=style,
    )
    ass_path = work_dir / "lyrics.ass"
    ass_path.write_text(ass_content, encoding="utf-8")

    # ── 2. Build ffmpeg filter ────────────────────────────────────────────────
    ass_escaped      = _ass_escape_path(str(ass_path))
    fontsdir_escaped = _ass_escape_path(str(BASE.resolve()))
    label_vf         = draw_style_label(style, work_dir)

    vf = (
        f"{_VIDEO_SCALE}"
        f",subtitles={ass_escaped}:fontsdir={fontsdir_escaped}"
        f",{label_vf}"
    )

    # ── 3. Run ffmpeg ─────────────────────────────────────────────────────────
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1",          # still image looped for audio duration
        "-i", str(BG_IMAGE),
        "-i", str(AUDIO),
        "-vf", vf,
        "-shortest",
    ] + _VIDEO_ENCODE + [str(out_path)]

    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace")[-800:]
        raise RuntimeError(f"ffmpeg failed for style '{style}':\n{err}")

    return out_path


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    OUT_DIR.mkdir(exist_ok=True)

    print("\n" + "=" * 60)
    print("  VIDEO TEST — ALL LYRIC STYLES")
    print("=" * 60)

    # Background
    print("\n[1/3] Generating background image...")
    make_background()

    # Timed lyrics
    print("\n[2/3] Building timed lyrics from test_audio.mp3...")
    duration = get_media_duration(str(AUDIO))
    timed    = build_timed_lyrics(duration)
    print(f"  Audio duration : {duration:.2f}s")
    print(f"  Lyric lines    : {len(timed)}")
    for item in timed:
        print(f"    [{item['start']:.2f}s – {item['end']:.2f}s]  {item['text']}")

    # Render each style
    print(f"\n[3/3] Rendering {len(STYLES)} videos...\n")
    passed, failed = [], []

    for style in STYLES:
        print(f"  -- {style.upper()} " + "-" * (44 - len(style)))
        try:
            out = render_style(style, timed, duration)
            size_kb = out.stat().st_size // 1024
            print(f"     OK  ->  {out.name}  ({size_kb} KB)")
            passed.append(style)
        except Exception as e:
            print(f"     FAILED: {e}")
            traceback.print_exc()
            failed.append(style)
        print()

    # Summary
    print("=" * 60)
    print(f"  DONE: {len(passed)}/{len(STYLES)} videos created in {OUT_DIR}")
    if passed:
        print("\n  Successful:")
        for s in passed:
            p = OUT_DIR / f"video_{s}.mp4"
            print(f"    {p}")
    if failed:
        print("\n  Failed:")
        for s in failed:
            print(f"    {s}")
    print("=" * 60 + "\n")

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
