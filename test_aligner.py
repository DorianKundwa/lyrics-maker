import sys
import os
from pydub import AudioSegment
from pydub.generators import Sine

# 1. Generate dummy audio (3 seconds of 440 Hz)
print("Generating dummy audio...")
audio = Sine(440).to_audio_segment(duration=3000)
audio_path = "test_audio.wav"
audio.export(audio_path, format="wav")

# 2. Generate dummy lyrics
print("Generating dummy lyrics...")
lyrics_path = "test_lyrics.txt"
with open(lyrics_path, "w", encoding="utf-8") as f:
    f.write("Line one\nLine two\nLine three\n")

# 3. Test the aligner
try:
    from aligners import align, parse_alignment_json
    print("\n--- Testing alignment cascade ---")
    json_path = align(audio_path, lyrics_path)
    print(f"\nAlignment JSON saved to: {json_path}")
    
    print("\nParsed Output:")
    data = parse_alignment_json(json_path)
    for d in data:
        print(f"[{d['start']:.2f} - {d['end']:.2f}] {d['text']}")
    print("\n--- Test SUCCESSFUL ---")
except Exception as e:
    import traceback
    traceback.print_exc()
    print("\n--- Test FAILED ---")
finally:
    # Cleanup
    if os.path.exists(audio_path): os.remove(audio_path)
    if os.path.exists(lyrics_path): os.remove(lyrics_path)
