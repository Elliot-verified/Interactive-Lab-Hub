#!/usr/bin/env python3
"""Ask for a number out loud, listen for the answer, read it back, and log it.

Numbers are a stress test for speech recognition. Whisper might hear the same
zip code as "14850", "1-4-8-5-0", "one four eight five oh", or "fourteen eight
fifty". This script records every attempt (the audio, the raw transcript, and
the digits it pulled out) so you can see those errors for yourself.

    python ask_number.py
    python ask_number.py --question "How many pets do you have?"
    python ask_number.py --question "What's your phone number?" --digits 10
    python ask_number.py --model base.en --min-silence 1.0

Every attempt is saved to responses/: a .wav of what was heard, plus a row in
responses/log.csv.
"""

import argparse
import csv
import re
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

import numpy as np
import sherpa_onnx
import sounddevice as sd
from faster_whisper import WhisperModel

from echo_bot import DEFAULT_VAD, DEFAULT_VOICE, SAMPLE_RATE, Speaker

OUT_DIR = Path(__file__).resolve().parent / "responses"

ONES = {"zero": 0, "oh": 0, "o": 0, "one": 1, "two": 2, "three": 3, "four": 4,
        "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
         "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
         "nineteen": 19}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
        "seventy": 70, "eighty": 80, "ninety": 90}
YES = {"yes", "yeah", "yep", "yup", "correct", "right", "sure"}
NO = {"no", "nope", "not", "wrong", "incorrect"}


def extract_digits(text: str) -> str:
    """Turns a transcript into a digit string.

    Handles digits ("14850"), spelled-out digits ("one four"), "oh" for zero,
    "double"/"triple" ("double five" -> "55"), and simple compounds
    ("fifty five" -> "55"). Anything it doesn't recognize is ignored.
    """
    words = re.findall(r"[a-z]+|\d+", text.lower().replace("-", " "))
    digits = []
    repeat = 1
    i = 0
    while i < len(words):
        w = words[i]
        if w.isdigit():
            digits.append(w * repeat)
            repeat = 1
        elif w in ("double", "triple"):
            repeat = 2 if w == "double" else 3
        elif w in ONES:
            digits.append(str(ONES[w]) * repeat)
            repeat = 1
        elif w in TEENS:
            digits.append(str(TEENS[w]))
        elif w in TENS:
            value = TENS[w]
            if i + 1 < len(words) and words[i + 1] in ONES and ONES[words[i + 1]]:
                value += ONES[words[i + 1]]
                i += 1
            digits.append(str(value))
        i += 1
    return "".join(digits)


def spoken(digits: str) -> str:
    """"14850" -> "1, 4, 8, 5, 0" so the voice reads it digit by digit."""
    return ", ".join(digits)


class Listener:
    """Waits for one utterance from the microphone, using the VAD to decide
    when the speaker's turn is over, then transcribes it."""

    def __init__(self, model: str, vad_model: Path, min_silence: float) -> None:
        self.recognizer = WhisperModel(model, device="cpu", compute_type="int8")
        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = str(vad_model)
        config.silero_vad.min_silence_duration = min_silence
        config.sample_rate = SAMPLE_RATE
        self.vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=30)
        self.window = config.silero_vad.window_size

    def listen(self, timeout: float = 10.0) -> tuple[np.ndarray, str, float]:
        """Returns (audio, transcript, seconds spent transcribing).
        Returns empty audio and text if nobody speaks before the timeout."""
        self.vad.reset()
        buffer = np.empty(0, dtype=np.float32)
        samples_per_read = int(0.1 * SAMPLE_RATE)
        started = time.perf_counter()

        # Opening the stream here, after the device has finished speaking,
        # keeps the device from hearing its own question.
        with sd.InputStream(channels=1, dtype="float32", samplerate=SAMPLE_RATE) as stream:
            while time.perf_counter() - started < timeout:
                chunk, _ = stream.read(samples_per_read)
                buffer = np.concatenate([buffer, chunk.reshape(-1)])
                while len(buffer) > self.window:
                    self.vad.accept_waveform(buffer[:self.window])
                    buffer = buffer[self.window:]

                while not self.vad.empty():
                    audio = np.array(self.vad.front.samples, dtype=np.float32)
                    self.vad.pop()
                    t0 = time.perf_counter()
                    segments, _ = self.recognizer.transcribe(audio, beam_size=1)
                    text = " ".join(s.text.strip() for s in segments)
                    if text:
                        return audio, text, time.perf_counter() - t0
        return np.empty(0, dtype=np.float32), "", 0.0


