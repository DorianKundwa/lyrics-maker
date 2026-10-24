# Lyrics alignment: measured results

**Date:** 2026-10-24
**Goal:** at least 90% of lyric words on screen within ±0.3 s of where they are sung.
**Metric:** PCO@0.3 s (share of word onsets within 0.3 s) and MAE (mean absolute onset error), the MIREX/Jamendo convention.

## Setup

- Reference: JamendoLyrics Spanish subset, word-level start/end annotations.
- Five tracks, 1485 annotated words: `10._Disparan_-_criatura`, `Guayeteo_-_JhoyKing`,
  `Caralibro_-_Vagos_Permanentes`, `Baila_-_Alfonso_Lugo`, `Te_Recuerdo_-_Wilson_Way`.
- Both engines aligned the **same mix audio** (not the Demucs stem the app uses), so the
  comparison is like-for-like and the app's numbers should be at least as good.
- Harness: `python tools/eval_alignment.py --engine forced|whisperx`.

## Results

| Track | Words | Forced (MMS_FA) | WhisperX | Delta |
|---|---|---|---|---|
| 10. Disparan - criatura | 194 | 95.9% | 29.4% | +66.5 |
| Guayeteo - JhoyKing | 340 | 97.9% | 20.6% | +77.4 |
| Caralibro - Vagos Permanentes | 211 | 76.3% | 3.3% | +73.0 |
| Baila - Alfonso Lugo | 282 | 99.6% | 3.5% | +96.1 |
| Te Recuerdo - Wilson Way | 458 | 94.1% | 68.3% | +25.8 |
| **All** | **1485** | **93.7%** | **30.8%** | **+62.9** |

Mean absolute onset error: **0.057–0.22 s** on the four good tracks, **6.48 s** on the failing one.
WhisperX MAE across the set: 7.44 s.

The 90% bar is met overall (93.7%), and every track is better than the old path,
so the "no track worse by more than 2 points" condition holds.

## Known failure: Caralibro (76.3%)

Four of five tracks are between 94% and 99.6%. `Caralibro` fails in two stretches:
lines 6–7 are sung around 80–88 s in the reference but the aligner places them at
94–134 s, and the same happens again around 215–260 s. The lyric sheet contains a line
group that the recording does not sing where the text expects it, so a monotonic
alignment has to park those words somewhere; it parks them at the next place the vocal
resumes, then re-syncs.

Every other word in that track is within 0.03 s of the reference, which is why the
overall MAE is dominated by this one track.

What now happens automatically: those lines are marked `"suspicious": true` in the
alignment JSON with a reason (`implausible pace for the line`), so a UI or a person can
spot them. The stalled lines were 2.1 s per word against 0.2–0.5 s for correctly placed
ones.

What is not solved: re-mapping a lyric group to a *different* occurrence (non-monotonic
alignment). That needs section-level matching and is out of scope for this change.

## Runtime

- Forced aligner: roughly 1–2 minutes per 2–3 minute song on CPU, plus a one-time
  ~1.2 GB MMS_FA model download.
- WhisperX path: several minutes per song.

## Reproducing

```bash
python tools/eval_alignment.py --engine forced
python tools/eval_alignment.py --engine whisperx
python -m pytest tests/ -q
```

The harness caches tracks under `data/jamendo/` (about 200 MB) and writes a JSON
summary per engine.
