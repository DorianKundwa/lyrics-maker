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

What is not solved by forced alignment alone: re-mapping a lyric group to a
*different* occurrence (non-monotonic alignment). The combo engine below now
handles the common case by searching back from the previous line; a line whose
only correct occurrence is more than 15 s from that anchor is still left flagged.

## Runtime

- Forced aligner: roughly 1–2 minutes per 2–3 minute song on CPU, plus a one-time
  ~1.2 GB MMS_FA model download.
- WhisperX path: several minutes per song.

## Combo repair (measured 2026-11-05)

The default engine is now `combo`: MMS_FA still places every known word, and only
the lines its own checks flag (straddles a silence, impossible pace, duplicate
placement) are re-checked with faster-whisper. The repair window starts at the end
of the previous line instead of the flagged line's own forced start, because a
parked line sits after the point where it is actually sung. A repair is accepted
only when the transcript confirms at least 60% of that line's words. The repair
language comes from the lyrics (`langdetect`), since short windows and isolated
vocals misdetect the language (Spanish singing was read as English, then Basque).

**Same five mix tracks, same harness** (`--engine combo`):

| Track | Words | Forced | Combo | Combo MAE |
|---|---|---|---|---|
| 10. Disparan - criatura | 194 | 95.9% | 95.9% | 0.057 s |
| Guayeteo - JhoyKing | 340 | 97.9% | 97.9% | 0.054 s |
| Caralibro - Vagos Permanentes | 211 | 76.3% | **80.6%** | 4.443 s |
| Baila - Alfonso Lugo | 282 | 99.6% | 99.6% | 0.027 s |
| Te Recuerdo - Wilson Way | 458 | 94.1% | 94.1% | 0.218 s |
| **All** | **1485** | **93.7%** | **94.3%** | **0.724 s** |

Three Caralibro lines were repaired; the other four tracks produced no flagged
lines, so no ASR window ran and their timings are unchanged.

**Vocal stem (the app's own path).** Demucs `htdemucs` on Caralibro, then
alignment on `vocals.wav`: forced **99.5%** PCO@0.3, MAE **0.053 s** (the mix was
76.3%). One line remained flagged and the repair had nothing to accept, so combo
is also 99.5%. The documented Caralibro failure is therefore mostly an artifact
of aligning the full mix; the app's stem path is already at the ceiling for this
track.

Runtime on CPU for the 4.6-minute Caralibro track: Demucs 224.8 s, forced
201.5 s. The combo run (forced stage plus the language vote and the repair
windows) took 151–164 s on warm caches. Clean tracks pay no speech recognition
at all; the only extra cost is the six-window language vote.

## Reproducing

```bash
python tools/eval_alignment.py --engine forced
python tools/eval_alignment.py --engine combo
python tools/eval_alignment.py --engine whisperx
python -m pytest tests/ -q
```

The harness caches tracks under `data/jamendo/` (about 200 MB) and writes a JSON
summary per engine.
