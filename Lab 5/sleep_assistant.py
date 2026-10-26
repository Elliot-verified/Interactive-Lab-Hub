#!/usr/bin/env python3
"""Bedside sleep-routine assistant for Raspberry Pi.

Examples:
    python sleep_assistant.py --arm-now
    python sleep_assistant.py --arm-now --start-now --max-samples 20
    python sleep_assistant.py --report night_log.jsonl

This is a routine and movement observer, not a medical sleep monitor. In
production mode it writes only derived numbers and timestamps, never frames.
"""

from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import shutil
import subprocess
import time
from typing import Any

import requests

from sleep_metrics import load_entries, summarize


DEFAULT_CONFIG = Path(__file__).with_name("sleep_config.json")


def load_config(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def combine(day, clock: str) -> datetime:
    hour, minute = map(int, clock.split(":"))
    return datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)


def schedule_near(now: datetime, bedtime: str, wake_time: str, lead_minutes: int) -> tuple[datetime, datetime]:
    """Find the active overnight schedule or the next one."""
    candidates = []
    for offset in (-1, 0, 1):
        bed = combine((now + timedelta(days=offset)).date(), bedtime)
        wake = combine(bed.date(), wake_time)
        if wake <= bed:
            wake += timedelta(days=1)
        candidates.append((bed, wake))
    lead = timedelta(minutes=lead_minutes)
    active = [pair for pair in candidates if pair[0] - lead <= now <= pair[1]]
    if active:
        return active[0]
    future = [pair for pair in candidates if pair[0] - lead > now]
    return min(future, key=lambda pair: pair[0])


class JsonlLog:
    def __init__(self, path: str | Path):
        self.handle = open(path, "a", encoding="utf-8")

    def write(self, kind: str, **values: Any) -> None:
        entry = {"t": datetime.now().isoformat(timespec="seconds"), "type": kind, **values}
        self.handle.write(json.dumps(entry) + "\n")
        self.handle.flush()

    def close(self) -> None:
        self.handle.close()


