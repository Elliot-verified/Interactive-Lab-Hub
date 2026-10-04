#!/usr/bin/env python3
"""A camera stylist: tell it where you're going, and it critiques your outfit.

You say where you're headed and what vibe you want; it takes a photo from the
webcam, looks at what you're wearing, and gives spoken feedback -- what's working
and one or two concrete changes for the occasion. It grabs a fresh frame every
time you speak, so when you change something and ask again, it's reacting to what
you're wearing now, not a stale first photo.

This reuses the speech stack from thinking_partner.py (Whisper VAD in, Piper out)
and adds a webcam frame plus Claude's vision. By default the MiniPiTFT shows the
live camera feed so you can frame yourself, with a colored border for the device
state (teal listening, purple thinking, orange speaking). The same live frames are
what gets sent for analysis, so nothing else has to open the camera.

    python outfit_check.py
    python outfit_check.py --log            # save a transcript of the session
    python outfit_check.py --no-preview     # show the waveform instead of the feed
    python outfit_check.py --no-display     # no screen at all
    python outfit_check.py --camera /dev/video0 --image-width 768

Needs ANTHROPIC_API_KEY. Capture uses ffmpeg against a V4L2 webcam. Stop the
screen service first so two programs don't fight over the display:
    sudo systemctl stop piscreen.service
"""

import argparse
import base64
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import anthropic

from thinking_partner import (
    DEFAULT_VAD, DEFAULT_VOICE, Ear, Mouth, NullDisplay, Transcript,
    WaveDisplay, HAVE_DISPLAY,
)

# Live preview needs OpenCV (to hold the camera open) on top of the display libs.
try:
    import cv2
    from PIL import Image, ImageDraw
    import board
    import digitalio
    import adafruit_rgb_display.st7789 as st7789
    HAVE_PREVIEW = HAVE_DISPLAY
except Exception:
    HAVE_PREVIEW = False


def _camera_index(device: str) -> int:
    """Turn '/dev/video0' (or '0') into the integer index OpenCV wants."""
    digits = "".join(c for c in device if c.isdigit())
    return int(digits) if digits else 0


class CameraDisplay:
    """Shows the live webcam feed on the MiniPiTFT with a state-colored border,
    and hands the same frames to the analysis so only one program holds the camera.

    The colored border is the listening/thinking/speaking cue, same palette as the
    waveform: teal = your turn, purple = thinking, orange = it's speaking.
    """

    def __init__(self, device: str, capture_size=(1280, 720)) -> None:
        self.cap = cv2.VideoCapture(_camera_index(device), cv2.CAP_V4L2)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, capture_size[0])
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, capture_size[1])
        ok = False
        for _ in range(15):          # let the camera warm up / auto-expose
            ok, _frame = self.cap.read()
            if ok:
                break
            time.sleep(0.1)
        if not ok:
            self.cap.release()
            raise RuntimeError("could not read from the camera")

        cs_pin = digitalio.DigitalInOut(board.D5)
        dc_pin = digitalio.DigitalInOut(board.D25)
        spi = board.SPI()
        self.disp = st7789.ST7789(
            spi, cs=cs_pin, dc=dc_pin, rst=None, baudrate=64000000,
            width=135, height=240, x_offset=53, y_offset=40,
        )
        self.width = self.disp.height   # 240
        self.height = self.disp.width   # 135
        self.rotation = 90
        backlight = digitalio.DigitalInOut(board.D22)
        backlight.switch_to_output()
        backlight.value = True

        self.state = WaveDisplay.IDLE
        self._latest = None             # most recent full-res BGR frame
        self._lock = threading.Lock()
        self._running = False
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._running = True
        self._thread.start()

    def set(self, state: str, level: float | None = None) -> None:
        self.state = state

    def _run(self) -> None:
        while self._running:
            ok, frame = self.cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            with self._lock:
                self._latest = frame
            small = cv2.resize(frame, (self.width, self.height))
            rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb)
            # State-colored border over the live image.
            color = WaveDisplay.COLORS.get(self.state, WaveDisplay.COLORS[WaveDisplay.IDLE])
            d = ImageDraw.Draw(img)
            for i in range(5):
                d.rectangle([i, i, self.width - 1 - i, self.height - 1 - i], outline=color)
            self.disp.image(img, self.rotation)

    def grab_jpeg(self, width: int) -> bytes | None:
        """Encode the latest live frame as JPEG at the given width for analysis."""
        with self._lock:
            frame = None if self._latest is None else self._latest.copy()
        if frame is None:
            return None
        h = int(width * frame.shape[0] / frame.shape[1])
        resized = cv2.resize(frame, (width, h))
        ok, enc = cv2.imencode(".jpg", resized)
        return enc.tobytes() if ok else None

    def close(self) -> None:
        self._running = False
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)
        try:
            self.cap.release()
        except Exception:
            pass
        try:
            self.disp.image(Image.new("RGB", (self.width, self.height)), self.rotation)
        except Exception:
            pass

SYSTEM_PROMPT = """You are a sharp, warm stylist giving spoken feedback on an \
outfit. You are shown a photo from the person's camera and told where they are \
going and the vibe they want.

Rules:
- Keep replies short, two to four sentences -- they are read aloud.
- First say briefly what is working, then give one or two concrete changes: a \
swap, something to add, remove, tuck, roll, or adjust, tuned to the occasion and \
vibe they gave you.
- Be specific about the actual garments and colors you can see. Do not invent \
items you cannot see.
- If the photo is too dark or too far, or you cannot see the outfit, say so and \
ask them to step back into frame or fix the light instead of guessing.
- Plain spoken language. No lists, no markdown, no emoji."""

