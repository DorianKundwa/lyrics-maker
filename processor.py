"""
Lyrics Video Processor
Handles: LRC parsing, ASS subtitle generation, video rendering, thumbnail creation
"""

from __future__ import annotations

import subprocess
import re
import os
import sys
import json
import math
import mimetypes
import textwrap
from functools import lru_cache
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

if sys.platform == "win32":
    FONT_BOLD = "C:/Windows/Fonts/arialbd.ttf"
    FONT_REGULAR = "C:/Windows/Fonts/arial.ttf"
elif sys.platform == "darwin":
    FONT_BOLD = "/Library/Fonts/Arial Bold.ttf"
    FONT_REGULAR = "/Library/Fonts/Arial.ttf"
else:
    FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


# ─── Helpers ────────────────────────────────────────────────────────────────

def seconds_to_ass(t: float) -> str:
    """Convert float seconds → ASS timestamp  H:MM:SS.cc

    Rounding the whole value in centiseconds (instead of the fractional part)
    keeps the result valid: 5.995 s becomes 6.00 s, never the malformed 5.100.
    """
    total_cs = max(0, int(round(float(t) * 100)))
    h, rem = divmod(total_cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def get_media_duration(path: str | Path) -> float:
    """Return duration of audio/video file in seconds."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
        capture_output=True, text=True, check=True, encoding="utf-8", errors="replace"
    )
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def concat_audio(files: list[Path], output: Path) -> Path:
    """Concatenate a list of audio files using ffmpeg concat filter to handle mixed formats."""
    if not files:
        return output
    if len(files) == 1:
        return files[0]
        
    cmd = ["ffmpeg", "-y"]
    filter_complex = ""
    for i, p in enumerate(files):
        cmd.extend(["-i", str(p)])
        filter_complex += f"[{i}:a]"
    filter_complex += f"concat=n={len(files)}:v=0:a=1[outa]"
    
    cmd.extend([
        "-filter_complex", filter_complex,
        "-map", "[outa]",
        "-c:a", "aac",
        "-b:a", "192k",
        str(output)
    ])
    subprocess.run(cmd, check=True, capture_output=True)
    return output


# ─── Lyrics Parsing ─────────────────────────────────────────────────────────

# Genius section header pattern: [Verse 1: Artist], [Chorus], [Hook], [Bridge], etc.
_GENIUS_SECTION_RE = re.compile(
    r"\["
    r"(?:Verse|Chorus|Hook|Bridge|Pre-Chorus|Outro|Intro|Interlude|Refrain|Skit|Spoken|Breakdown|Drop|Ad-?Libs?|Instrumental|Coda|Transition|Repeat|Tag|Part|Section)[^\]]*"
    r"\]",
    re.IGNORECASE,
)

# A whole line in square brackets is a section header in any language (Intro, Verso,
# Coro, Puente, Outro, ...). Sung lyrics never sit alone inside [].
_BRACKET_LINE_RE = re.compile(r"^\s*\[[^\]]*\]\s*$")

# Genius page title line, e.g. [Letra de "Dame Hombro"] or Lyrics for "Song".
_GENIUS_TITLE_RE = re.compile(r"^\s*\[?\s*(?:Letra de|Lyrics for|Lyrics)\b.*$", re.IGNORECASE)

# Genius uses (...) for annotations: crowd ad-libs, sound effects, stage directions,
# repeats like (x4), featured artist credits. None of them are sung lyrics.
_PAREN_ANNOTATION_RE = re.compile(r"\([^)]*\)")


def _clean_genius_line(line: str) -> str:
    """Strip Genius structural annotations ([Chorus], (Chay, chay)) from a lyric line."""
    line = _GENIUS_SECTION_RE.sub("", line)
    line = _PAREN_ANNOTATION_RE.sub("", line)
    return " ".join(line.split())


# LRC time tags: [mm:ss], [mm:ss.xx] or [mm:ss.xxx]. `[offset:±ms]` shifts
# every timestamp and is honoured by parse_lyrics.
_LRC_TIME_RE = re.compile(r"\[(\d+):(\d{1,2})(?:[.:](\d{1,6}))?\]")
_LRC_OFFSET_RE = re.compile(r"\[offset:\s*([+-]?\d+)\s*\]", re.IGNORECASE)


def _lrc_fraction_seconds(fraction: str | None) -> float:
    """LRC fractions are a decimal part of a second, whatever the digit count.

    ``[00:05.5]``, ``[00:05.50]`` and ``[00:05.500]`` all mean 5.5 s. Treating
    the digits as centiseconds made millisecond-precision files (common from
    some taggers) run late: ``.500`` was read as five whole seconds.
    """
    if not fraction:
        return 0.0
    return int(fraction) / (10 ** len(fraction))


def parse_lyrics(lyrics_path: str | Path) -> list[tuple[float | None, str]]:
    """
    Parse lyrics file.
    Supports:
      • LRC format  [mm:ss.xx] text — any fraction length, several timestamps
        per line, and a [offset:±ms] session offset
      • SRT-style   timestamp --> timestamp\\ntext
      • Plain text  (no timestamps, auto-timed later)
    Returns list of (timestamp_or_None, text).
    """
    text = Path(lyrics_path).read_text(encoding="utf-8", errors="replace")

    offset = 0.0
    offset_match = _LRC_OFFSET_RE.search(text)
    if offset_match:
        try:
            offset = int(offset_match.group(1)) / 1000.0
        except ValueError:
            offset = 0.0

    # LRC. A line may carry more than one timestamp ("[00:10][00:42]chorus"),
    # and lines without a time tag (metadata such as [ar:...]) are ignored.
    timed_lines: list[tuple[float, str]] = []
    for raw in text.splitlines():
        tags = list(_LRC_TIME_RE.finditer(raw))
        if not tags:
            continue
        body = raw[tags[-1].end():].strip()
        cleaned = _clean_genius_line(body)
        if not cleaned:
            continue
        for tag in tags:
            mm, ss, fraction = tag.group(1), tag.group(2), tag.group(3)
            ts = int(mm) * 60 + int(ss) + _lrc_fraction_seconds(fraction) + offset
            timed_lines.append((max(0.0, round(ts, 3)), cleaned))

    if timed_lines:
        return sorted(timed_lines, key=lambda item: item[0])

    # Plain text – strip blank lines and section headers like [Chorus], [Verse 1], etc.
    lines = []
    for l in text.splitlines():
        s = l.strip()
        if not s:
            continue
        if _BRACKET_LINE_RE.match(s) or _GENIUS_TITLE_RE.match(s):
            continue
        cleaned = _clean_genius_line(s)
        if cleaned:
            lines.append(cleaned)
    return [(None, line) for line in lines]


def _anchor_to_vocal_onset(
    timed: list[dict],
    vocal_onset: float,
    tolerance: float = 1.0,
    max_shift: float = 3.0,
) -> list[dict]:
    """
    If the aligner starts the first lyric well after the vocals really begin, the
    whole timeline is late by that same amount. Shift every line and word by it so
    the lyrics stay in sync with the audio.

    The shift is capped at `max_shift` seconds: the onset detector works on the
    stem's own loudness, so a breath or backing vocal early in the track can make
    it report an onset far before the singing, and pulling the whole timeline back
    by that much would break a song that was already aligned.
    """
    if not timed:
        return timed
    delta = float(vocal_onset) - float(timed[0]["start"])
    if delta >= -tolerance:
        # Only pull the timeline earlier when the aligner started it late by more than tolerance.
        return timed
    if -delta > max_shift:
        print(
            f"Vocal onset {float(vocal_onset):.2f}s is {-delta:.2f}s before the first "
            f"lyric line; too far to trust (max {max_shift:.1f}s), keeping alignment as is."
        )
        return timed
    shifted = []
    for item in timed:
        new = dict(item)
        new["start"] = max(0.0, float(item["start"]) + delta)
        new["end"] = max(new["start"] + 0.1, float(item["end"]) + delta)
        if item.get("words"):
            new["words"] = [
                {**w, "start": max(0.0, w["start"] + delta), "end": max(0.0, w["end"] + delta)}
                for w in item["words"]
            ]
        shifted.append(new)
    return shifted


def assign_timestamps(
    lyrics: list[tuple[float | None, str]],
    total_duration: float,
    start_offset: float = 2.0,
) -> list[dict]:
    """
    Returns list of dicts with start, end, text, words.
    If timestamps already exist they are used directly.
    Otherwise the duration is split evenly with a brief lead-in.
    """
    if lyrics and lyrics[0][0] is not None:
        # already timed
        timed = [(t, txt) for t, txt in lyrics]
    else:
        n = len(lyrics)
        available = total_duration - start_offset
        interval = available / max(n, 1)
        timed = [(start_offset + i * interval, txt) for i, (_, txt) in enumerate(lyrics)]

    result = []
    for i, (start, txt) in enumerate(timed):
        end = timed[i + 1][0] if i + 1 < len(timed) else total_duration
        result.append({"start": start, "end": end, "text": txt, "words": []})
    return result


# ─── ASS Subtitle Generation ────────────────────────────────────────────────

# Style presets: each entry drives the ASS [V4+ Styles] header and per-line tags.
_LYRIC_STYLES: dict[str, dict] = {
    "classic": {
        "bold": -1, "italic": 0, "border_style": 1,
        "outline": 4, "shadow": 3, "alignment": 5,
        "margin_l": 60, "margin_r": 60, "margin_v": 0,
        "back_colour": "&HAA000000",
        "tags": r"\fad(150,300)",
        "font_scale": 1.0, "neon": False,
    },
    "karaoke_bar": {
        "bold": -1, "italic": 0, "border_style": 3,
        "outline": 15, "shadow": 0, "alignment": 2,
        "margin_l": 0, "margin_r": 0, "margin_v": 60,
        "back_colour": "&HCC000000",
        "tags": r"\fad(100,200)",
        "font_scale": 1.0, "neon": False,
    },
    "cinematic": {
        "bold": 0, "italic": 1, "border_style": 1,
        "outline": 0, "shadow": 14, "alignment": 5,
        "margin_l": 80, "margin_r": 80, "margin_v": 0,
        "back_colour": "&H99000000",
        "tags": None,  # generated per-line with \move
        "font_scale": 1.0, "neon": False,
    },
    "minimal": {
        "bold": 0, "italic": 0, "border_style": 1,
        "outline": 0, "shadow": 2, "alignment": 2,
        "margin_l": 60, "margin_r": 60, "margin_v": 40,
        "back_colour": "&H88000000",
        "tags": r"\fad(200,400)",
        "font_scale": 0.65, "neon": False,
    },
    "bold": {
        "bold": -1, "italic": 0, "border_style": 1,
        "outline": 8, "shadow": 0, "alignment": 5,
        "margin_l": 60, "margin_r": 60, "margin_v": 0,
        "back_colour": "&H00000000",
        "tags": r"\fad(60,120)",
        "font_scale": 1.0, "neon": False,
    },
    "neon": {
        "bold": -1, "italic": 0, "border_style": 1,
        "outline": 0, "shadow": 0, "alignment": 5,
        "margin_l": 60, "margin_r": 60, "margin_v": 0,
        "back_colour": "&H00000000",
        "tags": r"\fad(200,400)",
        "font_scale": 1.0, "neon": True,  # two dialogue layers per line
    },
    "two_line": {
        "bold": -1, "italic": 0, "border_style": 1,
        "outline": 3, "shadow": 2, "alignment": 5,
        "margin_l": 60, "margin_r": 60, "margin_v": 0,
        "back_colour": "&HAA000000",
        "tags": None,  # custom multi-line layout
        "font_scale": 0.85, "neon": False, "multi_line": "two",
    },
    "full_lyrics": {
        "bold": 0, "italic": 0, "border_style": 1,
        "outline": 2, "shadow": 2, "alignment": 5,
        "margin_l": 60, "margin_r": 60, "margin_v": 0,
        "back_colour": "&HAA000000",
        "tags": None,  # custom multi-line layout
        "font_scale": 0.70, "neon": False, "multi_line": "full",
    },
}


def _karaoke_units(words: list, text: str) -> list[dict] | None:
    """Group aligned tokens back onto the words exactly as they are written.

    Aligners split hyphenated words ("After-party" becomes "After", "party")
    and may strip punctuation. Karaoke must display the original line, so this
    consumes one or more aligned tokens for each written word. It returns None
    when the token stream no longer spells the line, for example after the
    editor rewrites it.
    """
    def _norm(value: str) -> str:
        return re.sub(r"[\W_]+", "", str(value or ""), flags=re.UNICODE).lower()

    tokens = str(text or "").split()
    if not words or not tokens:
        return None

    units: list[dict] = []
    word_index = 0
    for token in tokens:
        target = _norm(token)
        if not target:
            if units:
                units[-1]["text"] += token
            continue
        if word_index >= len(words):
            return None

        accumulated = ""
        start = words[word_index].get("start")
        end = words[word_index].get("end")
        consumed = 0
        while word_index < len(words):
            part = words[word_index]
            accumulated += _norm(part.get("text", ""))
            end = part.get("end", end)
            word_index += 1
            consumed += 1
            if accumulated == target:
                break
            if len(accumulated) >= len(target):
                return None

        if not consumed or accumulated != target or start is None or end is None:
            return None
        units.append({"text": token, "start": start, "end": end})

    return units if word_index == len(words) else None


def _clean_word_timings(item: dict) -> dict:
    """Repair word timings that would make karaoke jump or stall.

    A single word can consume most of a line when the aligner anchors it to the
    wrong repetition, leaving every later word bunched at the end. When one
    inter-word gap is both longer than two seconds and more than half the line,
    the line is moved onto its densest cluster of words and spread evenly there.
    Normal spacing is left untouched, and overlapping or out-of-range timings are
    clamped in place.
    """
    words = item.get("words") or []
    if len(words) < 2:
        return item
    try:
        line_start = float(item["start"])
        line_end = float(item["end"])
    except (KeyError, TypeError, ValueError):
        return item
    if not (math.isfinite(line_start) and math.isfinite(line_end)) or line_end <= line_start:
        return item

    cleaned: list[dict] = []
    previous_start = line_start
    changed = False
    for word in words:
        try:
            start = float(word["start"])
            end = float(word["end"])
        except (KeyError, TypeError, ValueError):
            return item
        if not (math.isfinite(start) and math.isfinite(end)):
            return item

        start = min(max(start, line_start), line_end)
        if start < previous_start:
            start = previous_start
            changed = True
        if start > previous_start:
            previous_start = start
        end = min(max(end, start + 0.05), line_end)
        cleaned.append({**word, "start": round(start, 3), "end": round(end, 3)})

    for current, following in zip(cleaned, cleaned[1:]):
        if current["end"] > following["start"]:
            current["end"] = following["start"]
            changed = True

    starts = [word["start"] for word in cleaned]
    gaps = [later - earlier for earlier, later in zip(starts, starts[1:])]
    if not gaps or max(gaps) <= max(2.0, (line_end - line_start) * 0.5):
        return {**item, "words": cleaned} if changed else item

    # Split the starts wherever the singer pauses; the largest group is the
    # passage that actually belongs to this line. A lone early word is usually
    # a match against the intro or a repeated ad-lib, not the line's start.
    clusters: list[tuple[int, int]] = []
    cluster_start = 0
    for index, gap in enumerate(gaps, start=1):
        if gap > 1.5:
            clusters.append((cluster_start, index))
            cluster_start = index
    clusters.append((cluster_start, len(cleaned)))
    first, last = max(clusters, key=lambda pair: (pair[1] - pair[0], -pair[0]))

    if last - first >= 2:
        target_start = cleaned[first]["start"]
        cluster_gaps = sorted(gaps[first:last - 1])
        tail = cluster_gaps[len(cluster_gaps) // 2] if cluster_gaps else 0.2
        target_end = min(line_end, cleaned[last - 1]["start"] + max(0.1, tail))
    else:
        # No dense passage to anchor on: keep the original line bounds.
        target_start, target_end = line_start, line_end
    if target_end <= target_start:
        target_start, target_end = line_start, line_end

    step = (target_end - target_start) / len(cleaned)
    for index, word in enumerate(cleaned):
        word["start"] = round(target_start + index * step, 3)
        word["end"] = round(target_start + (index + 1) * step, 3)
    changed = True

    if not changed:
        return item
    return {
        **item,
        "start": round(target_start, 3),
        "end": round(target_end, 3),
        "words": cleaned,
    }


def _shift_position_tags(dialogue: str, dx: int, dy: int) -> str:
    """Move every \\pos()/\\move() in one dialogue line by (dx, dy) pixels."""
    if not dx and not dy:
        return dialogue

    def shift_pos(match):
        return f"\\pos({int(match.group(1)) + dx},{int(match.group(2)) + dy})"

    def shift_move(match):
        x1 = int(match.group(1)) + dx
        y1 = int(match.group(2)) + dy
        x2 = int(match.group(3)) + dx
        y2 = int(match.group(4)) + dy
        return f"\\move({x1},{y1},{x2},{y2}{match.group(5)})"

    dialogue = re.sub(r"\\pos\((-?\d+),(-?\d+)\)", shift_pos, dialogue)
    return re.sub(
        r"\\move\((-?\d+),(-?\d+),(-?\d+),(-?\d+)((?:,-?\d+)*)\)",
        shift_move,
        dialogue,
    )


# --- Line fitting ----------------------------------------------------------

# ASS plays at 1920x1080, so a lyric wider than that is measured here and drawn
# at a smaller size instead of being left to libass. An auto-wrapped long line
# puts a second row on top of the neighbouring lyric in the multi-row styles,
# which reads as two lines printed over each other.
_ASS_WIDTH = 1920
_FIT_FLOOR = 0.6   # never squeeze a line below 60 % of the chosen size


@lru_cache(maxsize=64)
def _lyric_font_path(font_name: str, bold: bool) -> str:
    """Font file used to measure lyrics, mirroring the style's font name."""
    name = (font_name or "Arial").strip().lower()
    candidates: list[str] = []
    if name == "edo":
        candidates.append(str(Path(__file__).parent.resolve() / "edo.ttf"))
    elif name == "impact":
        candidates += [
            "C:/Windows/Fonts/impact.ttf",
            "C:/Windows/Fonts/impactbd.ttf",
            "/Library/Fonts/Impact.ttf",
            "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
        ]
    for path in candidates:
        if Path(path).exists():
            return path
    fallback = FONT_BOLD if bold else FONT_REGULAR
    return fallback if Path(fallback).exists() else ""


@lru_cache(maxsize=128)
def _measure_font(font_path: str, size: int):
    try:
        return ImageFont.truetype(font_path, size) if font_path else None
    except Exception:
        return None


def _text_width(text: str, font_path: str, size: int, letter_spacing: int = 2) -> float:
    """Rendered width in ASS pixels, including the style's letter tracking."""
    width = 0.0
    font = _measure_font(font_path, size)
    if font is not None:
        try:
            width = float(font.getlength(text))
        except Exception:
            width = 0.0
    if not width:  # unknown font: fall back to bold-Arial metrics
        width = len(text) * size * 0.6
    return width + letter_spacing * len(text)


def _fit_font_size(
    text: str,
    size: int,
    font_name: str,
    bold: bool,
    max_width: float,
    letter_spacing: int = 2,
) -> int:
    """Largest size at most `size` that keeps `text` on one row inside `max_width`."""
    text = (text or "").strip()
    if not text or max_width <= 0:
        return size
    width = _text_width(text, _lyric_font_path(font_name, bold), size, letter_spacing)
    if width <= max_width:
        return size
    scale = max(_FIT_FLOOR, (max_width / width) * 0.985)
    return max(10, int(size * scale))


def generate_ass(
    timed_lyrics: list[dict],
    title: str = "",
    artist: str = "",
    total_duration: float = 0,
    font_name: str = "Arial",
    font_size: int = 72,
    word_highlight: bool = True,
    active_color: str = "#FFFFFF",
    upcoming_color: str = "#FF0000",
    sung_color: str = "#FFFFFF",
    lyric_style: str = "classic",
) -> str:
    """Build an ASS subtitle file string with optional style preset."""
    sc = _LYRIC_STYLES.get(lyric_style, _LYRIC_STYLES["classic"])
    eff_size    = max(10, int(font_size * sc["font_scale"]))
    primary_col = _hex_to_ass(active_color)
    second_col  = _hex_to_ass(upcoming_color)
    sung_col    = _hex_to_ass(sung_color)
    outline_col = "&H00000000"
    back_col    = sc["back_colour"]
    # Line spacing for multi-line styles (based on effective font size)
    spacing     = max(70, int(eff_size * 1.35))
    # Auto-wrapping is only unsafe where rows share the screen: there a
    # wrapped line lands on its neighbour, so those styles get one fitted
    # row instead. Single-position styles keep wrapping at full size.
    multi_row   = bool(sc.get("multi_line"))
    # Widest one-row line this style can draw, allowing for outline and shadow.
    safe_width  = max(
        200.0,
        _ASS_WIDTH - sc["margin_l"] - sc["margin_r"]
        - 2 * max(0, sc["outline"]) - max(0, sc["shadow"]) - 24,
    )

    def fit_tag(text: str) -> str:
        """Per-line font-size override for text wider than the frame."""
        if not multi_row:
            return ""
        fitted = _fit_font_size(text, eff_size, font_name, bool(sc["bold"]), safe_width)
        return f"{{\\fs{fitted}}}" if fitted != eff_size else ""

    # Rows are placed explicitly below, so libass must not wrap a long line
    # onto the next lyric row; fitting keeps every line on a single row.
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: {2 if multi_row else 0}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{eff_size},{primary_col},{second_col},{outline_col},{back_col},{sc["bold"]},{sc["italic"]},0,0,100,100,2,0,{sc["border_style"]},{sc["outline"]},{sc["shadow"]},{sc["alignment"]},{sc["margin_l"]},{sc["margin_r"]},{sc["margin_v"]},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines  = [header]
    # Glow colour for neon layer = upcoming/accent color
    neon_glow_col  = second_col
    multi_mode     = sc.get("multi_line", None)
    # Normalise once so display gaps, previews, and karaoke all use the same
    # corrected word/line timings.
    items = [_clean_word_timings(item) for item in timed_lyrics]

    for i, item in enumerate(items):
        first_line = len(lines)
        start = item["start"]
        end   = item["end"]
        karaoke_end = end
        # For teleprompter styles: extend end to next event start to fill gaps
        # so the screen never goes blank between alignment segments. Karaoke
        # still stops at the line's own end instead of holding its last word
        # through the gap.
        if multi_mode and i + 1 < len(items):
            next_start = items[i + 1]["start"]
            if next_start > end:
                end = next_start

        words = item.get("words", [])

        # ── Build karaoke or plain text body ──────────────────────────────────
        units = _karaoke_units(words, item.get("text", "")) if words and word_highlight else None
        if units:
            # \k: simple instant colour-swap per word. Each word owns the time
            # until the next word starts, so overlapping aligned intervals
            # cannot make the highlight clock run ahead of the song, and gaps
            # do not emit empty \k tags that clear the highlight.
            karaoke_text = ""
            current_time = float(start)
            line_end = max(float(start), float(karaoke_end))
            for index, unit in enumerate(units):
                w_start = min(max(float(unit["start"]), current_time), line_end)
                if index + 1 < len(units):
                    next_start = min(max(float(units[index + 1]["start"]), w_start), line_end)
                else:
                    next_start = line_end
                if w_start > current_time:
                    gap_cs = int(round((w_start - current_time) * 100))
                    if gap_cs > 0:
                        karaoke_text += f"{{\\k{gap_cs}}}"
                duration_cs = max(0, int(round((next_start - w_start) * 100)))
                karaoke_text += f"{{\\k{duration_cs}}}{_ass_escape(unit['text'])} "
                current_time = next_start
            text_body = karaoke_text.strip()
        else:
            text_body = _ass_escape(item.get("text", ""))

        # Fit this line once; karaoke and plain bodies show the same words.
        display_text = " ".join(unit["text"] for unit in units) if units else item.get("text", "")
        line_fit = fit_tag(display_text)

        # ── Per-style intro + outro tags ───────────────────────────────────────
        is_first = (i == 0)
        is_last  = (i == len(items) - 1)

        if lyric_style == "classic":
            # CLASSIC KARAOKE — Static centre position for all lines.
            # Only the very first line fades+drifts in; last line fades out.
            # Middle lines just appear/disappear instantly so words highlight
            # seamlessly from one line to the next with no visual bump.
            if is_first:
                prefix = r"\move(960,555,960,540,0,500)\fad(300,0)"
            elif is_last:
                prefix = r"\pos(960,540)\fad(0,400)"
            else:
                prefix = r"\pos(960,540)"  # snap to position — no drift, no fade

        elif lyric_style == "karaoke_bar":
            # KARAOKE BAR — Mimics a real karaoke machine.
            # The bar slides up from off-screen ONLY on the very first lyric.
            # All subsequent lines just appear at rest so the bar feels
            # persistent (text changes in place, no bouncing).
            # The bar disappears on the last lyric with a quick fade-down.
            if is_first:
                prefix = r"\move(960,1120,960,1020,0,350)"   # slide up once
            elif is_last:
                prefix = r"\pos(960,1020)\fad(0,200)"        # appear then fade out
            else:
                prefix = r"\pos(960,1020)"                   # appear in-place

        elif lyric_style == "cinematic":
            # CINEMATIC — Every line is its own dramatic 'scene'.
            # Text floats up slowly (blur-to-sharp entry) and dissolves.
            # The float IS the style — each line drifts throughout its lifetime.
            if is_first:
                # Longer fade-in for the opening scene
                prefix = r"\move(960,610,960,460,0,9000)\fad(700,650)\blur6\t(0,600,\blur0)"
            elif is_last:
                # Longer fade-out — the final scene lingers
                prefix = r"\move(960,610,960,460,0,9000)\fad(450,1000)\blur6\t(0,600,\blur0)"
            else:
                prefix = r"\move(960,610,960,460,0,9000)\fad(450,650)\blur6\t(0,600,\blur0)"

        elif lyric_style == "minimal":
            # MINIMAL — Understated. Pure soft crossfade, no movement.
            # Text stays perfectly still; only opacity transitions.
            # First line opens gently; last line lingers and dissolves.
            if is_first:
                prefix = r"\pos(960,900)\fad(700,500)"
            elif is_last:
                prefix = r"\pos(960,900)\fad(400,1000)"
            else:
                prefix = r"\pos(960,900)\fad(400,650)"

        elif lyric_style == "bold":
            # BOLD POP — Aggressive instant snap on EVERY line.
            # Slides in from slightly left + scale-bounce (115%→100%) in 80ms.
            # Zero fade in or out — pure hard cut on every transition.
            prefix = r"\move(900,540,960,540,0,80)\fscx115\fscy115\t(0,80,\fscx100\fscy100)"

        else:
            # neon and multi-line styles handled in emit blocks below
            prefix = sc.get("tags") or r"\fad(150,300)"


        line_text = f"{{{prefix}}}{line_fit}{text_body}"

        # ── Emit dialogue line(s) ─────────────────────────────────────────
        if sc["neon"]:
            # ── NEON: pulsing glow layer (accent colour) + crisp karaoke layer ─────
            plain = _ass_escape(item.get("text", ""))  # plain text for glow
            # Fade timing: first line longer intro, last line longer outro
            if is_first:
                fad_tag = r"\fad(500,600)"
            elif is_last:
                fad_tag = r"\fad(350,800)"
            else:
                fad_tag = r"\fad(350,550)"
            # Layer 0 — glow: accent-coloured blur that pulses then fades
            glow = (
                f"{{\\blur14\\1c{neon_glow_col}"
                f"\\t(0,800,\\blur24)\\t(800,1600,\\blur16)"
                f"{fad_tag}}}{line_fit}{plain}"
            )
            lines.append(
                f"Dialogue: 0,{seconds_to_ass(start)},{seconds_to_ass(end)},"
                f"Default,,0,0,0,,{glow}"
            )
            # Layer 1 — sharp karaoke text: word highlighting + fade
            sharp = f"{{\\blur0{fad_tag}}}{line_fit}{text_body}"
            lines.append(
                f"Dialogue: 1,{seconds_to_ass(start)},{seconds_to_ass(end)},"
                f"Default,,0,0,0,,{sharp}"
            )


        elif multi_mode == "two":
            # ── TWO-LINE: scroll up; graceful fade-out on last line ─────────────
            cur_y = 470
            tr_ms = 250

            if i == 0:
                # First line: both fade into position
                t_cur = rf"\pos(960,{cur_y})\fad(300,0)"
                lines.append(
                    f"Dialogue: 1,{seconds_to_ass(start)},{seconds_to_ass(end)},"
                    f"Default,,0,0,0,,{{{t_cur}}}{line_fit}{text_body}"
                )
                if 1 < len(items):
                    nxt   = _ass_escape(items[1]["text"])
                    next_fit = fit_tag(items[1]["text"])
                    t_nxt = rf"\pos(960,{cur_y + spacing})\fad(300,0)\1c{second_col}\alpha&H55&"
                    lines.append(
                        f"Dialogue: 0,{seconds_to_ass(start)},{seconds_to_ass(end)},"
                        f"Default,,0,0,0,,{{{t_nxt}}}{next_fit}{nxt}"
                    )
            elif is_last:
                # Last line: slides up into position, then fades out gracefully
                t_cur = rf"\move(960,{cur_y + spacing},960,{cur_y},0,{tr_ms})\alpha&H55&\t(0,{tr_ms},\alpha&H00&)\fad(0,450)"
                lines.append(
                    f"Dialogue: 1,{seconds_to_ass(start)},{seconds_to_ass(end)},"
                    f"Default,,0,0,0,,{{{t_cur}}}{line_fit}{text_body}"
                )
            else:
                # Middle lines: current slides up and brightens
                t_cur = rf"\move(960,{cur_y + spacing},960,{cur_y},0,{tr_ms})\alpha&H55&\t(0,{tr_ms},\alpha&H00&)"
                lines.append(
                    f"Dialogue: 1,{seconds_to_ass(start)},{seconds_to_ass(end)},"
                    f"Default,,0,0,0,,{{{t_cur}}}{line_fit}{text_body}"
                )
                # Next line slides in from below
                nxt   = _ass_escape(items[i + 1]["text"])
                next_fit = fit_tag(items[i + 1]["text"])
                t_nxt = rf"\move(960,{cur_y + 2*spacing},960,{cur_y + spacing},0,{tr_ms})\1c{second_col}\alpha&HBB&\t(0,{tr_ms},\alpha&H55&)"
                lines.append(
                    f"Dialogue: 0,{seconds_to_ass(start)},{seconds_to_ass(end)},"
                    f"Default,,0,0,0,,{{{t_nxt}}}{next_fit}{nxt}"
                )

        elif multi_mode == "full":
            # ── FULL LYRICS: teleprompter scroll; graceful start + last-line fade ──
            cur_y  = 490
            tr_ms  = 250
            window = [(i - 1, "past"), (i, "cur"), (i + 1, "next"), (i + 2, "far")]

            for slot_idx, (idx, role) in enumerate(window):
                if idx < 0 or idx >= len(items):
                    continue
                y_this = cur_y + (slot_idx - 1) * spacing
                y_from = y_this + spacing
                entry  = items[idx]
                layer  = 1 if role == "cur" else 0

                if role == "cur":
                    body = text_body
                    if i == 0:
                        # First line fades in from position
                        tags = rf"\pos(960,{y_this})\fad(400,0)"
                    elif is_last:
                        # Last line scrolls in then fades out gracefully
                        tags = rf"\move(960,{y_from},960,{y_this},0,{tr_ms})\alpha&H55&\t(0,{tr_ms},\alpha&H00&)\fad(0,500)"
                    else:
                        tags = rf"\move(960,{y_from},960,{y_this},0,{tr_ms})\alpha&H55&\t(0,{tr_ms},\alpha&H00&)"

                elif role == "past":
                    body = _ass_escape(entry["text"])
                    tags = rf"\move(960,{y_from},960,{y_this},0,{tr_ms})\1c{sung_col}\t(0,{tr_ms},\alpha&H88&)"

                elif role == "next":
                    body = _ass_escape(entry["text"])
                    if i == 0:
                        tags = rf"\pos(960,{y_this})\fad(400,0)\1c{second_col}\alpha&H55&"
                    else:
                        tags = rf"\move(960,{y_from},960,{y_this},0,{tr_ms})\1c{second_col}\alpha&H99&\t(0,{tr_ms},\alpha&H55&)"

                else:  # far
                    body = _ass_escape(entry["text"])
                    if i == 0:
                        tags = rf"\pos(960,{y_this})\fad(400,0)\1c{second_col}\alpha&H99&"
                    else:
                        tags = rf"\move(960,{y_from},960,{y_this},0,{tr_ms})\1c{second_col}\alpha&HCC&\t(0,{tr_ms},\alpha&H99&)"

                body_fit = line_fit if role == "cur" else fit_tag(entry["text"])
                lines.append(
                    f"Dialogue: {layer},{seconds_to_ass(start)},{seconds_to_ass(end)},"
                    f"Default,,0,0,0,,{{{tags}}}{body_fit}{body}"
                )

        else:
            lines.append(
                f"Dialogue: 1,{seconds_to_ass(start)},{seconds_to_ass(end)},"
                f"Default,,0,0,0,,{line_text}"
            )

        # The editor stores a per-segment offset; +y_offset moves text up.
        dx = int(item.get("x_offset", 0) or 0)
        dy = -int(item.get("y_offset", 0) or 0)
        if dx or dy:
            lines[first_line:] = [
                _shift_position_tags(entry, dx, dy) for entry in lines[first_line:]
            ]

    return "\n".join(lines)


def _ass_escape(text: str) -> str:
    """Minimal ASS special-char escaping."""
    return text.replace("{", "\\{").replace("}", "\\}")


def _hex_to_ass(hex_color: str) -> str:
    """Convert #RRGGBB (or #RGB) hex color to ASS &H00BBGGRR format."""
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"&H00{b:02X}{g:02X}{r:02X}"


# ─── Thumbnail Generation ────────────────────────────────────────────────────

def generate_thumbnail(
    bg_path: str | Path,
    output_path: str | Path,
    title: str = "",
    artist: str = "",
    size: tuple[int, int] = (1280, 720),
    font_name: str = "Arial",
) -> None:
    """Create a YouTube thumbnail at 1280×720."""
    img = Image.open(bg_path).convert("RGB")

    # Smart crop to 16:9
    w, h = img.size
    target_ratio = size[0] / size[1]
    current_ratio = w / h
    if current_ratio > target_ratio:
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        img = img.crop((left, 0, left + new_w, h))
    else:
        new_h = int(w / target_ratio)
        top = (h - new_h) // 2
        img = img.crop((0, top, w, top + new_h))

    img = img.resize(size, Image.LANCZOS)

    draw = ImageDraw.Draw(img)

    def load_font(path, size):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            return ImageFont.load_default()

    # Determine font paths
    if font_name == "Edo":
        font_path_bold = str(Path(__file__).parent.resolve() / "edo.ttf")
        font_path_reg = font_path_bold
    else:
        font_path_bold = FONT_BOLD
        font_path_reg = FONT_REGULAR

    cx = size[0] // 2
    font_size_title = 0

    # Title (large, bold, centered)
    if title:
        max_width = size[0] - 60
        font_size_title = 280
        font_title = load_font(font_path_bold, font_size_title)
        
        while font_size_title > 40:
            bbox = draw.textbbox((0, 0), title, font=font_title)
            if (bbox[2] - bbox[0]) <= max_width:
                break
            font_size_title -= 10
            font_title = load_font(font_path_bold, font_size_title)

        ty = size[1] // 2 - (60 if artist else 0)
        
        # Drop shadow
        draw.text((cx + 12, ty + 12), title, font=font_title, fill=(0, 0, 0, 200), anchor="mm")
        # Main text with outline
        draw.text((cx, ty), title, font=font_title, fill=(255, 255, 255, 255), anchor="mm", stroke_width=6, stroke_fill=(0, 0, 0, 255))

    # Artist name (smaller, below title or centered if no title)
    if artist:
        max_width = size[0] - 80
        font_size_artist = 120
        font_artist = load_font(font_path_reg, font_size_artist)
        
        while font_size_artist > 30:
            bbox = draw.textbbox((0, 0), artist, font=font_artist)
            if (bbox[2] - bbox[0]) <= max_width:
                break
            font_size_artist -= 5
            font_artist = load_font(font_path_reg, font_size_artist)

        if title:
            ay = size[1] // 2 + (font_size_title // 2) + 10
        else:
            ay = size[1] // 2
        
        # Drop shadow
        draw.text((cx + 6, ay + 6), artist, font=font_artist, fill=(0, 0, 0, 200), anchor="mm")
        # Main text with outline
        draw.text((cx, ay), artist, font=font_artist, fill=(255, 255, 255, 255), anchor="mm", stroke_width=4, stroke_fill=(0, 0, 0, 255))

    img = img.convert("RGB")
    img.save(str(output_path), "JPEG", quality=95, optimize=True)


# ─── Video Generation ────────────────────────────────────────────────────────

_VIDEO_SCALE = "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,setsar=1"

_FFMPEG_BASE_IMAGE = [
    "ffmpeg", "-y",
    "-loop", "1",          # still image → loop
]

_FFMPEG_BASE_VIDEO = [
    "ffmpeg", "-y",
    "-stream_loop", "-1",  # video → loop
]

def _get_ffmpeg_base(bg_path: str | Path) -> list[str]:
    mime, _ = mimetypes.guess_type(str(bg_path))
    if mime and mime.startswith('video/'):
        return _FFMPEG_BASE_VIDEO
    return _FFMPEG_BASE_IMAGE

_VIDEO_ENCODE = [
    "-c:v", "libx264",
    "-preset", "ultrafast",
    "-tune", "stillimage",
    "-crf", "23",
    "-c:a", "aac",
    "-b:a", "192k",
    "-pix_fmt", "yuv420p",
    "-movflags", "+faststart",
]


def _render_threads() -> int:
    """Threads per FFmpeg encode, budgeted across the concurrently running songs.

    Three jobs encoding 1080p with the default thread count fight over the same
    cores and end up slower than one at a time, so the budget is divided.
    """
    try:
        jobs = max(1, int(os.environ.get("LYRIC_JOB_CONCURRENCY", "3")))
    except ValueError:
        jobs = 3
    return max(1, (os.cpu_count() or 4) // jobs)


def _video_encode_args() -> list[str]:
    """Encode flags plus the per-encode thread cap."""
    return [*_VIDEO_ENCODE, "-threads", str(_render_threads())]


def _build_audio(work_dir: Path, main_audio: Path, outro_audio: Path | None) -> Path:
    if outro_audio:
        combined = work_dir / "_combined.m4a"
        return concat_audio([main_audio, outro_audio], combined)
    return main_audio


def generate_instrumental_video(
    bg_path: str | Path,
    audio_path: str | Path,
    output_path: str | Path,
    outro_path: str | Path | None = None,
    title: str = "",
    artist: str = "",
) -> None:
    """Background image + audio (no lyrics overlay)."""
    work_dir = Path(output_path).parent
    audio = _build_audio(work_dir, Path(audio_path), Path(outro_path) if outro_path else None)

    # Optional title watermark using drawtext with textfile= to avoid special-char escaping issues
    vf = _VIDEO_SCALE
    textfile_path = None
    if title or artist:
        label = f"{artist} \u2014 {title}" if (artist and title) else (title or artist)
        textfile_path = work_dir / "_watermark.txt"
        textfile_path.write_text(label, encoding="utf-8")
        textfile_escaped = _ffmpeg_escape_path(str(textfile_path))
        fontfile_escaped = _ffmpeg_escape_path(FONT_REGULAR)
        vf += (
            f",drawtext=fontfile={fontfile_escaped}:textfile={textfile_escaped}"
            f":fontsize=36:fontcolor=white@0.7:bordercolor=black@0.5:borderw=2"
            f":x=40:y=40"
        )

    cmd = _get_ffmpeg_base(bg_path) + [
        "-i", str(bg_path),
        "-i", str(audio),
        "-vf", vf,
        "-shortest",
    ] + _video_encode_args() + [str(output_path)]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd,
            output=result.stdout,
            stderr=result.stderr
        )


def generate_lyrics_video(
    bg_path: str | Path,
    audio_path: str | Path,
    vocals_path: str | Path,
    lyrics_path: str | Path,
    output_path: str | Path,
    outro_path: str | Path | None = None,
    title: str = "",
    artist: str = "",
    font_name: str = "Arial",
    font_size: int = 72,
    word_highlight: bool = True,
    language: str = "en",
    active_color: str = "#FFFFFF",
    upcoming_color: str = "#FF0000",
    sung_color: str = "#FFFFFF",
    lyric_style: str = "classic",
    outro_text: str = "Thanks for watching!",
) -> None:
    """Background image + synced lyrics subtitles + audio."""
    work_dir = Path(output_path).parent
    audio = _build_audio(work_dir, Path(audio_path), Path(outro_path) if outro_path else None)
    duration = get_media_duration(audio)

    parsed = parse_lyrics(lyrics_path)
    
    needs_alignment = parsed and parsed[0][0] is None
    if needs_alignment:
        print("Un-timed lyrics detected. Running intelligent alignment on vocals...")
        try:
            from aligners import align, parse_alignment_json
            # Align a pre-cleaned lyrics file: the aligner must never time section
            # headers like [Chorus] or annotations like (Chay, chay), or the real
            # lyrics start late.
            cleaned_lyrics_path = work_dir / "_cleaned_lyrics.txt"
            cleaned_lyrics_path.write_text(
                "\n".join(txt for _, txt in parsed),
                encoding="utf-8",
            )
            # Keep the timing JSON inside this job's own output folder: the
            # default name is derived from the audio basename, so two jobs with
            # the same song title would otherwise share (and overwrite) it.
            align_json = align(
                str(vocals_path),
                str(cleaned_lyrics_path),
                output_json=str(work_dir / "_alignment.json"),
                language=language or None,
            )
            timed_raw = parse_alignment_json(align_json)
            timed = []
            for seg in timed_raw:
                clean_text = _clean_genius_line(seg.get("text", ""))
                if clean_text:
                    seg = dict(seg)
                    seg["text"] = clean_text
                    timed.append(seg)
            timed = timed or timed_raw
            # The forced aligner places every word on the audio itself, so the
            # onset-anchoring correction is only for the transcribe-then-match
            # fallbacks, which can start the first line too late.
            engine = ""
            try:
                with open(align_json, "r", encoding="utf-8") as fh:
                    engine = str(json.load(fh).get("engine", ""))
            except Exception:
                pass
            if engine in ("mms_forced_align", "forced_whisper_combo"):
                print("Forced alignment used; onset anchoring not needed.")
            else:
                try:
                    from aligners.audio_utils import detect_first_vocal_onset
                    vocal_onset = detect_first_vocal_onset(str(vocals_path))
                    if vocal_onset is not None:
                        print(f"First vocal onset on the vocal stem: {vocal_onset:.2f}s")
                        timed = _anchor_to_vocal_onset(timed, vocal_onset)
                except Exception as onset_err:
                    print(f"Vocal onset anchoring skipped: {onset_err}")
        except Exception as e:
            print(f"Alignment engine failed: {e}. Falling back to even spacing.")
            timed = assign_timestamps(parsed, duration)
    else:
        timed = assign_timestamps(parsed, duration)

    # Prevent the last lyric from staying on screen forever during an outro
    if timed:
        last_item = timed[-1]
        if "words" in last_item and last_item["words"]:
            # If we have word-level timestamps, cap the line end to shortly after the last word
            last_word_end = last_item["words"][-1]["end"]
            new_end = last_word_end + 1.0
            if new_end < last_item["end"]:
                last_item["end"] = new_end
        elif last_item["end"] - last_item["start"] > 8.0:
            # If no word-level timestamps, just cap at 8 seconds max
            last_item["end"] = last_item["start"] + 8.0

    if outro_path:
        main_dur = get_media_duration(audio_path)
        outro_dur = get_media_duration(outro_path)
        timed.append({
            "start": main_dur,
            "end": main_dur + outro_dur,
            "text": outro_text or "Thanks for watching!",
            "words": []
        })

    # Repair implausible word spacing before SRT/LRC/ASS are written, so the
    # video and the downloadable timing files agree.
    timed = [_clean_word_timings(item) for item in timed]

    # Write SRT
    srt_lines = []
    for i, item in enumerate(timed):
        s = item["start"]
        e = item["end"]
        sh, sm, ss, sms = int(s//3600), int((s%3600)//60), int(s%60), int((s%1)*1000)
        eh, em, es, ems = int(e//3600), int((e%3600)//60), int(e%60), int((e%1)*1000)
        srt_lines.append(f"{i+1}\n{sh:02d}:{sm:02d}:{ss:02d},{sms:03d} --> {eh:02d}:{em:02d}:{es:02d},{ems:03d}\n{item['text']}\n")
    (work_dir / "lyrics.srt").write_text("\n".join(srt_lines), encoding="utf-8")

    # Write LRC
    lrc_lines = []
    for item in timed:
        s = item["start"]
        sm, ss, scs = int(s//60), int(s%60), int((s%1)*100)
        lrc_lines.append(f"[{sm:02d}:{ss:02d}.{scs:02d}]{item['text']}")
    (work_dir / "lyrics.lrc").write_text("\n".join(lrc_lines), encoding="utf-8")

    # Write ASS file
    ass_content = generate_ass(
        timed, title=title, artist=artist, total_duration=duration,
        font_name=font_name, font_size=font_size, word_highlight=word_highlight,
        active_color=active_color, upcoming_color=upcoming_color, sung_color=sung_color,
        lyric_style=lyric_style,
    )
    ass_path = work_dir / "_lyrics.ass"
    ass_path.write_text(ass_content, encoding="utf-8")

    ass_path_escaped = _ass_escape_path(str(ass_path))
    base_dir_escaped = _ass_escape_path(str(Path(__file__).parent.resolve()))
    vf = f"{_VIDEO_SCALE},subtitles={ass_path_escaped}:fontsdir={base_dir_escaped}"

    cmd = _get_ffmpeg_base(bg_path) + [
        "-i", str(bg_path),
        "-i", str(audio),
        "-vf", vf,
        "-shortest",
    ] + _video_encode_args() + [str(output_path)]
    subprocess.run(cmd, check=True, capture_output=True)

    # Returned so the editor can keep word-level timings instead of re-reading
    # the line-level SRT.
    return timed


def _ffmpeg_escape(text: str) -> str:
    """Escape text for use inside ffmpeg drawtext text= value."""
    return text.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")


def _ffmpeg_escape_path(path: str) -> str:
    """Escape a file path for use in an ffmpeg filter option value (e.g. drawtext fontfile/textfile).
    Converts backslashes to forward slashes, escapes colons (Windows drive letters),
    and wraps the whole path in single quotes to handle spaces in directory names."""
    p = path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    return f"'{p}'"


def _ass_escape_path(path: str) -> str:
    # FFmpeg subtitles filter path escaping (Windows drive letters, colons)
    # The entire path must be wrapped in single quotes to handle spaces correctly.
    p = path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    return f"'{p}'"


# ─── Editor Re-render Support ────────────────────────────────────────────────

def _retime_segment_words(segment: dict) -> dict:
    """Rescale a segment's word timings onto its edited [start, end].

    The editor changes a line's start/end but not every word, so the words are
    mapped from the segment's original span onto its current one. A line whose
    text was edited keeps its words too; generate_ass ignores them once they no
    longer spell the line.
    """
    words = segment.get("words") or []
    if not words:
        return segment
    try:
        start = float(segment.get("start", 0.0))
        end = float(segment.get("end", start))
        orig_start = float(segment.get("orig_start", start))
        orig_end = float(segment.get("orig_end", end))
    except (TypeError, ValueError):
        return segment

    old_span = orig_end - orig_start
    new_span = end - start
    if old_span <= 0 or new_span <= 0:
        return segment
    if abs(old_span - new_span) < 1e-6 and abs(orig_start - start) < 1e-6:
        return segment

    scale = new_span / old_span
    retimed = []
    for word in words:
        try:
            word_start = float(word["start"])
            word_end = float(word["end"])
        except (KeyError, TypeError, ValueError):
            retimed.append(word)
            continue
        new_start = start + (word_start - orig_start) * scale
        new_end = start + (word_end - orig_start) * scale
        new_start = min(max(new_start, start), max(start, end - 0.02))
        new_end = min(max(new_end, new_start + 0.02), end)
        retimed.append({**word, "start": round(new_start, 3), "end": round(new_end, 3)})

    updated = dict(segment)
    updated["words"] = retimed
    return updated


def rerender_lyrics_video(
    bg_path,
    audio_path,
    segments: list,
    output_path,
    font_name: str = "Arial",
    font_size: int = 72,
    active_color: str = "#FFFFFF",
    upcoming_color: str = "#FF0000",
    sung_color: str = "#FFFFFF",
    lyric_style: str = "classic",
    word_highlight: bool = True,
) -> None:
    """
    Re-render a lyrics video from Lyric Editor-corrected segments.

    Uses the same style presets and karaoke word timings as the original
    render, plus the per-segment x/y offsets stored by the editor.
    """
    work_dir = Path(output_path).parent
    work_dir.mkdir(parents=True, exist_ok=True)

    prepared = [_clean_word_timings(_retime_segment_words(segment)) for segment in segments]
    ass_content = generate_ass(
        prepared,
        font_name=font_name,
        font_size=font_size,
        word_highlight=word_highlight,
        active_color=active_color,
        upcoming_color=upcoming_color,
        sung_color=sung_color,
        lyric_style=lyric_style,
    )
    ass_path = work_dir / "_corrected.ass"
    ass_path.write_text(ass_content, encoding="utf-8")

    ass_escaped = _ass_escape_path(str(ass_path))
    base_dir_escaped = _ass_escape_path(str(Path(__file__).parent.resolve()))
    vf = f"{_VIDEO_SCALE},subtitles={ass_escaped}:fontsdir={base_dir_escaped}"

    cmd = _get_ffmpeg_base(bg_path) + [
        "-i", str(bg_path),
        "-i", str(audio_path),
        "-vf", vf,
        "-shortest",
    ] + _video_encode_args() + [str(output_path)]

    subprocess.run(cmd, check=True, capture_output=True)
