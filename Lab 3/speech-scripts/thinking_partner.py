#!/usr/bin/env python3
"""A thinking partner: it listens, and helps you think, but does not think for you.

This is a speech-in / speech-out device with a Socratic dialogue policy. You talk
through a problem out loud; it reflects it back, asks the question you have been
avoiding, or offers one reframe -- then hands the floor straight back to you. It
is deliberately not an answer machine. The point is to keep *you* doing the
thinking, with something that is visibly listening.

The screen shows a squiggly line that reacts to what is happening:
  - while it is LISTENING to you, the line is teal and its height follows your
    voice -- talk louder and the wave grows, so you can see you are being heard;
  - while it is THINKING (waiting on the model), the line is a slow purple pulse;
  - while it is SPEAKING back, the line is orange and rides its own voice.

    python thinking_partner.py
    python thinking_partner.py --min-silence 1.2      # give longer pauses to think
    python thinking_partner.py --model claude-haiku-4-5   # snappier replies
    python thinking_partner.py --no-display            # run without the screen

Needs ANTHROPIC_API_KEY in the environment. The screen half needs the Lab 2
display wired up; if the display libraries or hardware are missing it falls back
to printing the state and keeps working as a voice device.

Note: the boot service also draws to the screen. Stop it first so two programs
are not fighting over the display:
    sudo systemctl stop piscreen.service
"""

import argparse
import math
import os
import sys
import threading
import time
from pathlib import Path

import anthropic
import numpy as np
import sherpa_onnx
import sounddevice as sd
from faster_whisper import WhisperModel
from piper import PiperVoice

from datetime import datetime

from echo_bot import DEFAULT_VAD, DEFAULT_VOICE, SAMPLE_RATE

TRANSCRIPT_DIR = Path(__file__).resolve().parent / "transcripts"

# The display stack is optional so this still runs as a pure voice device on a
# machine without the screen (or while you are developing off the Pi).
try:
    import board
    import digitalio
    from PIL import Image, ImageDraw
    import adafruit_rgb_display.st7789 as st7789
    HAVE_DISPLAY = True
except Exception:
    HAVE_DISPLAY = False


SYSTEM_PROMPT = """You are a thinking partner running on a small speech device. \
The person is talking through a hard or unresolved problem out loud. Your job is \
to help them think.

Hard rules:
- Keep every reply to one to three sentences. It will be read aloud, so it must be \
short and easy to follow by ear.
- Prefer one good question over a list. Reflect what you heard, then ask the \
thing they seem to be stepping around, or offer a single different angle \
("what would this look like if the deadline weren't real?").
- Follow their lead. They set the topic and the pace. Do not change the subject.
- You may actually help the person solve their problem but help them think through it first
- Plain spoken language. No lists, no markdown, no emoji, no headings."""