class Output:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.oled = None
        if config.get("display") == "oled":
            try:
                import adafruit_ssd1306
                import board
                self.oled = adafruit_ssd1306.SSD1306_I2C(128, 32, board.I2C())
            except (ImportError, RuntimeError, OSError) as exc:
                print(f"OLED unavailable; using console: {exc}", flush=True)

    def show(self, text: str) -> None:
        print(text, flush=True)
        if not self.oled:
            return
        from PIL import Image, ImageDraw, ImageFont

        image = Image.new("1", (self.oled.width, self.oled.height))
        draw = ImageDraw.Draw(image)
        words, lines, line = text.split(), [], ""
        for word in words:
            candidate = f"{line} {word}".strip()
            if len(candidate) > 21:
                lines.append(line)
                line = word
            else:
                line = candidate
        lines.append(line)
        draw.multiline_text((0, 0), "\n".join(lines[:3]), font=ImageFont.load_default(), fill=255)
        self.oled.image(image)
        self.oled.show()

    def speak(self, text: str) -> None:
        self.show(text)
        model = self.config.get("piper_model")
        if model and shutil.which("piper") and shutil.which("aplay"):
            piper = subprocess.Popen(
                ["piper", "--model", model, "--output-raw"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
            )
            assert piper.stdin is not None and piper.stdout is not None
            player = subprocess.Popen(
                ["aplay", "-q", "-r", "22050", "-f", "S16_LE", "-t", "raw"],
                stdin=piper.stdout,
            )
            piper.stdin.write(text.encode("utf-8"))
            piper.stdin.close()
            player.wait()
            piper.wait()
        elif shutil.which("espeak"):
            subprocess.run(["espeak", text], check=False)


def wait_for_button(config: dict[str, Any], output: Output) -> None:
    backend = config.get("button", "keyboard")
    output.show("Press the button to arm the sleep assistant.")
    if backend == "qwiic":
        try:
            import qwiic_button

            button = qwiic_button.QwiicButton(config.get("qwiic_address", 0x6F))
            if not button.begin():
                raise RuntimeError("Qwiic button was not found")
            while not button.is_button_pressed():
                time.sleep(0.05)
            button.LED_on(100)
            while button.is_button_pressed():
                time.sleep(0.05)
            return
        except (ImportError, RuntimeError, OSError) as exc:
            output.show(f"Button unavailable; using keyboard: {exc}")
    input("Press Enter to arm: ")


class Vision:
    """One camera and two local models: YOLO for phones, MediaPipe for pose."""

    POSE_POINTS = (0, 11, 12, 23, 24, 25, 26, 27, 28)

    def __init__(self, config: dict[str, Any]):
        import cv2
        import mediapipe as mp
        from ultralytics import YOLO

        self.cv2 = cv2
        self.cap = cv2.VideoCapture(config.get("camera", 0))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, config.get("frame_width", 640))
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, config.get("frame_height", 480))
        if not self.cap.isOpened():
            raise RuntimeError("No camera found")
        self.detector = YOLO(config.get("yolo_model", "yolo26n_ncnn_model"))
        self.pose = mp.solutions.pose.Pose(
            static_image_mode=False,
            model_complexity=0,
            min_detection_confidence=config.get("pose_confidence", 0.5),
            min_tracking_confidence=config.get("pose_confidence", 0.5),
        )
        self.previous_pose: dict[int, tuple[float, float]] | None = None

    def sample(self, config: dict[str, Any]) -> tuple[dict[str, Any], Any]:
        for _ in range(3):
            self.cap.grab()
        ok, frame = self.cap.read()
        if not ok:
            raise RuntimeError("Could not read camera frame")

        result = self.detector.predict(
            frame,
            imgsz=config.get("image_size", 320),
            conf=config.get("object_confidence", 0.35),
            classes=[0, 67],  # COCO: person and cell phone
            verbose=False,
        )[0]
        names = [self.detector.names[int(box.cls)] for box in result.boxes]
        rgb = self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2RGB)
        pose_result = self.pose.process(rgb)
        points = self._visible_points(pose_result)
        motion = self._motion(points)
        observation = {
            "person_visible": "person" in names,
            "phone_visible": "cell phone" in names,
            "lying_down": self._lying_down(points),
            "pose_motion": round(motion, 4) if motion is not None else None,
            "brightness": round(float(self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2GRAY).mean()), 1),
        }
        if points:
            self.previous_pose = points
        return observation, frame

    def _visible_points(self, result) -> dict[int, tuple[float, float]]:
        if not result.pose_landmarks:
            return {}
        landmarks = result.pose_landmarks.landmark
        return {
            index: (landmarks[index].x, landmarks[index].y)
            for index in self.POSE_POINTS
            if landmarks[index].visibility >= 0.45
        }

    def _motion(self, points: dict[int, tuple[float, float]]) -> float | None:
        if not points or not self.previous_pose:
            return None
        common = points.keys() & self.previous_pose.keys()
        if len(common) < 4:
            return None
        scale = 1.0
        if 11 in points and 12 in points:
            scale = max(0.05, math.dist(points[11], points[12]))
        distances = [math.dist(points[index], self.previous_pose[index]) / scale for index in common]
        return sum(distances) / len(distances)

    @staticmethod
    def _lying_down(points: dict[int, tuple[float, float]]) -> bool | None:
        required = {11, 12, 23, 24}
        if not required <= points.keys():
            return None
        shoulders = ((points[11][0] + points[12][0]) / 2, (points[11][1] + points[12][1]) / 2)
        hips = ((points[23][0] + points[24][0]) / 2, (points[23][1] + points[24][1]) / 2)
        dx, dy = hips[0] - shoulders[0], hips[1] - shoulders[1]
        angle_from_horizontal = abs(math.degrees(math.atan2(dy, dx)))
        angle_from_horizontal = min(angle_from_horizontal, 180 - angle_from_horizontal)
        return angle_from_horizontal <= 35

    def close(self) -> None:
        self.pose.close()
        self.cap.release()


def get_weather(config: dict[str, Any]) -> str:
    location = config.get("location", {})
    if "latitude" not in location or "longitude" not in location:
        return "Weather is not configured."
    try:
        response = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": location["latitude"],
                "longitude": location["longitude"],
                "current": "temperature_2m,weather_code",
                "temperature_unit": location.get("temperature_unit", "fahrenheit"),
            },
            timeout=10,
        )
        response.raise_for_status()
        current = response.json()["current"]
        code = current["weather_code"]
        condition = (
            "clear" if code == 0
            else "partly cloudy" if code <= 3
            else "foggy" if code <= 48
            else "rainy" if code <= 67 or 80 <= code <= 82
            else "snowy" if code <= 77 or 85 <= code <= 86
            else "stormy"
        )
        return f"It is currently {round(current['temperature_2m'])} degrees and {condition} outside."
    except (requests.RequestException, KeyError, ValueError):
        return "I could not retrieve the weather."


