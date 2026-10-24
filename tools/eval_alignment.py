"""Score a lyrics aligner against JamendoLyrics word-level annotations.

Usage:
    python tools/eval_alignment.py --engine forced --tracks 10._Disparan_-_criatura
    python tools/eval_alignment.py --engine whisperx --tracks ...

Metric (MIREX/Jamendo convention):
    PCO@tol  share of word onsets within `tol` seconds of the reference
    MAE      mean absolute onset error in seconds
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from pathlib import Path
from typing import Iterable, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data" / "jamendo"
REPO = "jamendolyrics/jamendolyrics"

# Five Spanish tracks spanning different tempos/genres.
DEFAULT_TRACKS = [
    "10._Disparan_-_criatura",
    "Guayeteo_-_JhoyKing",
    "Caralibro_-_Vagos_Permanentes",
    "Baila_-_Alfonso_Lugo",
    "Te_Recuerdo_-_Wilson_Way",
]


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

def _download(repo_path: str, dest: Path) -> Path:
    from huggingface_hub import hf_hub_download

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 1024:
        return dest
    src = Path(hf_hub_download(REPO, repo_path, repo_type="dataset"))
    # Some mp3 entries are symlink stubs pointing at ../subsets/<lang>/mp3/...
    head = src.read_bytes()[:64]
    if src.stat().st_size < 4096 and head.startswith(b"../"):
        target = head.decode("utf-8", "replace").strip().lstrip("./")
        src = Path(hf_hub_download(REPO, target, repo_type="dataset"))
    shutil.copyfile(src, dest)
    return dest


def fetch_track(name: str, dest_dir: Optional[Path] = None) -> dict:
    """Download a track's audio, word annotations and lyric text."""
    dest = Path(dest_dir or (DATA_DIR / name))
    dest.mkdir(parents=True, exist_ok=True)
    audio = _download(f"mp3/{name}.mp3", dest / "audio.mp3")
    words = _download(f"annotations/words/{name}.csv", dest / "words.csv")
    lyrics = _download(f"lyrics/{name}.txt", dest / "lyrics.txt")
    return {"name": name, "audio": str(audio), "words": str(words), "lyrics": str(lyrics)}


def load_gt(words_csv: str | Path) -> list[tuple[float, float]]:
    rows = list(csv.DictReader(open(words_csv, encoding="utf-8")))
    return [(float(r["word_start"]), float(r["word_end"])) for r in rows]


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score_words(
    pred: Sequence[Optional[Sequence[float]]],
    gt: Sequence[Sequence[float]],
    tol: float = 0.3,
) -> dict:
    """Compare predicted word onsets with reference onsets, in order."""
    pairs = [(p, g) for p, g in zip(pred, gt) if p is not None]
    errs = [abs(float(p[0]) - float(g[0])) for p, g in pairs]
    # 1e-9 absorbs float noise (1.3 - 1.0 == 0.30000000000000004) so a word
    # exactly on the tolerance still counts.
    within = sum(1 for e in errs if e <= tol + 1e-9)
    n = len(errs)
    return {
        "n": n,
        "coverage": n / max(1, min(len(pred), len(gt))),
        "pco": within / n if n else 0.0,
        "mae": sum(errs) / n if n else float("inf"),
    }


def summarise(rows: Iterable[dict]) -> dict:
    rows = list(rows)
    total = sum(r["n"] for r in rows)
    if not total:
        return {"tracks": len(rows), "n": 0, "pco": 0.0, "mae": float("inf")}
    return {
        "tracks": len(rows),
        "n": total,
        "pco": sum(r["pco"] * r["n"] for r in rows) / total,
        "mae": sum(r["mae"] * r["n"] for r in rows) / total,
    }


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------

def run_engine(engine: str, track: dict, out_json: Path) -> list[Optional[list[float]]]:
    """Return predicted (start, end) per lyric word, in lyric order."""
    if engine == "forced":
        from aligners.forced_align_mms import align_lyrics
    elif engine == "whisperx":
        # Call the WhisperX module directly: aligners.align() now dispatches to
        # the forced aligner first, which would measure the wrong engine.
        from aligners.alignment_whisperx import align as align_lyrics  # type: ignore
    else:
        raise SystemExit(f"unknown engine: {engine}")

    align_lyrics(track["audio"], track["lyrics"], str(out_json))
    data = json.load(open(out_json, encoding="utf-8"))
    pred: list[Optional[list[float]]] = []
    for frag in data["fragments"]:
        for w in frag.get("words", []):
            pred.append([float(w["start"]), float(w["end"])])
    return pred


def evaluate(engine: str, names: Sequence[str], tol: float = 0.3, quiet: bool = False) -> dict:
    rows = []
    for name in names:
        track = fetch_track(name)
        out_json = ROOT / "data" / "jamendo" / f"{name}.{engine}.json"
        pred = run_engine(engine, track, out_json)
        gt = load_gt(track["words"])
        row = {"track": name, **score_words(pred, gt, tol), "gt_words": len(gt), "pred_words": len(pred)}
        rows.append(row)
        if not quiet:
            print(
                f"{name[:34]:34s} pred {row['pred_words']:4d} gt {row['gt_words']:4d} "
                f"PCO@{tol} {row['pco']*100:5.1f}%  MAE {row['mae']:.3f}s"
            )
    summary = summarise(rows)
    return {"engine": engine, "tol": tol, "tracks": rows, "summary": summary}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--engine", default="forced", choices=["forced", "whisperx"])
    ap.add_argument("--tracks", nargs="*", default=None)
    ap.add_argument("--tol", type=float, default=0.3)
    ap.add_argument("--json", default=None)
    args = ap.parse_args(argv)

    result = evaluate(args.engine, args.tracks or DEFAULT_TRACKS, args.tol)
    s = result["summary"]
    print(
        f"\n{args.engine}: {s['tracks']} tracks, {s['n']} words, "
        f"PCO@{args.tol} {s['pco']*100:.1f}%, MAE {s['mae']:.3f}s"
    )
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
