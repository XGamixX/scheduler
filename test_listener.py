#!/usr/bin/env python3
"""System-clock-only listener for testing a scheduled synthetic keystroke.

It arms a global keyboard listener at approximately T-100 ms, waits for the
specified key, and prints the local/UTC system time observed by the callback.
It does not contact NTP. Dependency: pynput.
"""

from __future__ import annotations

import argparse
import queue
import time
from datetime import datetime, timedelta, timezone

ARM_BEFORE_SECONDS = 0.100


def next_local_target_epoch_ns(target_text: str) -> int:
    try:
        parsed = datetime.strptime(target_text, "%H:%M:%S").time()
    except ValueError as exc:
        raise ValueError("Target time must use HH:MM:SS, e.g. 14:30:00") from exc

    now_ns = time.time_ns()
    candidate = datetime.combine(datetime.now().date(), parsed)
    target_seconds = time.mktime(candidate.timetuple())
    target_ns = int(target_seconds * 1_000_000_000)

    if target_ns <= now_ns:
        candidate += timedelta(days=1)
        target_seconds = time.mktime(candidate.timetuple())
        target_ns = int(target_seconds * 1_000_000_000)

    round_trip = time.localtime(target_seconds)
    if (round_trip.tm_hour, round_trip.tm_min, round_trip.tm_sec) != (
        parsed.hour, parsed.minute, parsed.second
    ):
        raise ValueError(
            "Requested local time does not exist due to a daylight-saving transition"
        )
    return target_ns


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--time", default="14:30:00", help="Local target time HH:MM:SS")
    parser.add_argument("--key", default="enter", help="Expected key name; use '*' for any key")
    args = parser.parse_args()

    try:
        from pynput import keyboard
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency. Install it with: python -m pip install pynput"
        ) from exc

    def key_name(key) -> str:
        if isinstance(key, keyboard.KeyCode):
            return (key.char or "").lower()
        return str(key).replace("Key.", "").lower()

    target_ns = next_local_target_epoch_ns(args.time)
    arm_ns = target_ns - int(ARM_BEFORE_SECONDS * 1e9)
    received: queue.Queue[tuple[str, int]] = queue.Queue(maxsize=1)

    def on_press(key):
        # Timestamp in the callback immediately, before formatting or printing.
        observed_ns = time.time_ns()
        if observed_ns < arm_ns:
            return

        name = key_name(key)
        if args.key != "*" and name != args.key.lower():
            return

        try:
            received.put_nowait((name, observed_ns))
        except queue.Full:
            pass
        return False

    listener = keyboard.Listener(on_press=on_press)
    listener.start()

    target_local = datetime.fromtimestamp(target_ns / 1e9).astimezone()
    print(f"[Test] Target local system time: {target_local.isoformat(timespec='seconds')}")
    print(f"[Test] Arming at approximately T-{ARM_BEFORE_SECONDS * 1000:.0f} ms.")
    print(f"[Test] Waiting for key: {args.key}", flush=True)

    # Deliberately uses only time.time_ns() for the wait, as requested.
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
        name, observed_ns = received.get_nowait()
    except queue.Empty:
        print("[Test] Listener stopped without capturing a matching key.")
        return 2

    local_time = datetime.fromtimestamp(observed_ns / 1e9).astimezone()
    utc_time = datetime.fromtimestamp(observed_ns / 1e9, tz=timezone.utc)
    lateness_ms = (observed_ns - target_ns) / 1e6

    print(f"[Test] Captured key: {name}")
    print(f"[Test] Local system time: {local_time.isoformat(timespec='microseconds')}")
    print(f"[Test] UTC system time:   {utc_time.isoformat(timespec='microseconds')}")
    print(f"[Test] Unix time ns:      {observed_ns}")
    print(f"[Test] Relative to target: {lateness_ms:+.3f} ms")
    print(
        "[Test] This timestamps callback processing, not the original hardware "
        "or kernel input-event timestamp."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