class WaveDisplay:
    """Drives the MiniPiTFT on a background thread, drawing a reactive waveform.

    Thread-safety is deliberately loose: `state` and `level` are single
    attributes, and assigning a Python float/str is atomic under the GIL, so the
    audio loop can set them without a lock and the draw loop just reads them.
    """

    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"

    COLORS = {
        LISTENING: (0, 210, 160),   # teal: your turn
        THINKING:  (150, 110, 230),  # purple: it's working
        SPEAKING:  (255, 140, 30),   # orange: its turn
        IDLE:      (60, 70, 80),     # dim: waiting
    }

    def __init__(self) -> None:
        # Same wiring as Lab 2's screen scripts (ST7789, landscape).
        cs_pin = digitalio.DigitalInOut(board.D5)
        dc_pin = digitalio.DigitalInOut(board.D25)
        spi = board.SPI()
        self.disp = st7789.ST7789(
            spi, cs=cs_pin, dc=dc_pin, rst=None, baudrate=64000000,
            width=135, height=240, x_offset=53, y_offset=40,
        )
        self.width = self.disp.height   # 240, swapped for landscape
        self.height = self.disp.width   # 135
        self.rotation = 90

        backlight = digitalio.DigitalInOut(board.D22)
        backlight.switch_to_output()
        backlight.value = True

        self.state = self.IDLE
        self.level = 0.0            # 0..1, how loud right now
        self._smooth = 0.0          # smoothed level, so the wave doesn't jitter
        self._phase = 0.0
        self._running = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._running = True
        self._thread.start()

    def set(self, state: str, level: float | None = None) -> None:
        self.state = state
        if level is not None:
            self.level = max(0.0, min(1.0, level))

    def _run(self) -> None:
        frame_dt = 1 / 30
        while self._running:
            # Ease the drawn level toward the target so it looks springy, not
            # steppy, even though we only get a new reading every ~100 ms.
            self._smooth += (self.level - self._smooth) * 0.35
            self._phase += 0.45
            self._draw()
            time.sleep(frame_dt)

    def _draw(self) -> None:
        img = Image.new("RGB", (self.width, self.height))
        d = ImageDraw.Draw(img)
        color = self.COLORS[self.state]
        cy = self.height / 2

        if self.state == self.THINKING:
            # A calm breathing pulse -- not tied to sound, it's the device's turn.
            env = 0.25 + 0.15 * math.sin(self._phase * 0.5)
        elif self.state == self.IDLE:
            env = 0.06
        else:
            # LISTENING / SPEAKING: a floor so it's alive at silence, plus the
            # live level so louder speech makes a taller wave.
            env = 0.10 + 0.85 * self._smooth

        amp = env * (self.height * 0.42)
        points = []
        for x in range(0, self.width, 3):
            t = x / self.width
            # Two sine components at different rates read as a "squiggle" rather
            # than a clean sine, and the amplitude tapers at the edges.
            taper = math.sin(math.pi * t)
            y = cy + amp * taper * (
                0.7 * math.sin(8 * math.pi * t + self._phase)
                + 0.3 * math.sin(19 * math.pi * t - self._phase * 1.7)
            )
            points.append((x, y))
        if len(points) > 1:
            d.line(points, fill=color, width=3, joint="curve")

        self.disp.image(img, self.rotation)

    def close(self) -> None:
        self._running = False
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)
        try:
            self.disp.image(Image.new("RGB", (self.width, self.height)), self.rotation)
        except Exception:
            pass


class NullDisplay:
    """Stand-in when there is no screen: prints state changes, ignores levels."""

    def __init__(self) -> None:
        self._last = None

    def start(self) -> None:
        pass

    def set(self, state: str, level: float | None = None) -> None:
        if state != self._last:
            print(f"  [{state}]")
            self._last = state

    def close(self) -> None:
        pass


def rms_level(audio: np.ndarray, gain: float) -> float:
    """Map a chunk of float32 audio to a 0..1 loudness for the waveform."""
    if audio.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(np.square(audio))))
    return max(0.0, min(1.0, rms * gain))


class Ear:
    """Captures the mic, shows the live level, and returns one transcribed turn."""

    def __init__(self, model: str, vad_model: Path, min_silence: float,
                 display, gain: float) -> None:
        self.recognizer = WhisperModel(model, device="cpu", compute_type="int8")
        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = str(vad_model)
        config.silero_vad.min_silence_duration = min_silence
        config.sample_rate = SAMPLE_RATE
        self.vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=30)
        self.window = config.silero_vad.window_size
        self.display = display
        self.gain = gain

    def listen(self) -> str:
        """Blocks until the speaker finishes a turn; returns the transcript."""
        self.vad.reset()
        self.display.set(WaveDisplay.LISTENING, 0.0)
        buffer = np.empty(0, dtype=np.float32)
        samples_per_read = int(0.1 * SAMPLE_RATE)

        with sd.InputStream(channels=1, dtype="float32", samplerate=SAMPLE_RATE) as stream:
            while True:
                chunk, _ = stream.read(samples_per_read)
                mono = chunk.reshape(-1)
                self.display.set(WaveDisplay.LISTENING, rms_level(mono, self.gain))
                buffer = np.concatenate([buffer, mono])

                while len(buffer) > self.window:
                    self.vad.accept_waveform(buffer[:self.window])
                    buffer = buffer[self.window:]

                while not self.vad.empty():
                    utterance = np.array(self.vad.front.samples, dtype=np.float32)
                    self.vad.pop()
                    self.display.set(WaveDisplay.THINKING)
                    segments, _ = self.recognizer.transcribe(utterance, beam_size=1)
                    text = " ".join(s.text.strip() for s in segments)
                    if text:
                        return text
                    # Only noise: go back to listening.
                    self.display.set(WaveDisplay.LISTENING, 0.0)


