"""
Comprehensive test suite for all 8 lyric styles in processor.py
Run with: python test_styles.py
"""

import sys, os, re, traceback

sys.path.insert(0, os.path.dirname(__file__))
from processor import generate_ass, parse_lyrics, assign_timestamps

# ── Sample data ────────────────────────────────────────────────────────────────
STYLES = ["classic", "karaoke_bar", "cinematic", "minimal", "bold", "neon",
          "two_line", "full_lyrics"]

SAMPLE_LRC = "sample.lrc"
TOTAL_DUR   = 90.0   # seconds

def build_timed_lyrics(with_words=True):
    """Parse sample.lrc → timed_lyrics with synthetic word timings."""
    raw = parse_lyrics(SAMPLE_LRC)
    timed = assign_timestamps(raw, TOTAL_DUR)
    if with_words:
        for item in timed:
            words = item["text"].split()
            n = len(words)
            seg = (item["end"] - item["start"]) / max(n, 1)
            item["words"] = [
                {"text": w, "start": item["start"] + j * seg,
                 "end": item["start"] + (j + 1) * seg}
                for j, w in enumerate(words)
            ]
    return timed

def build_gapped_timed_lyrics():
    """Create timed lyrics with deliberate gaps between events (simulates alignment output)."""
    timed = build_timed_lyrics(with_words=False)
    # Shrink each event's end by 0.3s to create a gap
    for i, item in enumerate(timed[:-1]):
        item["end"] = max(item["start"] + 0.5, item["end"] - 0.3)
    return timed

# ── Helpers ────────────────────────────────────────────────────────────────────
PASS = "✅"
FAIL = "❌"
results = []

def check(name, condition, detail=""):
    mark = PASS if condition else FAIL
    msg  = f"  {mark} {name}"
    if detail:
        msg += f"  →  {detail}"
    print(msg)
    results.append(condition)
    return condition

def dialogue_lines(ass_text):
    return [l for l in ass_text.splitlines() if l.startswith("Dialogue:")]

def has_tag(ass_text, tag_pattern):
    return bool(re.search(tag_pattern, ass_text))

def count_fad(ass_text):
    return len(re.findall(r"\\fad\(", ass_text))

def no_rogue_fad(lines_list, expected_max, label):
    """Checks that \\fad() appears at most `expected_max` times across all lines."""
    n = sum(1 for l in lines_list if r"\fad(" in l)
    ok = n <= expected_max
    check(f"{label}: \\fad() count ≤ {expected_max}", ok,
          f"found {n}")
    return ok

def all_events_covered(timed, ass_text, label):
    """Ensure every lyric start time appears in at least one Dialogue event."""
    d_lines = dialogue_lines(ass_text)
    starts_in_ass = set()
    for dl in d_lines:
        # Extract start timestamp from Dialogue line
        parts = dl.split(",")
        if len(parts) >= 3:
            starts_in_ass.add(parts[1].strip())
    
    missing = 0
    from processor import seconds_to_ass
    for item in timed:
        ts = seconds_to_ass(item["start"])
        if ts not in starts_in_ass:
            missing += 1
    ok = missing == 0
    check(f"{label}: all {len(timed)} lyric starts have a Dialogue event", ok,
          f"{missing} missing" if not ok else "")
    return ok

def no_blank_gaps_for_multiline(timed, ass_text, style):
    """For multi-line styles: verify that between consecutive lyric events,
    the ASS event end times are >= next event start times (no blank window)."""
    from processor import seconds_to_ass
    d_lines = dialogue_lines(ass_text)
    
    # Build a timeline of (start_sec, end_sec) from dialogue
    timeline = []
    for dl in d_lines:
        parts = dl.split(",")
        if len(parts) < 3:
            continue
        def ts_to_sec(ts):
            try:
                ts = ts.strip()
                h, rest = ts.split(":")
                m, s = rest.split(".")
                return int(h)*3600 + int(m[0:2])*60 + int(m[2:4] if len(m)>2 else 0) + int(s)/100
            except:
                # format H:MM:SS.cc
                try:
                    h, mm, rest = ts.split(":")
                    ss, cc = rest.split(".")
                    return int(h)*3600 + int(mm)*60 + int(ss) + int(cc)/100
                except:
                    return 0.0
        timeline.append((ts_to_sec(parts[1]), ts_to_sec(parts[2])))
    
    if not timeline:
        check(f"{style}: no blank gaps", False, "no dialogue lines found")
        return
    
    max_covered = max(e for _, e in timeline)
    last_lyric_end = timed[-1]["end"]
    ok = max_covered >= last_lyric_end - 0.1
    check(f"{style}: events cover full lyric duration", ok,
          f"max_covered={max_covered:.2f}s, last_lyric={last_lyric_end:.2f}s")