def capture_photo(device: str, width: int) -> bytes:
    """Grab one JPEG frame from the webcam and return its bytes."""
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        path = tmp.name
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-f", "v4l2", "-video_size", "1280x720", "-i", device,
             "-frames:v", "1", "-vf", f"scale={width}:-1", "-update", "1", path],
            check=True, timeout=15,
        )
        return Path(path).read_bytes()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def image_block(jpeg: bytes) -> dict:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(jpeg).decode("ascii"),
        },
    }


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", dest="claude_model", default="claude-opus-5",
                        help="Claude model (default: claude-opus-5; it must support vision)")
    parser.add_argument("--whisper-model", default="tiny.en")
    parser.add_argument("--vad-model", type=Path, default=DEFAULT_VAD)
    parser.add_argument("--voice", type=Path, default=DEFAULT_VOICE)
    parser.add_argument("--min-silence", type=float, default=0.8)
    parser.add_argument("--gain", type=float, default=18.0)
    parser.add_argument("--camera", default="/dev/video0",
                        help="V4L2 webcam device (default: /dev/video0)")
    parser.add_argument("--image-width", type=int, default=768,
                        help="downscale the photo to this width before sending (default: 768)")
    parser.add_argument("--no-display", action="store_true")
    parser.add_argument("--no-preview", action="store_true",
                        help="show the waveform instead of the live camera feed")
    parser.add_argument("--log", nargs="?", const="transcripts", default=None, metavar="DIR",
                        help="save a timestamped transcript of the session")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set. Export it first:\n"
                 "  export ANTHROPIC_API_KEY=sk-ant-...")
    for path, what in [(args.vad_model, "VAD model"), (args.voice, "Piper voice")]:
        if not path.is_file():
            sys.exit(f"{what} not found at {path}. Run ./setup.sh first.")

    # Pick the display + how we grab a photo. The live preview holds the camera
    # open via OpenCV and shares its frames, so we must NOT also shoot with ffmpeg
    # (two openers of one webcam conflict). Fall back to waveform + ffmpeg capture.
    display = None
    capture = None
    if not args.no_display and not args.no_preview and HAVE_PREVIEW:
        try:
            cam = CameraDisplay(args.camera)
            display = cam
            capture = lambda: cam.grab_jpeg(args.image_width)
            print("Live camera preview on the screen.")
        except Exception as e:
            print(f"(camera preview unavailable: {e} -- falling back)")
    if display is None:
        if args.no_display or not HAVE_DISPLAY:
            if not HAVE_DISPLAY and not args.no_display:
                print("(display libraries not found -- running voice-only)")
            display = NullDisplay()
        else:
            display = WaveDisplay()
        capture = lambda: capture_photo(args.camera, args.image_width)
    display.start()

    print("Loading models...", flush=True)
    ear = Ear(args.whisper_model, args.vad_model, args.min_silence, display, args.gain)
    mouth = Mouth(args.voice, display, args.gain)
    client = anthropic.Anthropic()

    transcript = Transcript(args.log, args.claude_model) if args.log else None
    if transcript:
        print(f"Logging transcript to {transcript.path}")

    def shoot() -> dict | None:
        """Take a photo; speak an apology and return None if the camera fails."""
        display.set(WaveDisplay.THINKING)
        try:
            jpeg = capture()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"  camera error: {e}")
            jpeg = None
        if not jpeg:
            mouth.say("I couldn't get a picture from the camera. Check that it's plugged in.")
            return None
        print(f"  (captured {len(jpeg) // 1024} KB)")
        return image_block(jpeg)

    # History holds text turns only; the live photo is attached fresh per request
    # so old frames don't accumulate in context.
    history: list[dict] = []

    mouth.say("Where are you headed, and what's the vibe?")
    print("Ready. Tell me the occasion; I take a fresh look each time you speak. "
          "Ctrl-C to stop.\n")

    try:
        while True:
            heard = ear.listen()
            print(f"  you:  {heard}")
            if transcript:
                transcript.write("you", heard)

            # Grab a current frame every turn -- this is the "live" part: change
            # your outfit, speak again, and it critiques what you're wearing now.
            photo = shoot()
            user_content: list[dict] = ([photo] if photo else []) + [
                {"type": "text", "text": heard}
            ]

            display.set(WaveDisplay.THINKING)
            response = client.messages.create(
                model=args.claude_model,
                max_tokens=300,
                system=SYSTEM_PROMPT,
                messages=history + [{"role": "user", "content": user_content}],
                output_config={"effort": "low"},
            )
            reply = " ".join(b.text for b in response.content if b.type == "text").strip()
            if not reply:
                reply = "Tell me a bit more about where you're going."

            # Store only text, so the next turn's photo is the only image in context.
            history.append({"role": "user", "content": heard})
            history.append({"role": "assistant", "content": reply})

            print(f"  it:   {reply}\n")
            if transcript:
                transcript.write("it", reply)
            mouth.say(reply)
    except KeyboardInterrupt:
        print("\nLooking good. Bye.")
    finally:
        display.close()
        if transcript:
            transcript.close()


if __name__ == "__main__":
    main()