class Mouth:
    """Synthesizes with Piper and drives the waveform from its own audio."""

    def __init__(self, voice_path: Path, display, gain: float) -> None:
        self.voice = PiperVoice.load(str(voice_path))
        self.display = display
        self.gain = gain

    def say(self, text: str) -> None:
        self.display.set(WaveDisplay.SPEAKING, 0.0)
        for chunk in self.voice.synthesize(text):
            audio = np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16)
            as_float = audio.astype(np.float32) / 32768.0
            # Louder synthesized speech -> taller orange wave, same as the mic.
            self.display.set(WaveDisplay.SPEAKING, rms_level(as_float, self.gain))
            sd.play(audio, samplerate=chunk.sample_rate)
            sd.wait()
        self.display.set(WaveDisplay.IDLE, 0.0)


class Transcript:
    """Appends the spoken conversation to a timestamped text file as it happens."""

    def __init__(self, directory: str, model: str) -> None:
        self.dir = Path(directory).expanduser()
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = self.dir / f"session-{stamp}.txt"
        self.file = open(self.path, "w", encoding="utf-8")
        self.file.write(f"# Thinking partner session {datetime.now().isoformat(timespec='seconds')}\n")
        self.file.write(f"# model: {model}\n\n")
        self.file.flush()

    def write(self, speaker: str, text: str) -> None:
        # Flush every line so a Ctrl-C still leaves a complete transcript.
        self.file.write(f"{speaker}: {text}\n")
        self.file.flush()

    def close(self) -> None:
        self.file.close()


def main() -> None:
    # The Pi's console may be a latin-1/C locale; Claude's replies contain
    # characters like the em dash. Force UTF-8 so printing never crashes.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", dest="claude_model", default="claude-opus-5",
                        help="Claude model for the dialogue (default: claude-opus-5; "
                             "try claude-haiku-4-5 for lower latency)")
    parser.add_argument("--whisper-model", default="tiny.en",
                        help="whisper model size (default: tiny.en)")
    parser.add_argument("--vad-model", type=Path, default=DEFAULT_VAD)
    parser.add_argument("--voice", type=Path, default=DEFAULT_VOICE)
    parser.add_argument("--min-silence", type=float, default=0.9,
                        help="seconds of silence that end your turn (default: 0.9; "
                             "thinking out loud has long pauses, so this is generous)")
    parser.add_argument("--gain", type=float, default=18.0,
                        help="how strongly the waveform reacts to volume (default: 18)")
    parser.add_argument("--no-display", action="store_true",
                        help="run as a voice-only device, no screen")
    parser.add_argument("--log", nargs="?", const=str(TRANSCRIPT_DIR), default=None,
                        metavar="DIR",
                        help="save a timestamped transcript. Bare --log writes to "
                             f"{TRANSCRIPT_DIR.name}/; --log DIR writes there instead.")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. Export it first:\n"
                 "  export ANTHROPIC_API_KEY=sk-ant-...")
    for path, what in [(args.vad_model, "VAD model"), (args.voice, "Piper voice")]:
        if not path.is_file():
            sys.exit(f"{what} not found at {path}. Run ./setup.sh first.")

    if args.no_display or not HAVE_DISPLAY:
        if not HAVE_DISPLAY and not args.no_display:
            print("(display libraries not found -- running voice-only)")
        display = NullDisplay()
    else:
        display = WaveDisplay()
    display.start()

    print("Loading models...", flush=True)
    ear = Ear(args.whisper_model, args.vad_model, args.min_silence, display, args.gain)
    mouth = Mouth(args.voice, display, args.gain)
    client = anthropic.Anthropic()

    transcript = Transcript(args.log, args.claude_model) if args.log else None
    if transcript:
        print(f"Logging transcript to {transcript.path}")

    history: list[dict] = []

    mouth.say("I'm here. What's on your mind?")
    print("Ready. Talk it through. Ctrl-C to stop.\n")

    try:
        while True:
            heard = ear.listen()
            print(f"  you:  {heard}")
            if transcript:
                transcript.write("you", heard)
            history.append({"role": "user", "content": heard})

            display.set(WaveDisplay.THINKING)
            response = client.messages.create(
                model=args.claude_model,
                max_tokens=200,
                system=SYSTEM_PROMPT,
                messages=history,
                output_config={"effort": "low"},  # keep the reply quick
            )
            reply = " ".join(b.text for b in response.content if b.type == "text").strip()
            if not reply:
                reply = "Say more about that."
            history.append({"role": "assistant", "content": reply})

            print(f"  it:   {reply}\n")
            if transcript:
                transcript.write("it", reply)
            mouth.say(reply)
    except KeyboardInterrupt:
        print("\nTake care.")
    finally:
        display.close()
        if transcript:
            transcript.close()


if __name__ == "__main__":
    main()