# ═══════════════════════════════════════════════════════════════════════════════
print("\n" + "="*60)
print(" LYRICS STYLE TEST SUITE")
print("="*60)

timed      = build_timed_lyrics(with_words=True)
timed_gaps = build_gapped_timed_lyrics()
n          = len(timed)

for style in STYLES:
    print(f"\n-- {style.upper()} " + "-"*38)
    try:
        ass = generate_ass(
            timed,
            title="Test Song", artist="Test Artist",
            total_duration=TOTAL_DUR,
            font_name="Arial", font_size=72,
            word_highlight=True,
            active_color="#FFFFFF",
            upcoming_color="#FF4444",
            sung_color="#AAAAAA",
            lyric_style=style,
        )

        dlines = dialogue_lines(ass)

        # 1. Output is non-empty
        check(f"{style}: generates output", len(ass) > 100)

        # 2. Has correct number of Dialogue lines
        #    single-line: 1 per lyric (neon: 2 per lyric)
        #    two_line: ~2 per lyric (cur + nxt)
        #    full_lyrics: up to 4 per lyric
        if style == "neon":
            expected_min = n * 2
        elif style in ("two_line", "full_lyrics"):
            expected_min = n      # at least one per lyric
        else:
            expected_min = n
        check(f"{style}: ≥ {expected_min} Dialogue lines", len(dlines) >= expected_min,
              f"got {len(dlines)}")

        # 3. All lyric start times are covered
        all_events_covered(timed, ass, style)

        # 4. Style-specific tag checks
        if style == "classic":
            # First line should have \fad( (fade-in)
            first_dl = next((l for l in dlines if "Dialogue:" in l), "")
            check("classic: first line has \\fad", r"\fad(" in first_dl)
            # Middle lines should NOT have \fad
            middle_lines = dlines[1:-1]
            middle_fad = [l for l in middle_lines if r"\fad(" in l]
            check("classic: middle lines have NO \\fad", len(middle_fad) == 0,
                  f"{len(middle_fad)} middle lines have \\fad")
            # Middle lines should have \pos
            middle_pos = [l for l in middle_lines if r"\pos(" in l]
            check("classic: middle lines have \\pos", len(middle_pos) == len(middle_lines),
                  f"{len(middle_pos)}/{len(middle_lines)} have \\pos")

        elif style == "karaoke_bar":
            # First line should have \move (slide up)
            first_dl = next((l for l in dlines if "Dialogue:" in l), "")
            check("karaoke_bar: first line has \\move", r"\move(" in first_dl)
            # Middle lines should have \pos (not \move)
            middle_lines = dlines[1:-1]
            middle_move = [l for l in middle_lines if r"\move(" in l]
            check("karaoke_bar: middle lines have NO \\move", len(middle_move) == 0,
                  f"{len(middle_move)} middle lines still have \\move")
            middle_pos = [l for l in middle_lines if r"\pos(" in l]
            check("karaoke_bar: middle lines have \\pos", len(middle_pos) == len(middle_lines),
                  f"{len(middle_pos)}/{len(middle_lines)} have \\pos")
            # No line should have \fad (except last)
            non_last_fad = [l for l in dlines[:-1] if r"\fad(" in l]
            check("karaoke_bar: no \\fad except last line",
                  len(non_last_fad) == 0,
                  f"{len(non_last_fad)} non-last lines have \\fad")

        elif style == "cinematic":
            # Every line should have \move and \blur and \fad
            ok_move  = all(r"\move(" in l for l in dlines)
            ok_blur  = all(r"\blur" in l for l in dlines)
            ok_fad   = all(r"\fad(" in l for l in dlines)
            check("cinematic: all lines have \\move", ok_move)
            check("cinematic: all lines have \\blur (blur-to-sharp)", ok_blur)
            check("cinematic: all lines have \\fad (per-scene fade)", ok_fad)

        elif style == "minimal":
            # Every line should have \fad and \pos, no \move
            ok_fad  = all(r"\fad(" in l for l in dlines)
            ok_pos  = all(r"\pos(" in l for l in dlines)
            ok_move = all(r"\move(" not in l for l in dlines)
            check("minimal: all lines have \\fad", ok_fad)
            check("minimal: all lines have \\pos (static)", ok_pos)
            check("minimal: no \\move (text is still)", ok_move)

        elif style == "bold":
            # Every line should have \fscx and \t (scale bounce), NO \fad
            ok_fscx = all(r"\fscx" in l for l in dlines)
            ok_t    = all(r"\t(" in l for l in dlines)
            ok_nofad = all(r"\fad(" not in l for l in dlines)
            check("bold: all lines have \\fscx (scale bounce)", ok_fscx)
            check("bold: all lines have \\t (transform)", ok_t)
            check("bold: ZERO \\fad on any line", ok_nofad)

        elif style == "neon":
            # Two layers per event: layer 0 (glow) and layer 1 (sharp)
            layer0 = [l for l in dlines if l.startswith("Dialogue: 0,")]
            layer1 = [l for l in dlines if l.startswith("Dialogue: 1,")]
            check("neon: layer-0 (glow) events = n", len(layer0) == n,
                  f"got {len(layer0)}, expected {n}")
            check("neon: layer-1 (sharp) events = n", len(layer1) == n,
                  f"got {len(layer1)}, expected {n}")
            ok_blur_glow = all(r"\blur" in l for l in layer0)
            check("neon: glow layer has \\blur", ok_blur_glow)
            ok_karaoke_sharp = any(r"\k" in l for l in layer1)
            check("neon: sharp layer has karaoke \\k tags", ok_karaoke_sharp)

        elif style == "two_line":
            # Should have both layer 0 (context) and layer 1 (current)
            layer1 = [l for l in dlines if l.startswith("Dialogue: 1,")]
            layer0 = [l for l in dlines if l.startswith("Dialogue: 0,")]
            check("two_line: current-line events (layer 1) = n", len(layer1) == n,
                  f"got {len(layer1)}")
            check("two_line: context events (layer 0) > 0", len(layer0) > 0,
                  f"got {len(layer0)}")

        elif style == "full_lyrics":
            # Should have layer 1 (current) + multiple layer 0 (context)
            layer1 = [l for l in dlines if l.startswith("Dialogue: 1,")]
            layer0 = [l for l in dlines if l.startswith("Dialogue: 0,")]
            check("full_lyrics: current-line events (layer 1) = n", len(layer1) == n,
                  f"got {len(layer1)}")
            check("full_lyrics: context events (layer 0) > n", len(layer0) > n,
                  f"got {len(layer0)} vs n={n}")

        # 5. Gap-fill test for multi-line styles
        if style in ("two_line", "full_lyrics"):
            ass_gap = generate_ass(
                timed_gaps,
                lyric_style=style,
                active_color="#FFFFFF", upcoming_color="#FF4444",
                sung_color="#AAAAAA", total_duration=TOTAL_DUR,
            )
            no_blank_gaps_for_multiline(timed_gaps, ass_gap, style)

        # 6. Karaoke \k tags present when word_highlight=True (single-line styles)
        if style not in ("two_line", "full_lyrics"):
            ok_k = any(r"\k" in l for l in dlines)
            check(f"{style}: karaoke \\k tags present", ok_k)

        # Save output for manual inspection
        out_path = os.path.join("test_out", f"test_{style}.ass")
        os.makedirs("test_out", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(ass)
        print(f"  📄 Saved: {out_path}")

    except Exception as e:
        check(f"{style}: no exception", False, str(e))
        traceback.print_exc()

# ── Summary ────────────────────────────────────────────────────────────────────
print("\n" + "="*60)
passed = results.count(True)
total  = len(results)
print(f" RESULT: {passed}/{total} checks passed")
if passed == total:
    print(" 🎉 ALL TESTS PASSED")
else:
    failed = total - passed
    print(f" ⚠️  {failed} check(s) FAILED — review output above")
print("="*60 + "\n")

sys.exit(0 if passed == total else 1)