def collect_reminder(config: dict[str, Any], output: Output) -> str | None:
    output.speak("Would you like a reminder in the morning?")
    if config.get("reminder_input", "terminal") != "terminal":
        return None
    try:
        reminder = input("Morning reminder (leave blank for none): ").strip()
        return reminder or None
    except EOFError:
        return None


def save_report(log_path: str | Path, config: dict[str, Any], output: Output) -> None:
    summary = summarize(load_entries(log_path), config.get("movement_threshold", 0.12))
    reminder = next(
        (entry.get("text") for entry in load_entries(log_path) if entry.get("type") == "reminder"),
        None,
    )
    lines = [get_weather(config), *summary.lines()]
    if reminder:
        lines.append(f"Reminder: {reminder}")
    report = "\n".join(lines)
    Path(config.get("report_file", "morning_report.txt")).write_text(report + "\n", encoding="utf-8")
    for line in lines:
        output.speak(line)


def run(config: dict[str, Any], args: argparse.Namespace) -> None:
    output = Output(config)
    if not args.arm_now:
        wait_for_button(config, output)

    now = datetime.now()
    bed, wake = schedule_near(now, config["bedtime"], config["wake_time"], config["pre_bed_minutes"])
    monitor_start = now if args.start_now else bed - timedelta(minutes=config["pre_bed_minutes"])
    output.speak(f"Armed. Monitoring starts at {monitor_start:%-I:%M %p}.")
    while datetime.now() < monitor_start:
        time.sleep(min(30, (monitor_start - datetime.now()).total_seconds()))

    log = JsonlLog(config.get("log_file", "night_log.jsonl"))
    log.write(
        "session_start",
        target_bedtime=bed.isoformat(timespec="seconds"),
        target_wake=wake.isoformat(timespec="seconds"),
        development_mode=config.get("development_mode", True),
    )
    vision = Vision(config)
    brightness = deque(maxlen=max(2, config.get("brightness_baseline_samples", 12)))
    asked_for_reminder = False
    awake_logged = False
    samples = 0
    image_dir = Path(config.get("image_directory", "sleep_debug"))
    if config.get("development_mode"):
        image_dir.mkdir(exist_ok=True)

    try:
        while datetime.now() < wake:
            observation, frame = vision.sample(config)
            log.write("sample", **observation)
            samples += 1

            now = datetime.now()
            within_bedtime_hour = abs((now - bed).total_seconds()) <= 3600
            if observation["lying_down"] and within_bedtime_hour and not asked_for_reminder:
                reminder = collect_reminder(config, output)
                if reminder:
                    log.write("reminder", text=reminder)
                asked_for_reminder = True

            baseline = sum(brightness) / len(brightness) if brightness else observation["brightness"]
            brightness.append(observation["brightness"])
            near_wake = wake - now <= timedelta(minutes=config.get("wake_detection_minutes", 60))
            light_on = observation["brightness"] - baseline >= config.get("light_change_threshold", 25)
            moved = (observation["pose_motion"] or 0) >= config.get("awake_motion_threshold", 0.3)
            if near_wake and not awake_logged and (light_on or moved):
                reason = "light turned on" if light_on else "large pose change"
                log.write("awake_sign", reason=reason)
                awake_logged = True

            if config.get("development_mode") and (
                samples == 1 or samples % config.get("debug_image_every_samples", 12) == 0
            ):
                stamp = now.strftime("%Y%m%d-%H%M%S")
                vision.cv2.imwrite(str(image_dir / f"{stamp}.jpg"), frame)

            if args.max_samples and samples >= args.max_samples:
                break
            time.sleep(config.get("sample_interval_seconds", 5))
    except KeyboardInterrupt:
        output.show("Monitoring stopped.")
    finally:
        vision.close()
        log.write("session_end", samples=samples)
        log.close()

    if datetime.now() >= wake or args.report_after:
        output.speak("Good morning. It is time to wake up.")
        save_report(config.get("log_file", "night_log.jsonl"), config, output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--arm-now", action="store_true", help="bypass the physical button")
    parser.add_argument("--start-now", action="store_true", help="bypass the bedtime wait")
    parser.add_argument("--max-samples", type=int, help="stop after N samples (development)")
    parser.add_argument("--report-after", action="store_true", help="present a report after a short test")
    parser.add_argument("--report", metavar="LOG", help="only present a report from an existing log")
    args = parser.parse_args()
    config = load_config(args.config)
    output = Output(config)
    if args.report:
        save_report(args.report, config, output)
    else:
        run(config, args)


if __name__ == "__main__":
    main()
