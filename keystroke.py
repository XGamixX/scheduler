#!/usr/bin/env python3
"""Command-line entry point for the NTP-synchronized one-shot keystroke.

Calibration and waiting live in scheduler.py. This module owns CLI parsing
and desktop keyboard injection.
"""

from __future__ import annotations

import argparse
import queue
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import pyautogui
    PYAUTOGUI_IMPORT_ERROR = None
except Exception as exc:  # Includes display/session initialization errors.
    pyautogui = None
    PYAUTOGUI_IMPORT_ERROR = exc

from scheduler import (
    CALIBRATION_SERVERS,
    CALIBRATION_SPACING_SECONDS,
    DEFAULT_CALIBRATION_SAMPLES,
    DEFAULT_SERVER,
    Endpoint,
    MAX_DRIFT_PPM,
    NTP_TIMEOUT_SECONDS,
    RETRY_INTERVAL_SECONDS,
    SYNC_INTERVAL_SECONDS,
    SYNC_SAMPLES,
    SYNC_SPACING_SECONDS,
    Sample,
    SyncedClock,
    calibrate,
    next_local_target_utc_ns,
    query_ntp,
    resolve_endpoint,
    wait_until,
)

if pyautogui is not None:
    pyautogui.PAUSE = 0

DEFAULT_TARGET = "14:30:00"
DEFAULT_KEY = "enter"

# Enter is broadly supported by desktop input backends. The initial prompt
# confirms that the terminal is focused; these two injected Enter presses
# then verify that global capture and synthetic input work.
STARTUP_FIRST_KEY = "enter"
STARTUP_SECOND_KEY = "enter"
STARTUP_FIRST_GRACE_SECONDS = 10.0
STARTUP_SECOND_GRACE_SECONDS = 1.0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--time", default=DEFAULT_TARGET, help="Local target time HH:MM:SS")
    parser.add_argument("--key", default=DEFAULT_KEY, help="PyAutoGUI key name, e.g. enter, space, f5")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="NTP server when calibration is disabled")
    parser.add_argument("--calibrate", action="store_true", help="Run multi-server directional-variation calibration")
    parser.add_argument("--samples", type=int, default=DEFAULT_CALIBRATION_SAMPLES, help="Calibration samples per server")
    parser.add_argument("--sync-interval", type=float, default=SYNC_INTERVAL_SECONDS, help="Seconds between successful refreshes")
    parser.add_argument("--retry-interval", type=float, default=RETRY_INTERVAL_SECONDS, help="Minimum delay between failed refresh attempts")
    parser.add_argument("--timeout", type=float, default=NTP_TIMEOUT_SECONDS, help="NTP UDP timeout in seconds")
    parser.add_argument("--calibration-report", default="ntp_calibration.json", help="JSON report path; use empty string to disable")
    parser.add_argument(
        "--no-startup-key-test",
        "--skip-startup-key-test",
        dest="startup_key_test",
        action="store_false",
        default=True,
        help="Skip the startup terminal-focus and keyboard-injection checks",
    )
    return parser


def _key_name(key: object) -> str:
    """Normalize pynput key objects to names such as 'enter' or 'f5'."""
    name = getattr(key, "name", None)
    if isinstance(name, str) and name:
        return name.lower()
    return str(key).replace("Key.", "").lower()


def _big_keyboard_warning(message: str) -> None:
    border = "!" * 76
    print("\n" + border)
    print("WARNING: STARTUP KEYBOARD TEST FAILED")
    print(message)
    print("The scheduler will continue, but keyboard injection/capture may not work.")
    print("Check desktop permissions, the active display session, and pynput support.")
    print(border + "\n", flush=True)


