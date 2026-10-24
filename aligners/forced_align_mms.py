"""
CTC forced alignment of KNOWN lyrics (MMS_FA / torchaudio).

Unlike the WhisperX path, this never transcribes: the lyric words themselves are
the reference, so every word is placed on the audio instead of being guessed from
a transcript. On the Jamendo Spanish tracks measured for this project it puts
~96% of word onsets within 0.3 s, against ~29% for the transcribe-then-match path.

Public API matches the other engines:
    align_lyrics(audio_path, lyrics_path, output_json=None, language=None) -> str

Requires torchaudio that still ships ``forced_align`` (2.6-2.8; removed in 2.9).
On an unsupported torchaudio the module raises, and the dispatcher falls back to
WhisperX.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path
from typing import List, Optional, Sequence

MIN_LINE_DUR = 0.12
SILENCE_MIN_SEC = 1.2    # a quiet stretch this long separates two sung sections
SILENCE_REL_DB = -35.0   # quiet = this far below the stem's loudest moment
MAX_SEC_PER_WORD = 2.0   # a word taking longer than this means the line is misplaced
DUPLICATE_REPEAT_SEC = 0.6  # the same line twice within this window is a mapping error

_FALLBACK_DIR = Path(__file__).resolve().parent / "alignments"


def _align_dir() -> Path:
    try:
        from .config import ALIGN_DIR  # type: ignore
        return Path(ALIGN_DIR)
    except Exception:
        _FALLBACK_DIR.mkdir(parents=True, exist_ok=True)
        return _FALLBACK_DIR


# ---------------------------------------------------------------------------
# Text preparation
# ---------------------------------------------------------------------------

def normalize_word(word: str) -> str:
    """MMS_FA is trained on romanised letters; drop accents, digits, punctuation."""
    w = unicodedata.normalize("NFKD", str(word)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z']", "", w.lower())


def read_lyric_lines(lyrics_path: str | Path) -> List[str]:
    lines = []
    for raw in Path(lyrics_path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line:
            lines.append(line)
    return lines


# ---------------------------------------------------------------------------
# Audio / model
# ---------------------------------------------------------------------------

_BUNDLE = None
_MODEL = None


def _bundle():
    """Load MMS_FA once per process (~1.2 GB checkpoint on first use)."""
    global _BUNDLE
    if _BUNDLE is None:
        import torchaudio

        if not hasattr(torchaudio.functional, "forced_align"):
            raise RuntimeError(
                "torchaudio.functional.forced_align is unavailable (removed in 2.9); "
                "pin torchaudio 2.6-2.8 or use the WhisperX path"
            )
        _BUNDLE = torchaudio.pipelines.MMS_FA
    return _BUNDLE


def _model():
    global _MODEL
    if _MODEL is None:
        _MODEL = _bundle().get_model()
        _MODEL.eval()
    return _MODEL


def _ensure_wav(audio_path: str | Path) -> str:
    """torchaudio can read most formats, but mp3/m4a need an ffmpeg build."""
    path = Path(audio_path)
    if path.suffix.lower() == ".wav":
        return str(path)
    from .audio_utils import convert_to_wav  # local import keeps module import light

    out = path.with_suffix(".fa.wav")
    if out.exists() and out.stat().st_size > 0:
        return str(out)
    convert_to_wav(str(path), str(out))
    return str(out)


def load_waveform(audio_path: str | Path):
    import torch
    import torchaudio

    wav, sr = torchaudio.load(_ensure_wav(audio_path))
    if wav.shape[0] > 1:
        wav = wav.mean(dim=0, keepdim=True)
    target = _bundle().sample_rate
    if sr != target:
        wav = torchaudio.functional.resample(wav, sr, target)
    return wav


def detect_silences(wav, sample_rate: int, min_sec: float = SILENCE_MIN_SEC,
                    rel_db: float = SILENCE_REL_DB, hop_sec: float = 0.05):
    """Quiet stretches long enough that no word can be sung across them."""
    import numpy as np

    samples = wav.squeeze(0).detach().cpu().numpy()
    win = max(1, int(sample_rate * hop_sec))
    frames = len(samples) // win
    if frames == 0:
        return []
    rms = np.sqrt(np.mean(samples[: frames * win].reshape(frames, win) ** 2, axis=1))
    peak = float(rms.max()) or 1.0
    thresh = peak * (10 ** (rel_db / 20.0))

    breaks = []
    start = None
    for i, quiet in enumerate(rms < thresh):
        if quiet and start is None:
            start = i
        elif not quiet and start is not None:
            if (i - start) * hop_sec >= min_sec:
                breaks.append((start * hop_sec, i * hop_sec))
            start = None
    if start is not None and (frames - start) * hop_sec >= min_sec:
        breaks.append((start * hop_sec, frames * hop_sec))
    return breaks


def flag_fragments(fragments: List[dict], breaks: Sequence[tuple]) -> int:
    """Mark lines the audio says cannot be right; returns how many were flagged.

    Signals that need no model: a line spanning a silent gap, a line whose pace
    is impossible for its word count, and the same lyric line landing twice at
    the same moment (the mapping folded onto one occurrence).
    """
    for f in fragments:
        f["suspicious"] = False
        f["reasons"] = []

    for f in fragments:
        begin, end = float(f["begin"]), float(f["end"])
        for b_start, b_end in breaks:
            if begin < b_start and b_end < end:
                f["reasons"].append(f"spans a {b_end - b_start:.1f}s silence at {b_start:.1f}s")
        n_words = max(1, len(f.get("words") or []))
        if (end - begin) / n_words > MAX_SEC_PER_WORD:
            f["reasons"].append("implausible pace for the line")
        f["suspicious"] = bool(f["reasons"])

    by_text: dict = {}
    for i, f in enumerate(fragments):
        text = (f.get("lines") or [""])[0].strip().lower()
        by_text.setdefault(text, []).append(i)
    for idxs in by_text.values():
        for a, b in zip(idxs, idxs[1:]):
            if abs(float(fragments[b]["begin"]) - float(fragments[a]["begin"])) < DUPLICATE_REPEAT_SEC:
                for i in (a, b):
                    fragments[i]["reasons"].append("same line placed twice at the same time")
                    fragments[i]["suspicious"] = True

    return sum(1 for f in fragments if f["suspicious"])


# ---------------------------------------------------------------------------
# Alignment
# ---------------------------------------------------------------------------

def _align_all(audio_path, lines: List[str]):
    """Return per-line word times, per-word confidence, and the audio duration."""
    import torch

    bundle = _bundle()
    model = _model()
    dictionary = bundle.get_dict()

    line_words: List[List[str]] = []
    tokens_per_word: List[List[int]] = []
    for line in lines:
        # Keep every word, including ones with no MMS_FA letters ("47"): the
        # rendered line must read exactly as written, so those get a slot by
        # interpolation instead of being dropped.
        words = line.split()
        line_words.append(words)
        for w in words:
            ids = [dictionary[c] for c in normalize_word(w) if c in dictionary]
            tokens_per_word.append(ids)

    if not any(tokens_per_word):
        raise ValueError("no lyric word could be mapped to the MMS_FA vocabulary")

    wav = load_waveform(audio_path)
    breaks = detect_silences(wav, bundle.sample_rate)
    with torch.inference_mode():
        emission, _ = model(wav)

    aligner = bundle.get_aligner()
    spans = aligner(emission[0], tokens_per_word)  # one TokenSpan list per word

    seconds_per_frame = wav.shape[1] / emission.shape[1] / bundle.sample_rate
    times: List[Optional[List[float]]] = []
    confs: List[float] = []
    for chunk in spans:
        if not chunk:
            times.append(None)
            confs.append(0.0)
            continue
        start = chunk[0].start * seconds_per_frame
        end = chunk[-1].end * seconds_per_frame
        times.append([start, max(start + 0.02, end)])
        confs.append(float(sum(s.score for s in chunk) / len(chunk)))

    duration = wav.shape[1] / bundle.sample_rate
    return line_words, times, confs, duration, breaks


def _fill_missing(times: List[Optional[List[float]]], lo: float, hi: float) -> List[List[float]]:
    """Give words with no valid characters a slot between their known neighbours.

    Words like "47" or "..." have no MMS_FA letters; they still need a slot so
    the line's word list stays complete.
    """
    n = len(times)
    known = [i for i, t in enumerate(times) if t is not None]
    if not known:
        step = max(0.05, (hi - lo) / max(1, n))
        return [[lo + i * step, lo + (i + 1) * step] for i in range(n)]

    out: List[Optional[List[float]]] = [list(t) if t is not None else None for t in times]  # type: ignore[list-item]

    bounds = [-1] + known + [n]
    for a, b in zip(bounds, bounds[1:]):
        count = b - a - 1
        if count <= 0:
            continue
        prev_end = out[a][1] if a >= 0 else None  # type: ignore[index]
        next_start = out[b][0] if b < n else None  # type: ignore[index]
        gap_lo = prev_end if prev_end is not None else min(lo, next_start if next_start is not None else lo)
        gap_hi = next_start if next_start is not None else max(hi, gap_lo + 0.05 * count)
        span = max(gap_hi - gap_lo, 0.05 * count)
        step = span / count
        for k in range(count):
            s = gap_lo + k * step
            out[a + 1 + k] = [s, s + max(step * 0.85, 0.03)]

    return out  # type: ignore[return-value]


def align_words(audio_path, lines: List[str]) -> List[dict]:
    """Align every lyric line and return fragment dicts in the shared schema."""
    line_words, flat_times, confs, duration, breaks = _align_all(audio_path, lines)

    fragments: List[dict] = []
    prev_end = 0.0
    wi = 0
    for line_idx, words in enumerate(line_words):
        if not words:
            continue
        n = len(words)
        chunk_times = flat_times[wi: wi + n]
        chunk_confs = confs[wi: wi + n]
        wi += n

        known = [t for t in chunk_times if t is not None]
        lo = min((t[0] for t in known), default=prev_end + 0.05)
        hi = max((t[1] for t in known), default=lo + 1.0)
        times = _fill_missing(chunk_times, max(prev_end, lo - 0.05), hi)

        # Keep the line monotonic and inside the audio
        cursor = max(prev_end + 0.01, times[0][0] - 0.02)
        word_objs = []
        for w, (s, e) in zip(words, times):
            s = max(s, cursor)
            e = max(e, s + 0.05)
            word_objs.append({"text": w, "start": round(s, 3), "end": round(e, 3)})
            cursor = e

        begin = round(max(prev_end, times[0][0] - 0.02), 3)
        end = round(min(duration, max(cursor, begin + MIN_LINE_DUR)), 3)
        confidence = round(sum(chunk_confs) / max(1, len(chunk_confs)), 3)

        fragments.append({
            "begin": f"{begin:.3f}",
            "end": f"{end:.3f}",
            "lines": [lines[line_idx]],
            "words": word_objs,
            "confidence": confidence,
        })
        prev_end = end

    flag_fragments(fragments, breaks)
    return fragments


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def align_lyrics(
    audio_path: str,
    lyrics_path: str,
    output_json: Optional[str] = None,
    language: Optional[str] = None,
) -> str:
    """Align known lyrics to audio and write the shared fragments JSON."""
    if output_json is None:
        base = os.path.splitext(os.path.basename(str(audio_path)))[0]
        output_json = str(_align_dir() / f"{base}_forced_alignment.json")

    lines = read_lyric_lines(lyrics_path)
    if not lines:
        raise ValueError(f"no lyrics found in {lyrics_path}")

    print(f"[forced] Aligning {len(lines)} lines with MMS_FA (no transcription)...")
    fragments = align_words(audio_path, lines)

    flagged = sum(1 for f in fragments if f.get("suspicious"))
    print(f"[forced] Done. {len(fragments)} fragments, {flagged} flagged for review")

    os.makedirs(os.path.dirname(os.path.abspath(output_json)), exist_ok=True)
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(
            {
                "fragments": fragments,
                "engine": "mms_forced_align",
                "flagged_lines": flagged,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )
    return output_json


# Compatibility alias used by the dispatcher
align = align_lyrics

__all__ = ["align_lyrics", "align", "align_words", "normalize_word", "read_lyric_lines"]
