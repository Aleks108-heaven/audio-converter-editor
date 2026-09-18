# Audio Toolkit

Convert, cut, trim, split, join, and mix audio files (MP3, FLAC, WAV, M4A, WMA, AAC, MP2, AMR, OGG).

## Setup

1. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Install ffmpeg (required for every format except WAV) and make sure it's on PATH:
   ```powershell
   winget install ffmpeg
   ```
   Verify with:
   ```bash
   ffmpeg -version
   ```

## GUI

```bash
python -m audio_toolkit
```

The waveform is the permanent center of the window — it's always visible,
above every tool panel, with transport controls (Play/Stop, a live time
readout, Start/End/Length) directly beneath it, the way a dedicated audio
editor is laid out. A narrow grouped tool tree (File / Edit / Effects /
Create / Record) sits to its left and swaps out only the panel to its right
— the waveform and transport never disappear.

**Empty state:** click anywhere in the waveform area (or the "Open Audio"
button in the toolbar) to load a file. Its name, sample rate, channels, and
format then show next to the transport.

Every single-file tool (Convert, Trim/Cut, Split, Effects, Equalizer,
Channels, Tags) operates on that one open file — no re-browsing for the
same input on every panel. Multi-file tools (Join, Mix, Batch Queue, and
Combine in Channels) keep their own explicit file pickers since they aren't
operating on one "current" file. **Convert / Export** is pinned in the
toolbar at all times as the one clear "next step" action.

Selecting **Trim / Cut** switches the waveform into drag-to-select mode —
drag a range and it syncs to the Start/End fields (and to Play, which then
previews just that selection). Selecting **Split** switches it into
click-to-add-markers mode, syncing to the Split-at field. Recording and
Generate automatically open their output when done, so you can immediately
trim or apply an effect to what you just created.

## CLI

```bash
# Convert
python -m audio_toolkit.cli convert in.flac out.mp3 --bitrate 192k

# Trim: keep only 0:05 - 1:30
python -m audio_toolkit.cli trim in.wav out.wav --start 0:05 --end 1:30

# Cut: remove 0:30 - 0:45, splice the rest together
python -m audio_toolkit.cli cut in.wav out.wav --start 0:30 --end 0:45

# Split at 1:00 and 2:30 into three files
python -m audio_toolkit.cli split in.mp3 out_dir/ --at 1:00 --at 2:30

# Join multiple files, with a 0.5s gap between them
python -m audio_toolkit.cli join a.mp3 b.mp3 c.mp3 -o joined.mp3 --gap 0.5

# Mix (overlay) tracks, with optional per-track gain in dB
python -m audio_toolkit.cli mix a.wav b.wav -o mixed.wav --gain -3 --gain 0

# Read/write metadata tags (MP3, FLAC, M4A, WMA, OGG only)
python -m audio_toolkit.cli get-tags song.mp3
python -m audio_toolkit.cli set-tags song.mp3 --title "My Song" --artist "Me" --album "Demo"

# Batch-rename files from their tags
python -m audio_toolkit.cli rename *.mp3 --pattern "{artist} - {title}" --out-dir renamed/

# Effects: fade-in, fade-out, normalize, reverse work on any format;
# echo, chorus, flanger, reverb require ffmpeg's audio filters
python -m audio_toolkit.cli effect fade-in in.wav out.wav --duration 1500
python -m audio_toolkit.cli effect normalize in.wav out.wav
python -m audio_toolkit.cli effect echo in.wav out.wav --delay 400 --decay 0.6
python -m audio_toolkit.cli effect reverb in.wav out.wav --decay 0.5

# Play through the default speakers (blocks until finished)
python -m audio_toolkit.cli play in.mp3

# Record from the default microphone
python -m audio_toolkit.cli record out.wav --duration 5 --channels 1

# Split a stereo file into separate left/right mono files
python -m audio_toolkit.cli split-channels stereo.wav out_dir/

# Combine two mono files into stereo
python -m audio_toolkit.cli combine-channels left.wav right.wav stereo.wav

# Adjust left/right channel gain independently
python -m audio_toolkit.cli channel-gain in.wav out.wav --left -6 --right 3

# Generate a tone, noise, or silence
python -m audio_toolkit.cli generate-tone tone.wav --freq 440 --duration 5
python -m audio_toolkit.cli generate-noise noise.wav --type pink --duration 5
python -m audio_toolkit.cli generate-silence gap.wav --duration 2 --channels 2

# List installed text-to-speech voices, then synthesize speech
python -m audio_toolkit.cli list-voices
python -m audio_toolkit.cli tts speech.wav --text "Hello there" --voice <voice-id> --rate 180

# Build a multi-chapter audiobook from a text file (blank lines split chapters)
python -m audio_toolkit.cli audiobook book.txt audiobook.mp3 --pause 1.0

# Batch: convert every file to the same format/bitrate, continuing past errors
python -m audio_toolkit.cli batch convert a.wav b.flac c.m4a -o out_dir/ --format mp3 --bitrate 192k

# Batch: apply the same effect to every file
python -m audio_toolkit.cli batch effect normalize a.wav b.wav c.wav -o out_dir/

# Graphic equalizer: boost/cut specific frequency bands (Hz:dB pairs)
python -m audio_toolkit.cli eq in.wav out.wav --gain 60:4 --gain 1000:-2 --gain 8000:3

# Simple bass/treble shelving
python -m audio_toolkit.cli bass-treble in.wav out.wav --bass 5 --treble -3
```

Times can be given as seconds (`5.5`) or `mm:ss` / `mm:ss.ms` (`1:30.250`).

## Notes

- Tagging (`get-tags`/`set-tags`/`rename`) works on MP3, FLAC, M4A, WMA, and OGG.
  WAV, AMR, and MP2 don't have a reliably supported tag format and will report a
  clear error rather than corrupt the file.
- `fade-in`, `fade-out`, `normalize`, and `reverse` run through pydub directly.
  `echo`, `chorus`, `flanger`, and `reverb` shell out to ffmpeg's `-af` filter
  graph and need ffmpeg on PATH even for WAV input.
- Recording/playback and channel splitting need `sounddevice` and `numpy`
  (installed by `requirements.txt`) plus a working audio device; `channel-gain`
  and `split-channels`/`combine-channels` require stereo input for splitting
  and mono input for combining.
- Text-to-speech (`tts`/`audiobook`/`list-voices`) uses `pyttsx3`, which is
  fully offline: SAPI5 on Windows, NSSpeechSynthesizer on macOS, espeak on
  Linux. Voice selection and available voices depend on what's installed on
  the OS — run `list-voices` to see your options.
- `batch convert`/`batch effect` (and the Batch Queue GUI tab) process every
  file in the queue even if some fail — each file's outcome is reported
  individually, and the command exits non-zero only if at least one failed.
- `eq` and `bass-treble` (and the Equalizer GUI tab) use ffmpeg's `equalizer`/
  `bass`/`treble` filters directly and need ffmpeg on PATH even for WAV input.
  The 10-band layout is 31/62/125/250/500/1000/2000/4000/8000/16000 Hz, gains
  clamped to ±12 dB.
