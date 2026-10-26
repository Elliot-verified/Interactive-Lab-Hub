"""Pure analysis helpers for the bedside sleep-routine prototype.

The camera loop writes derived observations to JSONL.  This module deliberately
does not import camera, GPIO, or ML libraries, so reports can be tested on a
laptop and production logs never need to contain images.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Iterable


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def format_clock(value: str | None) -> str:
    if not value:
        return "unknown"
    return parse_time(value).strftime("%-I:%M %p")


def format_duration(seconds: float) -> str:
    minutes = max(0, round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} hr {minutes} min"
    if hours:
        return f"{hours} hr"
    return f"{minutes} min"


@dataclass
class SleepSummary:
    bedtime: str | None
    target_bedtime: str | None
    on_time: bool | None
    phone_put_away: str | None
    movement_events: int
    movement_score: float
    longest_still_seconds: float
    awake_sign: str | None
    sample_count: int

    def lines(self) -> list[str]:
        if self.bedtime:
            timing = "on time" if self.on_time else "later than planned"
            bed_line = f"You appeared to get into bed at {format_clock(self.bedtime)}, {timing}."
        else:
            bed_line = "I could not confidently tell when you got into bed."

        phone_line = (
            f"Your phone was last seen and then put away at {format_clock(self.phone_put_away)}."
            if self.phone_put_away
            else "I could not confidently tell when your phone was put away."
        )
        motion_line = (
            f"I noticed {self.movement_events} larger pose changes; "
            f"your longest still period was {format_duration(self.longest_still_seconds)}."
        )
        return [bed_line, phone_line, motion_line]


def load_entries(path: str | Path) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _first_sustained_lying(samples: list[dict], minimum_seconds: float = 30) -> str | None:
    start: dict | None = None
    for sample in samples:
        if sample.get("person_visible") and sample.get("lying_down"):
            start = start or sample
            if (parse_time(sample["t"]) - parse_time(start["t"])).total_seconds() >= minimum_seconds:
                return start["t"]
        else:
            start = None
    return None


def _phone_put_away(samples: list[dict]) -> str | None:
    """Return the first phone-absent sample after its final visible sample."""
    visible_indexes = [i for i, sample in enumerate(samples) if sample.get("phone_visible")]
    if not visible_indexes:
        return None
    last_visible = visible_indexes[-1]
    for sample in samples[last_visible + 1 :]:
        if not sample.get("phone_visible"):
            return sample["t"]
    return None


def _longest_still(samples: list[dict], threshold: float) -> float:
    longest = 0.0
    still_start: datetime | None = None
    previous: datetime | None = None
    for sample in samples:
        now = parse_time(sample["t"])
        is_still = (
            sample.get("person_visible")
            and sample.get("pose_motion") is not None
            and sample["pose_motion"] < threshold
        )
        if is_still:
            still_start = still_start or now
            previous = now
            longest = max(longest, (previous - still_start).total_seconds())
        else:
            still_start = None
            previous = None
    return longest


def summarize(entries: Iterable[dict], movement_threshold: float = 0.12) -> SleepSummary:
    entries = list(entries)
    samples = sorted(
        (entry for entry in entries if entry.get("type") == "sample"),
        key=lambda entry: entry["t"],
    )
    session = next((entry for entry in entries if entry.get("type") == "session_start"), {})
    bedtime = _first_sustained_lying(samples)
    target_bedtime = session.get("target_bedtime")
    on_time = None
    if bedtime and target_bedtime:
        on_time = parse_time(bedtime) <= parse_time(target_bedtime)

    movements = [
        sample["pose_motion"]
        for sample in samples
        if sample.get("pose_motion") is not None and sample["pose_motion"] >= movement_threshold
    ]
    awake = next((entry for entry in entries if entry.get("type") == "awake_sign"), None)
    return SleepSummary(
        bedtime=bedtime,
        target_bedtime=target_bedtime,
        on_time=on_time,
        phone_put_away=_phone_put_away(samples),
        movement_events=len(movements),
        movement_score=round(sum(movements), 3),
        longest_still_seconds=_longest_still(samples, movement_threshold),
        awake_sign=awake.get("reason") if awake else None,
        sample_count=len(samples),
    )
