"""
Test: verify the _ffmpeg_escape_path fix handles spaces in directory names.

Tests:
  1. Unit-test the escape helper itself.
  2. Verify the drawtext filter string is built with single-quoted paths.
  3. Live ffmpeg smoke-test using the real files already in outputs/.
"""

import subprocess
import sys
import os
from pathlib import Path

# ── make sure we import from the project ──────────────────────────────────────
BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

import processor as proc

PASS = "\033[92m✓ PASS\033[0m"
FAIL = "\033[91m✗ FAIL\033[0m"
failures = []

def ok(name):
    print(f"  {PASS}  {name}")

def fail(name, reason=""):
    print(f"  {FAIL}  {name}")
    if reason:
        print(f"         {reason}")
    failures.append(name)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Unit tests for _ffmpeg_escape_path
# ─────────────────────────────────────────────────────────────────────────────
print("\n[1] Unit tests for _ffmpeg_escape_path")

# Windows path with space and drive letter
path_win = r"C:\Users\admin\Documents\lyrics maker\outputs\test\_watermark.txt"
escaped  = proc._ffmpeg_escape_path(path_win)

if escaped.startswith("'") and escaped.endswith("'"):
    ok("result is single-quoted")
else:
    fail("result is single-quoted", f"got: {escaped}")

if "\\:" in escaped:
    ok("colon is escaped (\\:)")
else:
    fail("colon is escaped", f"got: {escaped}")

if "\\" not in escaped.strip("'").replace("\\:", ""):
    ok("backslashes converted to forward slashes")
else:
    fail("backslashes converted", f"got: {escaped}")

# Path without spaces should still be quoted
path_simple = r"C:\Windows\Fonts\arial.ttf"
escaped_simple = proc._ffmpeg_escape_path(path_simple)
if escaped_simple.startswith("'") and escaped_simple.endswith("'"):
    ok("simple path also gets single-quoted")
else:
    fail("simple path gets single-quoted", f"got: {escaped_simple}")

# Single-quote inside path should be escaped
path_apos = r"C:\Users\o'brien\font.ttf"
escaped_apos = proc._ffmpeg_escape_path(path_apos)
if "\\'" in escaped_apos:
    ok("embedded single-quote is escaped")
else:
    fail("embedded single-quote escaped", f"got: {escaped_apos}")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Verify the drawtext vf string is built correctly
# ─────────────────────────────────────────────────────────────────────────────
print("\n[2] Drawtext filter-string construction")

# Simulate what generate_instrumental_video builds
font_escaped = proc._ffmpeg_escape_path(proc.FONT_REGULAR)
textfile_path = Path(r"C:\Users\admin\Documents\lyrics maker\outputs\IShowSpeed_World_Cup_\_watermark.txt")
textfile_escaped = proc._ffmpeg_escape_path(str(textfile_path))

vf = (
    f"scale=1920:1080,setsar=1"
    f",drawtext=fontfile={font_escaped}:textfile={textfile_escaped}"
    f":fontsize=36:fontcolor=white@0.7:bordercolor=black@0.5:borderw=2"
    f":x=40:y=40"
)

if "'C\\:/Users/admin/Documents/lyrics maker" in vf or \
   "'C\\:/Users/admin/Documents/lyrics" in vf:
    ok("textfile path with space is single-quoted in vf string")
else:
    fail("textfile path with space quoted in vf",
         f"vf snippet: {vf[vf.find('textfile='):vf.find('textfile=')+80]}")

if "fontfile='C\\:/Windows" in vf or "fontfile='C\\:/" in vf:
    ok("fontfile path is single-quoted in vf string")
else:
    fail("fontfile path quoted in vf",
         f"vf snippet: {vf[:vf.find(':textfile=')]}")


# ─────────────────────────────────────────────────────────────────────────────
# 3. Live ffmpeg smoke-test (uses real output files)
# ─────────────────────────────────────────────────────────────────────────────
print("\n[3] Live ffmpeg smoke-test (path with space)")

# Find any image we can use as a quick background
img_candidates = [
    BASE / "test dir" / "out.mp4",   # already an mp4, but we want an image
    BASE / "outputs" / "IShowSpeed_World_Cup_" / "thumbnail.jpg",
]
bg = None
for c in img_candidates:
    if c.exists() and c.suffix.lower() in (".jpg", ".jpeg", ".png"):
        bg = c
        break

audio = BASE / "outputs" / "IShowSpeed_World_Cup_" / "_combined.m4a"
out   = BASE / "outputs" / "IShowSpeed_World_Cup_" / "_test_fix_output.mp4"

if bg is None:
    print(f"  \033[93m⚠ SKIP\033[0m  No suitable background image found for live test")
elif not audio.exists():
    print(f"  \033[93m⚠ SKIP\033[0m  Combined audio not found: {audio}")
else:
    print(f"  Background : {bg}")
    print(f"  Audio      : {audio}")
    print(f"  Output     : {out}")
    try:
        proc.generate_instrumental_video(
            bg_path=bg,
            audio_path=audio,
            output_path=out,
            outro_path=None,
            title="World Cup",
            artist="IShowSpeed",
        )
        if out.exists() and out.stat().st_size > 0:
            ok(f"generate_instrumental_video succeeded (output={out.stat().st_size:,} bytes)")
            out.unlink()  # clean up test artifact
        else:
            fail("output mp4 was not created or is empty")
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace") if isinstance(e.stderr, bytes) else str(e.stderr)
        fail("generate_instrumental_video raised CalledProcessError",
             stderr[-400:])
    except Exception as e:
        fail("generate_instrumental_video raised unexpected error", str(e))


# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────
print()
if failures:
    print(f"\033[91mFAILED ({len(failures)} test(s)): {', '.join(failures)}\033[0m")
    sys.exit(1)
else:
    print("\033[92mAll tests passed!\033[0m")
    sys.exit(0)
