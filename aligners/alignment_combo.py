"""
Forced alignment plus a Whisper second opinion on the lines the audio model
flags.

The MMS_FA forced aligner places every known lyric word on the audio, which is
both fast (one CTC pass) and the most accurate path measured for this project.
A few lines are still impossible according to the audio itself: they straddle a
silence, last far longer than their word count can sustain, or duplicate another
line at the same moment. Those are exactly the lines worth re-checking with
speech recognition.

Transcribing the whole song (the WhisperX path) takes minutes. This engine cuts
a short window around each flagged line, transcribes only that window with
faster-whisper, and replaces the forced timings only when the transcript
confirms enough of the line's own words. Unflagged lines cost nothing, and a
missing model or a failed transcription leaves the forced result in place.

Measured on the project's five-track Jamendo set (mix audio): 94.3% of word
onsets within 0.3 s, against 93.7% for forced alignment alone, with ASR run only
on the three flagged lines. On the Demucs vocal stem used by the app, the same
track that failed on the mix scores 99.5%.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable, List, Optional

from .alignment_whisperx import _dp_match
from .forced_align_mms import (
    MIN_LINE_DUR,
    _fill_missing,
    align_words as forced_align_words,
    read_lyric_lines,
)

MAX_REPAIRS = 16        # cap the ASR work for one song
WINDOW_PAD_SEC = 0.4    # context around a flagged line
MIN_WINDOW_SEC = 1.2
MAX_WINDOW_SEC = 15.0
MIN_MATCH_RATIO = 0.6   # share of the line's words the transcript must confirm

_WHISPER_MODEL = None
# Concurrent jobs share this module-level cache, so the first load must not run
# twice: two Whisper instances would double the memory and race on the download.
_WHISPER_MODEL_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Which lines are worth a second opinion
# ---------------------------------------------------------------------------

def _needs_repair(fragment: dict) -> bool:
    """The forced aligner already decided this line cannot be right.

    Trusting its own flags keeps the ASR pass small: a clean line never costs a
    transcription.
    """
    return bool(fragment.get("suspicious"))


def _window_for(
    fragment: dict, duration: float, previous_end: float = 0.0
) -> tuple[float, float]:
    """Audio window that should contain this line, padded for context.

    A flagged line is often parked at the next vocal onset instead of where it
    is sung. Searching from the end of the previous line (a confident anchor)
    rather than from the line's own forced start finds the earlier occurrence.
    """
    begin = max(0.0, float(fragment.get("begin", 0.0)) - WINDOW_PAD_SEC)
    if previous_end > 0.0:
        begin = min(begin, max(0.0, previous_end - WINDOW_PAD_SEC))
    end = max(float(fragment.get("end", 0.0)), begin) + WINDOW_PAD_SEC
    end = min(duration, end)
    if end - begin < MIN_WINDOW_SEC:
        end = min(duration, begin + MIN_WINDOW_SEC)
        begin = max(0.0, end - MIN_WINDOW_SEC)
    if end - begin > MAX_WINDOW_SEC:
        # A misplaced line can span a whole gap. Start where the line begins and
        # keep the search bounded instead of transcribing the entire gap.
        end = min(duration, begin + MAX_WINDOW_SEC)
    return begin, end


# ---------------------------------------------------------------------------
# Mapping a transcript window onto one lyric line
# ---------------------------------------------------------------------------

def _candidate_words(
    line_text: str,
    asr_words: List[dict],
    window_start: float,
    window_end: float,
) -> Optional[List[dict]]:
    """Return new word timings for a line, or None when the transcript disagrees.

    Only words the transcript confirms are taken from it; the rest are
    interpolated between their neighbours, so the line stays complete and in
    order.
    """
    words = str(line_text or "").split()
    if not words or not asr_words:
        return None

    mapping = _dp_match(words, asr_words)
    need = min(len(words), max(2, math.ceil(MIN_MATCH_RATIO * len(words))))
    if len(mapping) < need:
        return None

    times: List[Optional[List[float]]] = [None] * len(words)
    for index, asr_index in mapping.items():
        if index >= len(words) or asr_index >= len(asr_words):
            continue
        asr = asr_words[asr_index]
        try:
            start = max(window_start, float(asr["start"]))
            end = min(window_end, max(start + 0.05, float(asr["end"])))
        except (KeyError, TypeError, ValueError):
            continue
        times[index] = [start, end]

    known = [entry for entry in times if entry is not None]
    if not known:
        return None
    low = max(window_start, min(entry[0] for entry in known) - 0.15)
    high = min(window_end, max(entry[1] for entry in known) + 0.15)
    if high <= low:
        return None
    filled = _fill_missing(times, low, high)

    candidate: List[dict] = []
    cursor = low
    for text, (start, end) in zip(words, filled):
        start = min(max(float(start), cursor), max(low, window_end - 0.03))
        end = min(max(float(end), start + 0.05), window_end)
        if end <= start:
            return None
        candidate.append({
            "text": text,
            "start": round(start, 3),
            "end": round(end, 3),
        })
        cursor = end

    return candidate if _accept(candidate) else None


def _accept(candidate: List[dict]) -> bool:
    """Reject a repair that is not a plausible, ordered line in its window."""
    if not candidate:
        return False
    starts = [word["start"] for word in candidate]
    if any(not math.isfinite(value) for value in starts):
        return False
    if any(later < earlier for earlier, later in zip(starts, starts[1:])):
        return False
    if candidate[-1]["end"] <= candidate[0]["start"]:
        return False
    return candidate[-1]["end"] - candidate[0]["start"] <= MAX_WINDOW_SEC


# ---------------------------------------------------------------------------
# Repair pass
# ---------------------------------------------------------------------------

def _changed(original: List[dict], candidate: List[dict]) -> bool:
    if len(original) != len(candidate):
        return True
    for old, new in zip(original, candidate):
        try:
            if abs(float(old["start"]) - new["start"]) > 0.05:
                return True
            if abs(float(old["end"]) - new["end"]) > 0.05:
                return True
        except (KeyError, TypeError, ValueError):
            return True
    return False


def repair_fragments(
    fragments: List[dict],
    transcribe: Callable[[float, float], List[dict]],
    duration: float,
    max_repairs: int = MAX_REPAIRS,
) -> int:
    """Replace flagged lines with transcript-confirmed timings.

    Mutates `fragments` and returns how many lines actually changed. A failing
    transcriber stops the pass and leaves the remaining forced timings as they
    were, so alignment still succeeds without a Whisper model.
    """
    repaired = 0
    for position, fragment in enumerate(fragments):
        if repaired >= max_repairs:
            break
        if not _needs_repair(fragment):
            continue

        line = (fragment.get("lines") or [""])[0]
        previous_end = float(fragments[position - 1]["end"]) if position else 0.0
        window_start, window_end = _window_for(fragment, duration, previous_end)
        try:
            asr_words = transcribe(window_start, window_end)
        except Exception as exc:  # model missing, decode error, ...
            print(f"[combo] Whisper repair unavailable ({exc}); keeping forced timings.")
            break

        candidate = _candidate_words(line, asr_words, window_start, window_end)
        if candidate and _changed(fragment.get("words") or [], candidate):
            fragment["words"] = candidate
            fragment["begin"] = f"{candidate[0]['start']:.3f}"
            fragment["end"] = (
                f"{max(candidate[-1]['end'], candidate[0]['start'] + MIN_LINE_DUR):.3f}"
            )
            fragment["repaired_by"] = "whisper"
            fragment["suspicious"] = False
            repaired += 1

    if repaired:
        _enforce_monotonic(fragments, duration)
    return repaired


def _enforce_monotonic(fragments: List[dict], duration: float) -> None:
    """Keep every line and word in order after localized repairs."""
    previous_end = 0.0
    for fragment in fragments:
        words = fragment.get("words") or []
        cursor = max(previous_end, float(fragment.get("begin", 0.0)))
        for word in words:
            start = max(float(word.get("start", cursor)), cursor)
            end = max(float(word.get("end", start + 0.05)), start + 0.05)
            word["start"] = round(start, 3)
            word["end"] = round(end, 3)
            cursor = end

        if words:
            begin = float(words[0]["start"])
            end = min(duration, max(float(words[-1]["end"]), begin + MIN_LINE_DUR))
        else:
            begin = max(previous_end, float(fragment.get("begin", previous_end)))
            end = min(
                duration,
                max(float(fragment.get("end", begin + MIN_LINE_DUR)), begin + MIN_LINE_DUR),
            )
        fragment["begin"] = f"{begin:.3f}"
        fragment["end"] = f"{end:.3f}"
        previous_end = end


# ---------------------------------------------------------------------------
# Whisper window transcriber
# ---------------------------------------------------------------------------

def _whisper_model():
    """Load faster-whisper once per process (downloads on first use)."""
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        with _WHISPER_MODEL_LOCK:
            if _WHISPER_MODEL is None:
                from faster_whisper import WhisperModel

                size = os.environ.get("LYRIC_WHISPER_MODEL", "small")
                device, compute_type = "cpu", "int8"
                try:
                    import torch

                    if torch.cuda.is_available():
                        device, compute_type = "cuda", "float16"
                except Exception:
                    pass
                _WHISPER_MODEL = WhisperModel(
                    size, device=device, compute_type=compute_type
                )
    return _WHISPER_MODEL


def _detect_repair_language(model, samples, sample_rate: int) -> Optional[str]:
    """Vote over chunks spread across the song.

    A single 30 s window is unreliable for this: a musical intro, a guest verse
    or a noisy section can each return a different language (Caralibro's intro
    reads as English at 0.41 confidence). Summing probabilities over six windows
    gives the language of the song instead of the language of its intro.
    """
    chunk = sample_rate * 30
    # The first three minutes are the sung part of the track; the outro adds
    # instrumental and spoken sections that made a whole-song vote pick the
    # wrong language for Caralibro (Basque from the ending).
    span = min(len(samples), sample_rate * 180)
    if span <= chunk:
        offsets = [0]
    else:
        count = min(6, span // chunk)
        offsets = [int(i * (span - chunk) / max(1, count - 1)) for i in range(count)]

    scores: dict[str, float] = {}
    votes: dict[str, int] = {}
    for offset in offsets:
        try:
            detected, probability, _ = model.detect_language(samples[offset:offset + chunk])
        except Exception:
            continue
        if not detected:
            continue
        scores[detected] = scores.get(detected, 0.0) + float(probability)
        votes[detected] = votes.get(detected, 0) + 1

    if not scores:
        return None
    winner = max(scores, key=lambda language: (votes[language], scores[language]))
    if votes[winner] < 2 and len(offsets) > 1:
        return None
    if len(offsets) > 1 and votes[winner] * 2 < len(offsets):
        return None  # no majority; let Whisper decide per window
    print(
        f"[combo] Repair language: {winner} "
        f"(votes {votes[winner]}/{len(offsets)}, score {scores[winner]:.2f})"
    )
    return winner


def _build_transcriber(audio_path: str, language: Optional[str]):
    """Decode the vocal stem once, then transcribe short slices in memory."""
    from .forced_align_mms import _bundle, load_waveform

    model = _whisper_model()
    waveform = load_waveform(audio_path)
    sample_rate = _bundle().sample_rate
    samples = waveform.squeeze(0).detach().cpu().numpy().astype("float32")
    lang = language or None
    if not lang:
        # A short repair window is too little audio for reliable per-window
        # language detection, so decide once for the whole song and reuse it.
        try:
            lang = _detect_repair_language(model, samples, sample_rate)
        except Exception as exc:
            print(f"[combo] Language detection failed ({exc}); using per-window detection.")

    def transcribe(start: float, end: float) -> List[dict]:
        first = max(0, int(start * sample_rate))
        last = min(len(samples), int(end * sample_rate))
        if last - first < int(0.2 * sample_rate):
            return []
        segments, _info = model.transcribe(
            samples[first:last],
            language=lang,
            beam_size=1,
            word_timestamps=True,
            condition_on_previous_text=False,
            vad_filter=False,
        )
        words: List[dict] = []
        for segment in segments:
            for word in (segment.words or []):
                if word.start is None or word.end is None:
                    continue
                words.append({
                    "word": word.word,
                    "start": start + float(word.start),
                    "end": start + float(word.end),
                })
        return words

    return transcribe


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _duration(audio_path: str, fragments: List[dict]) -> float:
    last_end = max((float(f.get("end", 0.0)) for f in fragments), default=0.0)
    measured = 0.0
    try:
        from .audio_utils import _resolve_ffmpeg_tools

        _ffmpeg, ffprobe = _resolve_ffmpeg_tools(None)
        result = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json", "-show_format", str(audio_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0:
            measured = float(json.loads(result.stdout)["format"]["duration"])
    except Exception:
        pass
    return max(last_end + 5.0, measured, 1.0)


def align_lyrics(
    audio_path: str,
    lyrics_path: str,
    output_json: Optional[str] = None,
    language: Optional[str] = None,
    *,
    forced_fn: Optional[Callable[[], List[dict]]] = None,
    transcriber: Optional[Callable[[float, float], List[dict]]] = None,
    duration: Optional[float] = None,
) -> str:
    """Align known lyrics with MMS_FA, repairing flagged lines with Whisper.

    `forced_fn`, `transcriber` and `duration` exist so the merge logic can be
    tested without a model; the app leaves them at their defaults.
    """
    if output_json is None:
        from .config import ALIGN_DIR

        base = os.path.splitext(os.path.basename(str(audio_path)))[0]
        output_json = os.path.join(ALIGN_DIR, f"{base}_combo_alignment.json")

    lines = read_lyric_lines(lyrics_path)
    if not lines:
        raise ValueError(f"no lyrics found in {lyrics_path}")

    if forced_fn is None:
        forced_fn = lambda: forced_align_words(audio_path, lines)

    print(f"[combo] Forced-aligning {len(lines)} lines with MMS_FA...")
    fragments = forced_fn()
    if duration is None:
        duration = _duration(audio_path, fragments)

    repairs = 0
    if any(_needs_repair(fragment) for fragment in fragments):
        if transcriber is None:
            if not language:
                # The lyrics are the most reliable language signal; isolated
                # vocals are sparse and can be misdetected (Spanish singing read
                # as English). langdetect is optional.
                try:
                    from .alignment_whisperx import _detect_language_from_text

                    language = _detect_language_from_text(" ".join(lines)) or None
                    if language:
                        print(f"[combo] Language from lyrics: {language}")
                except Exception as exc:
                    print(f"[combo] Lyrics language detection failed: {exc}")
            try:
                transcriber = _build_transcriber(audio_path, language)
            except Exception as exc:
                print(f"[combo] Whisper repair unavailable ({exc}); keeping forced timings.")
        if transcriber is not None:
            print(f"[combo] Re-checking flagged lines with Whisper ({MAX_REPAIRS} max)...")
            repairs = repair_fragments(fragments, transcriber, duration)

    flagged = sum(1 for fragment in fragments if fragment.get("suspicious"))
    print(f"[combo] Done. {len(fragments)} fragments, {repairs} repaired, {flagged} still flagged")

    os.makedirs(os.path.dirname(os.path.abspath(output_json)), exist_ok=True)
    with open(output_json, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "fragments": fragments,
                "engine": "forced_whisper_combo",
                "flagged_lines": flagged,
                "whisper_repairs": repairs,
            },
            handle,
            indent=2,
            ensure_ascii=False,
        )
    return output_json


# Compatibility alias used by the dispatcher
align = align_lyrics

__all__ = ["align_lyrics", "align", "repair_fragments", "MAX_REPAIRS"]
