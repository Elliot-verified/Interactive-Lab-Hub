#!/usr/bin/env python3
"""A camera stylist: tell it where you're going, and it critiques your outfit.

You say where you're headed and what vibe you want; it takes a photo from the
webcam, looks at what you're wearing, and gives spoken feedback -- what's working
and one or two concrete changes for the occasion. Then you can keep talking:
ask follow-ups, or say "take another look" after you change something and it
re-shoots.

This reuses the speech + screen stack from thinking_partner.py (Whisper VAD in,
Piper out, the reactive waveform on the MiniPiTFT) and adds a webcam frame plus
Claude's vision.

    python outfit_check.py
    python outfit_check.py --log            # save a transcript of the session
    python outfit_check.py --no-display     # no screen
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
from pathlib import Path

import anthropic

from thinking_partner import (
    DEFAULT_VAD, DEFAULT_VOICE, Ear, Mouth, NullDisplay, Transcript,
    WaveDisplay, HAVE_DISPLAY,
)

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

# Spoken phrases that mean "shoot a fresh photo" rather than ask about the old one.
RECAPTURE_CUES = ("look again", "take another", "another look", "new photo",
                  "new outfit", "changed", "recheck", "check again", "look now")


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
    parser.add_argument("--log", nargs="?", const="transcripts", default=None, metavar="DIR",
                        help="save a timestamped transcript of the session")
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

    def shoot() -> dict | None:
        """Take a photo; speak an apology and return None if the camera fails."""
        display.set(WaveDisplay.THINKING)
        try:
            jpeg = capture_photo(args.camera, args.image_width)
            print(f"  (captured {len(jpeg) // 1024} KB from {args.camera})")
            return image_block(jpeg)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"  camera error: {e}")
            mouth.say("I couldn't get a picture from the camera. Check that it's plugged in.")
            return None

    history: list[dict] = []

    mouth.say("Where are you headed, and what's the vibe?")
    print("Ready. Tell me the occasion, then let me look. Ctrl-C to stop.\n")

    try:
        while True:
            heard = ear.listen()
            print(f"  you:  {heard}")
            if transcript:
                transcript.write("you", heard)

            # Shoot a fresh frame on the first turn, or when they ask for a new look.
            take_new = not history or any(cue in heard.lower() for cue in RECAPTURE_CUES)
            content: list[dict] = []
            if take_new:
                mouth.say("Let me take a look.")
                photo = shoot()
                if photo:
                    content.append(photo)
            content.append({"type": "text", "text": heard})
            history.append({"role": "user", "content": content})

            display.set(WaveDisplay.THINKING)
            response = client.messages.create(
                model=args.claude_model,
                max_tokens=300,
                system=SYSTEM_PROMPT,
                messages=history,
                output_config={"effort": "low"},
            )
            reply = " ".join(b.text for b in response.content if b.type == "text").strip()
            if not reply:
                reply = "Tell me a bit more about where you're going."
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