def save_wav(path: Path, audio: np.ndarray) -> None:
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(SAMPLE_RATE)
        f.writeframes(pcm.tobytes())


def log_attempt(row: dict) -> None:
    log_path = OUT_DIR / "log.csv"
    is_new = not log_path.exists()
    with open(log_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row))
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--question", default="What is your zip code?")
    parser.add_argument("--digits", type=int, default=None,
                        help="expected number of digits, e.g. 5 for a zip code "
                             "(default: accept any length)")
    parser.add_argument("--tries", type=int, default=3,
                        help="how many times to ask before giving up (default: 3)")
    parser.add_argument("--model", default="tiny.en",
                        help="whisper model size (default: tiny.en)")
    parser.add_argument("--vad-model", type=Path, default=DEFAULT_VAD)
    parser.add_argument("--voice", type=Path, default=DEFAULT_VOICE)
    parser.add_argument("--min-silence", type=float, default=0.8,
                        help="seconds of silence that end your turn (default: 0.8; "
                             "people pause between digit groups, so this is "
                             "longer than echo_bot's)")
    args = parser.parse_args()

    for path, what in [(args.vad_model, "VAD model"), (args.voice, "Piper voice")]:
        if not path.is_file():
            sys.exit(f"{what} not found at {path}. Run ./setup.sh first.")
    OUT_DIR.mkdir(exist_ok=True)

    print("Loading models...", flush=True)
    listener = Listener(args.model, args.vad_model, args.min_silence)
    speaker = Speaker(args.voice)
    session = datetime.now().strftime("%Y%m%d-%H%M%S")

    question = args.question
    for attempt in range(1, args.tries + 1):
        print(f"\nAsking: {question}")
        speaker.say(question)
        audio, heard, asr_time = listener.listen()

        if not heard:
            print("  (heard nothing)")
            question = f"Sorry, I didn't hear anything. {args.question}"
            continue

        digits = extract_digits(heard)
        wav_path = OUT_DIR / f"{session}-attempt{attempt}.wav"
        save_wav(wav_path, audio)
        print(f"  heard:  {heard!r}")
        print(f"  digits: {digits or '(none)'}   [asr {asr_time:.2f}s]")

        row = {"time": datetime.now().isoformat(timespec="seconds"),
               "question": args.question, "attempt": attempt, "model": args.model,
               "min_silence": args.min_silence, "transcript": heard,
               "digits": digits, "asr_seconds": round(asr_time, 2),
               "confirmed": "", "audio": wav_path.name}

        if not digits:
            question = f"Sorry, I didn't catch a number. {args.question}"
        elif args.digits and len(digits) != args.digits:
            question = (f"I heard {len(digits)} digits, but I need {args.digits}. "
                        f"{args.question}")
        else:
            speaker.say(f"I heard {spoken(digits)}. Is that right?")
            _, reply, _ = listener.listen()
            reply_words = set(re.findall(r"[a-z']+", reply.lower()))
            confirmed = bool(reply_words & YES) and not reply_words & NO
            print(f"  confirm: {reply!r} -> {'yes' if confirmed else 'no'}")
            row["confirmed"] = "yes" if confirmed else "no"
            if confirmed:
                log_attempt(row)
                speaker.say("Great, thanks. Got it.")
                print(f"\nRecorded: {digits}")
                return
            question = f"Okay, let's try again. {args.question}"

        log_attempt(row)

    speaker.say("Sorry, I couldn't get that. Let's try again later.")
    print("\nGave up after", args.tries, "tries.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