def _startup_keyboard_check() -> bool:
    """Confirm terminal focus, then verify global capture of two harmless keys.

    The first synthetic key gets a 10-second grace period so the user can
    approve an OS permission prompt. A second, different key is then sent and
    must be observed promptly. Failure is non-fatal to the scheduler.
    """
    print("[Startup] Keyboard preflight is enabled.")
    if sys.stdin.isatty():
        try:
            input("[Startup] Focus this terminal, then press Enter to continue: ")
        except EOFError:
            print("[Startup] No interactive stdin; terminal-focus confirmation skipped.")
    else:
        print("[Startup] stdin is not a terminal; terminal-focus confirmation skipped.")

    event_queue: queue.Queue[tuple[str, int]] = queue.Queue()
    listener = None
    listener_error: Optional[BaseException] = None

    try:
        from pynput import keyboard

        def on_press(key: object) -> None:
            name = _key_name(key)
            if name == STARTUP_FIRST_KEY:
                event_queue.put((name, time.monotonic_ns()))

        listener = keyboard.Listener(on_press=on_press)
        listener.start()
        # Give the hook thread a moment to initialize before injecting the probe.
        time.sleep(0.2)
        if not listener.is_alive():
            listener_error = getattr(listener, "exception", None) or RuntimeError(
                "global keyboard listener did not start"
            )
            listener = None
    except Exception as exc:
        listener_error = exc
        listener = None

    def send_probe(key_name: str) -> Optional[str]:
        try:
            pyautogui.press(key_name)
            return None
        except Exception as exc:
            return f"Could not inject {key_name.upper()}: {exc}"

    def wait_for_key(expected: str, timeout: float) -> bool:
        if listener is None:
            time.sleep(timeout)
            return False
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                observed, _timestamp = event_queue.get(timeout=remaining)
            except queue.Empty:
                return False
            if observed == expected:
                return True

    print(
        f"[Startup] Sending first Enter probe (up to "
        f"{STARTUP_FIRST_GRACE_SECONDS:.0f} seconds for capture).",
        flush=True,
    )
    first_injection_error = send_probe(STARTUP_FIRST_KEY)
    first_caught = wait_for_key(STARTUP_FIRST_KEY, STARTUP_FIRST_GRACE_SECONDS)

    if first_caught:
        print(f"[Startup] First probe ({STARTUP_FIRST_KEY.upper()}) captured.")
    else:
        print(
            f"[Startup] First probe was not observed within "
            f"{STARTUP_FIRST_GRACE_SECONDS:.0f} seconds; trying a second probe."
        )

    # This is intentionally sent immediately after the first probe's grace
    # period (or immediately after a successful first capture).
    print(
        f"[Startup] Sending second Enter probe (up to "
        f"{STARTUP_SECOND_GRACE_SECONDS:.1f} seconds for capture).",
        flush=True,
    )
    second_injection_error = send_probe(STARTUP_SECOND_KEY)
    second_caught = wait_for_key(STARTUP_SECOND_KEY, STARTUP_SECOND_GRACE_SECONDS)

    if listener is not None:
        try:
            listener.stop()
            listener.join(timeout=1.0)
        except Exception:
            pass

    if second_caught:
        if first_caught:
            print("[Startup] Keyboard injection and global capture verified.", flush=True)
        else:
            print(
                "[Startup] Second probe captured; the first probe was likely lost "
                "during permission/hook initialization.",
                flush=True,
            )
        return True

    problems = []
    if listener_error is not None:
        problems.append(f"Keyboard listener error: {listener_error}")
    if first_injection_error:
        problems.append(first_injection_error)
    if second_injection_error:
        problems.append(second_injection_error)
    if not problems:
        problems.append(
            f"The listener did not observe the second Enter within "
            f"{STARTUP_SECOND_GRACE_SECONDS:.1f} seconds."
        )
    _big_keyboard_warning("\n".join(problems))
    return False


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if pyautogui is None:
        raise SystemExit(
            "PyAutoGUI could not initialize this desktop session. Install it with "
            "`python -m pip install pyautogui` and check display/session permissions. "
            f"Underlying error: {PYAUTOGUI_IMPORT_ERROR}"
        )
    if args.samples < 3:
        parser.error("--samples must be at least 3")
    if args.sync_interval <= 0 or args.retry_interval <= 0 or args.timeout <= 0:
        parser.error("--sync-interval, --retry-interval and --timeout must be positive")

    # Perform this before time calibration so permission prompts and the
    # 10-second first-probe grace period do not consume the scheduled window.
    if args.startup_key_test:
        _startup_keyboard_check()

    # Choose the next local occurrence before calibration, so calibration time
    # itself does not accidentally push today's requested occurrence to tomorrow.
    target_utc_ns = next_local_target_utc_ns(args.time)

    clock = SyncedClock(MAX_DRIFT_PPM)
    selected_endpoint: Optional[Endpoint] = None
    offset_correction_ns = 0
    correction_uncertainty_ns = 0
    initial_samples: list[Sample] = []

    while clock.anchor_mono_ns is None:
        try:
            if args.calibrate:
                report_path = Path(args.calibration_report) if args.calibration_report else None
                selected = calibrate(
                    CALIBRATION_SERVERS,
                    samples_per_server=args.samples,
                    spacing=CALIBRATION_SPACING_SECONDS,
                    timeout=args.timeout,
                    report_path=report_path,
                )
                selected_endpoint = selected.endpoint
                offset_correction_ns = selected.relative_bias_ns
                correction_uncertainty_ns = selected.relative_bias_uncertainty_ns
                initial_samples = selected.samples
            else:
                selected_endpoint = resolve_endpoint(args.server)
                print(f"[NTP] Using {selected_endpoint.host}; multi-server calibration disabled.")
                for index in range(SYNC_SAMPLES):
                    try:
                        initial_samples.append(query_ntp(selected_endpoint, args.timeout))
                        sample = initial_samples[-1]
                        print(
                            f"  [NTP] initial {index + 1}/{SYNC_SAMPLES}: "
                            f"offset={sample.offset_ns / 1e6:+.3f} ms, "
                            f"RTT={sample.delay_ns / 1e6:.3f} ms"
                        )
                    except Exception as exc:
                        print(f"  [NTP] initial {index + 1}/{SYNC_SAMPLES} failed: {exc}")
                    if index + 1 < SYNC_SAMPLES:
                        time.sleep(SYNC_SPACING_SECONDS)

            if len(initial_samples) < 2:
                raise RuntimeError("Fewer than two successful initial NTP samples")

            clock.install_samples(
                initial_samples,
                offset_correction_ns=offset_correction_ns,
                correction_uncertainty_ns=correction_uncertainty_ns,
            )
        except Exception as exc:
            print(f"[NTP] Initial synchronization failed: {exc}")
            print("[NTP] Retrying in 5 seconds; no successful time model exists yet.")
            # Avoid repeatedly redoing a potentially expensive calibration after
            # a partial initial failure; the next pass will recalibrate.
            time.sleep(5)
            initial_samples = []
            selected_endpoint = None

    assert selected_endpoint is not None
    target_local = datetime.fromtimestamp(target_utc_ns / 1e9).astimezone()
    target_utc = datetime.fromtimestamp(target_utc_ns / 1e9, tz=timezone.utc)

    print(f"[Scheduler] Target local: {target_local.isoformat(timespec='seconds')}")
    print(f"[Scheduler] Target UTC:   {target_utc.isoformat(timespec='seconds')}")
    print(f"[Scheduler] Selected NTP server: {selected_endpoint.host}")
    print(f"[Scheduler] Key: {args.key}")
    print(
        "[Scheduler] Current uncertainty proxy: "
        f"{clock.uncertainty_ns() / 1e6:.3f} ms (not a certified bound)."
    )

    wait_until(
        clock=clock,
        target_utc_ns=target_utc_ns,
        endpoint=selected_endpoint,
        offset_correction_ns=offset_correction_ns,
        correction_uncertainty_ns=correction_uncertainty_ns,
        sync_interval=args.sync_interval,
        retry_interval=args.retry_interval,
        timeout=args.timeout,
    )

    # Avoid console output between the deadline check and input injection.
    pyautogui.press(args.key)
    print(f"[Scheduler] Injected key {args.key!r}; exiting.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n[System] Cancelled by user.")
