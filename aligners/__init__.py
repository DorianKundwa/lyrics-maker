import os
import traceback

# WhisperX is the default: its word times come from the transcript, so a track
# that opens slowly and then switches to fast rap does not drag every later
# lyric line out of sync the way a monotonic CTC alignment can. Forced
# alignment stays available explicitly, and as a fallback if WhisperX fails.
DEFAULT_ENGINE = "whisperx"

_ENGINE_ORDER = {
    "whisperx": ("whisperx", "combo", "forced"),
    "combo": ("combo", "forced", "whisperx"),
    "forced": ("forced", "whisperx"),
    "auto": ("whisperx", "combo", "forced"),
}

_ENGINE_LABELS = {"whisperx": "WhisperX", "combo": "Combo", "forced": "Forced"}


def _engine() -> str:
    """Alignment engine choice: whisperx (default), combo, forced, auto."""
    return os.environ.get("LYRIC_ALIGN_ENGINE", DEFAULT_ENGINE).strip().lower()


def _run_engine(name: str, audio_path: str, lyrics_path: str,
                output_json: str, language) -> str:
    if name == "whisperx":
        import whisperx  # noqa: F401  (import guard for the cascade)
        from .alignment_whisperx import align as run
        print("Attempting WhisperX alignment (transcript-driven, handles tempo changes)...")
        # Strict: let a broken/unavailable WhisperX fail the cascade instead of
        # silently returning its own heuristic timing.
        return run(audio_path, lyrics_path, output_json, language=language, fallback=False)
    if name == "combo":
        from .alignment_combo import align_lyrics as run
        print("Attempting MMS_FA forced alignment with Whisper repair for flagged lines...")
    elif name == "forced":
        from .forced_align_mms import align_lyrics as run
        print("Attempting MMS_FA forced alignment of the supplied lyrics...")
    else:
        raise ValueError(f"unknown alignment engine: {name}")
    return run(audio_path, lyrics_path, output_json, language=language)


def align(audio_path: str, lyrics_path: str, output_json: str = None, language: str = None) -> str:
    """
    Unified alignment function.

    Tries the engines in the order selected by LYRIC_ALIGN_ENGINE
    (default: WhisperX -> combo -> forced), then Aeneas and finally the
    heuristic fallback. Returns the path to the generated JSON file.
    """
    if output_json is None:
        from .config import ALIGN_DIR
        base = os.path.splitext(os.path.basename(audio_path))[0]
        output_json = os.path.join(ALIGN_DIR, f"{base}_alignment.json")

    order = _ENGINE_ORDER.get(_engine(), _ENGINE_ORDER[DEFAULT_ENGINE])
    for name in order:
        try:
            res = _run_engine(name, audio_path, lyrics_path, output_json, language)
            if res and os.path.exists(res):
                print(f"{_ENGINE_LABELS.get(name, name)} alignment succeeded.")
                return res
            print(f"{_ENGINE_LABELS.get(name, name)} alignment produced no output.")
        except Exception as e:
            print(f"{_ENGINE_LABELS.get(name, name)} alignment skipped/failed: {e}")

    # Aeneas (passes None -> falls back to 'eng' internally)
    try:
        import aeneas
        from .alignment_aeneas import align as aeneas_align
        print("Aeneas is available. Attempting Aeneas alignment...")
        res = aeneas_align(audio_path, lyrics_path, output_json, language=None)
        if res and os.path.exists(res):
            print("Aeneas alignment succeeded.")
            return res
    except Exception as e:
        print(f"Aeneas alignment skipped/failed: {e}")

    # Last resort: heuristic (auto-detects language from lyrics text)
    print("Falling back to Heuristic alignment...")
    try:
        from .alignment_whisperx import _heuristic_align
        res = _heuristic_align(audio_path, lyrics_path, output_json, language=None)
        if res and os.path.exists(res):
            print("Heuristic alignment succeeded.")
            return res
    except Exception as e:
        print(f"Heuristic alignment failed: {e}")
        traceback.print_exc()

    raise RuntimeError("All alignment engines failed.")


def parse_alignment_json(json_path: str):
    from .alignment_whisperx import parse_alignment_json as parse_json
    return parse_json(json_path)
