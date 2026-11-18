#!/usr/bin/env python3
"""Listen for a global keystroke near a target time and print system time.

This is a sanity-check listener for scheduler.py. It uses ONLY the system
wall clock for scheduling; it does not contact an NTP server.

Dependency: pynput. Start this listener before starting scheduler.py.
"""

from __future__ import annotations

import argparse
import queue
import time
from datetime import datetime, timedelta, timezone

DEFAULT_TARGET = "14:30:00"
ARM_BEFORE_SECONDS = 0.100


def next_local_target_epoch_ns(target_text: str) -> int:
    try:
        parsed = datetime.strptime(target_text, "%H:%M:%S").time()
    except ValueError as exc:
        raise ValueError("Target time must use HH:MM:SS, e.g. 14:30:00") from exc

    now = datetime.now()
    candidate = datetime.combine(now.date(), parsed)
    target_seconds = time.mktime(candidate.timetuple())
    target_ns = int(target_seconds * 1_000_000_000)

    if target_ns <= time.time_ns():
        candidate += timedelta(days=1)
        target_seconds = time.mktime(candidate.timetuple())
        target_ns = int(target_seconds * 1_000_000_000)

    round_trip = time.localtime(target_seconds)
    if (round_trip.tm_hour, round_trip.tm_min, round_trip.tm_sec) != (
        parsed.hour, parsed.minute, parsed.second
    ):
        raise ValueError(
            "The requested local time does not exist due to a daylight-saving transition"
        )

    return target_ns


def key_name(key: object) -> str:
    """Normalize pynput key objects without importing pynput at module load."""
    char = getattr(key, "char", None)
    if char:
        return str(char).lower()
    name = getattr(key, "name", None)
    if isinstance(name, str) and name:
        return name.lower()
    return str(key).replace("Key.", "").lower()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--time", default=DEFAULT_TARGET, help="Local target time HH:MM:SS")
    parser.add_argument("--key", default="enter", help="Expected key name; use '*' for any key")
    args = parser.parse_args()

    try:
        from pynput import keyboard
    except ImportError as exc:  # pragma: no cover - depends on installation
        raise SystemExit(
            "Missing dependency. Install it with: python -m pip install pynput"
        ) from exc

    target_ns = next_local_target_epoch_ns(args.time)
    arm_ns = target_ns - int(ARM_BEFORE_SECONDS * 1e9)
    received: queue.Queue[tuple[str, int]] = queue.Queue(maxsize=1)

    def on_press(key):
        # Timestamp first. Ignore events before the T-100 ms arm threshold.
        # Checking the wall clock here avoids missing a key if the main thread
        # is delayed while waking from sleep.
        received_ns = time.time_ns()
        if received_ns < arm_ns:
            return

        name = key_name(key)
        expected = args.key.lower()
        if expected != "*" and name != expected:
            return

        try:
            received.put_nowait((name, received_ns))
        except queue.Full:
            pass
        return False  # Stop listening after the first matching key.

    listener = keyboard.Listener(on_press=on_press)
    listener.start()

    target_display = datetime.fromtimestamp(target_ns / 1e9).astimezone()
    print(f"[Test] Target local system time: {target_display.isoformat(timespec='seconds')}")
    print(f"[Test] Will arm approximately {ARM_BEFORE_SECONDS * 1000:.0f} ms before target.")
    print(f"[Test] Waiting for key: {args.key}", flush=True)

    # Sleep using only the system wall clock until T - 100 ms.
    while True:
        remaining_ns = arm_ns - time.time_ns()
        if remaining_ns <= 0:
            break
        if remaining_ns > 10_000_000:
            time.sleep((remaining_ns - 5_000_000) / 1e9)
        else:
            time.sleep(remaining_ns / 1e9)

    print("[Test] ARMED. Waiting for the scheduler's keystroke...", flush=True)

    listener.join()

    try:
        name, received_ns = received.get_nowait()
    except queue.Empty:
        print("[Test] Listener stopped without capturing a matching key.")
        return 2

    local_time = datetime.fromtimestamp(received_ns / 1e9).astimezone()
    utc_time = datetime.fromtimestamp(received_ns / 1e9, tz=timezone.utc)
    lateness_ms = (received_ns - target_ns) / 1e6

    print(f"[Test] Captured key: {name}")
    print(f"[Test] Local system time: {local_time.isoformat(timespec='microseconds')}")
    print(f"[Test] UTC system time:   {utc_time.isoformat(timespec='microseconds')}")
    print(f"[Test] Unix time ns:      {received_ns}")
    print(f"[Test] Relative to target: {lateness_ms:+.3f} ms")
    print(
        "[Test] Note: this is the time the Python input callback ran, "
        "not a hardware timestamp of the original key event."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
