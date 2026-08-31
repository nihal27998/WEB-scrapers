# Automated Video Dubbing System

Turns a YouTube video (any spoken language) into an English-dubbed version:
same visuals, same pacing, new English audio.

## Setup

```bash
# System dependency
sudo apt install ffmpeg      # or: brew install ffmpeg

# Python dependencies
pip install -r requirements.txt
```

If you want the IndicTrans2 translation backend (better quality for Indian
source languages), also install the optional block at the bottom of
`requirements.txt` and pass `--indictrans2`.

## Usage

```bash
python dub.py "https://www.youtube.com/watch?v=XXXXXXXX"
```

Common options:

```bash
python dub.py "<url>" \
  --outdir runs/my_video \
  --whisper-model medium \
  --voice en-US-JennyNeural \
  --indictrans2 \
  --source-lang hi
```

| Flag | Purpose |
|---|---|
| `--outdir` | Where intermediate files + final video are written (default: `runs/<timestamp>`) |
| `--whisper-model` | `tiny`/`base`/`small`/`medium`/`large-v3` — bigger = more accurate, slower |
| `--voice` | Any [edge-tts voice](https://github.com/rany2/edge-tts#voice-list) (`edge-tts --list-voices`) |
| `--indictrans2` | Use IndicTrans2 instead of the default Google-Translate fallback |
| `--source-lang` | ISO code hint for IndicTrans2 (e.g. `hi`, `ta`, `te`) |

## How it works

1. **Download** — `yt-dlp` pulls the best available video+audio.
2. **Extract audio** — `ffmpeg` isolates a 16kHz mono WAV for ASR.
3. **Transcribe** — `faster-whisper` transcribes speech *in the source
   language*, producing timestamped segments.
4. **Translate** — each segment is translated to natural English
   (IndicTrans2 for Indian languages, or Google Translate as a general
   fallback). Translating per-segment (not as one blob) keeps timestamps
   aligned with the text.
5. **Synthesize** — `edge-tts` generates English speech per segment, then
   each clip is time-stretched with `ffmpeg atempo` to fit its original
   segment's duration — this is what keeps a long video in sync.
6. **Assemble** — clips are placed on a single timeline at their original
   start times (`pydub`), silence-padding the gaps.
7. **Remux** — the new audio track replaces the original one with
   `ffmpeg -c:v copy`, so the video stream is never re-encoded.

Intermediate artifacts (`transcript.json`, `translated.json`, per-segment
clips) are cached to `--outdir`, so re-running after a crash or a tweak to
a later stage doesn't redo earlier stages.

## Known limitations / next steps

- Single synthetic voice for the whole video — no speaker diarization yet
  (see stretch goal: `pyannote.audio` for diarization + Coqui XTTS for
  per-speaker voice cloning).
- Sync is achieved by uniformly time-stretching each clip to its slot;
  extreme stretch ratios are clamped to `[0.5x, 2.0x]` to avoid
  chipmunk/slow-motion artifacts, which can cause minor drift on segments
  where the English translation is much longer/shorter than the original.
- Background music/SFX in the original audio is fully replaced along with
  the speech (no source separation) — a possible enhancement is to run
  vocal isolation (e.g. Demucs) first and mix the dub back over the
  original background track.
