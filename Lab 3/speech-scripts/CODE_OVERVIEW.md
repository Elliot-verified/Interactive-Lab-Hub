# Lab 3 speech-scripts — code overview

This folder holds the speech-enabled devices for Lab 3. Everything runs **on the
Raspberry Pi**, in the Lab 3 virtual environment (`../.venv`), and nothing in the
listen/transcribe/speak path leaves the Pi — only the two Claude-powered devices
(`thinking_partner.py`, `outfit_check.py`) make a network call, and only to the
Claude API.

## The shared pipeline

Every interactive device here is built from the same four stages:

```
mic → VAD (endpointing) → Whisper (speech→text) → policy → Piper (text→speech) → speaker
                                                      │
                               (thinking_partner / outfit_check: Claude API)
```

- **VAD / endpointing** — a Silero voice-activity detector (via `sherpa-onnx`)
  watches the mic stream and decides *when your turn is over*, i.e. after enough
  silence. The amount of silence is the `--min-silence` knob.
- **Speech→text** — `faster-whisper` transcribes each finished utterance on CPU.
  Model size (`tiny.en` … `small.en`) trades accuracy for latency.
- **Policy** — what the device *does* with your words. This is the only part that
  differs between devices: echo it back, log a number, or call Claude.
- **Text→speech** — `piper` (a small neural TTS) speaks the reply.

## Files

### Provided by the course (starting points)

| File | What it does |
|---|---|
| `transcribe.py` | Transcribes a `.wav` and reports the real-time factor. Used for the Part B model-size comparison. |
| `listen.py` | Live loop: VAD-segments the mic and transcribes each utterance. Demonstrates the `--min-silence` endpointing knob. |
| `echo_bot.py` | The full loop end to end, with a trivial "repeat what you said" policy. Defines the shared constants and the `Speaker` class the other devices build on. |
| `setup.sh` | One-time setup: installs the classic TTS engines and pre-fetches the VAD model, a Piper voice, and a Whisper model. |

`echo_bot.py` is also the **library** the devices below import from:
`SAMPLE_RATE`, `DEFAULT_VAD`, `DEFAULT_VOICE`, and the `Speaker` class.

### Devices built for this lab

#### `ask_number.py` — Part B number prompt
Verbally asks for a number (zip code, phone number, pet count), transcribes the
answer, and reads it back to confirm.

- `extract_digits()` turns messy transcripts into a digit string — it handles
  spelled-out digits ("one four"), `"oh"` for zero, `"double"/"triple"`, and
  simple compounds ("fifty five" → `55`).
- Every attempt is saved to `responses/`: the recorded `.wav` plus a row in
  `responses/log.csv` (transcript, digits found, model, ASR time, confirmed?).
  That log is the evidence for *how speech recognition mangles digit strings*.
- Re-asks up to `--tries` times if it hears nothing, the wrong digit count, or a
  "no" on confirmation.

#### `thinking_partner.py` — a Socratic voice device
You talk through a hard problem out loud; it reflects, asks the question you're
avoiding, and (per the tuned prompt) can help you work toward a solution after
helping you think. Keeps conversation history, so it's a real dialogue.

This file also defines the **reusable components** that `outfit_check.py` imports:

| Class / func | Role |
|---|---|
| `Ear` | Mic capture + VAD + Whisper. `listen()` blocks until your turn ends and returns the transcript. Updates the display's live level while listening. |
| `Mouth` | Piper synthesis + playback. Drives the display level from its own audio while speaking. |
| `WaveDisplay` | Background thread that draws a reactive **squiggly waveform** on the MiniPiTFT (ST7789, 240×135 landscape). Color = state (teal listening, purple thinking, orange speaking); height follows the live audio level. |
| `NullDisplay` | Stand-in when there's no screen (prints state changes). Lets every device run voice-only. |
| `Transcript` | `--log` support: appends a timestamped, flushed transcript per session to `transcripts/`. |
| `rms_level()` | Maps a chunk of audio to a 0–1 loudness for the waveform. |

The system prompt (`SYSTEM_PROMPT`) is the dialogue policy: short spoken replies,
one good question over a list, follow the user's lead. It was tuned after a
classmate found pure questioning too withholding (see the Part E write-up).

Model: `claude-opus-5` by default (`--model claude-haiku-4-5` for lower latency),
adaptive thinking at `effort: "low"` to stay responsive in a voice loop.

#### `outfit_check.py` — a camera stylist (vision)
Tell it where you're going and the vibe; it photographs your outfit and speaks
feedback — what's working and one or two concrete changes — using Claude's
**vision**. It reuses `Ear`, `Mouth`, `Transcript`, and the display classes from
`thinking_partner.py`, and adds:

- **A fresh frame every turn.** Each time you speak it grabs a current photo, so
  changing your outfit and asking again critiques what you're wearing *now*. Only
  text is kept in conversation history, so old frames don't pile up in context.
- **`CameraDisplay`** — the default screen mode. It holds the webcam open with
  OpenCV, shows the **live feed** on the MiniPiTFT with a state-colored border,
  and `grab_jpeg()` hands the *same* frames to the analysis. This matters: a
  webcam can only be opened by one program at a time, so the preview and the
  capture must share one pipeline. With `--no-preview` (or no OpenCV) it falls
  back to the `WaveDisplay` and captures stills with `ffmpeg` (`capture_photo()`).
- **The image payload** — `image_block()` base64-encodes the JPEG into the
  Claude message, placed before the text block.

## Running them

On the Pi, in this folder, with the venv active and your key exported:

```bash
cd ~/Interactive-Lab-Hub/Lab\ 3
source .venv/bin/activate
export ANTHROPIC_API_KEY=sk-ant-...        # only the Claude devices need this
sudo systemctl stop piscreen.service        # free the screen (until reboot)

python speech-scripts/thinking_partner.py --log
python speech-scripts/outfit_check.py --log
```

Common flags: `--min-silence <sec>` (endpointing), `--no-display` (voice only),
`--log` (save a transcript), and for the stylist `--no-preview` (waveform instead
of the live feed) and `--image-width <px>` (detail vs. speed).

## Dependencies

Pinned in `../requirements.txt`. Beyond the base speech stack (`sounddevice`,
`faster-whisper`, `sherpa-onnx`, `piper-tts`): `anthropic` (Claude), the Adafruit
display stack plus `lgpio` (the Pi 5 GPIO backend — install the PyPI `lgpio`,
*not* `adafruit-lgpio`, which has no wheel for this architecture), `pillow`, and
`opencv-python-headless` (live preview + capture).

## Privacy

`transcripts/` and `responses/` are git-ignored — they can contain personal
speech and recordings, so they stay on the Pi and are never pushed.
