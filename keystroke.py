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
    FULL_CALIBRATION_MIN_REMAINING_SECONDS,
    MAX_DRIFT_PPM,
    MIN_NTP_SYNC_REMAINING_SECONDS,
    NTP_TIMEOUT_SECONDS,
    RETRY_INTERVAL_SECONDS,
    SYNC_INTERVAL_SECONDS,
    SYNC_SAMPLES,
    SYNC_SPACING_SECONDS,
    SyncedClock,
    calibrate,
    collect_single_server_samples,
    next_local_target_utc_ns,
    resolve_endpoint_before,
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
STARTUP_LISTENER_JOIN_TIMEOUT_SECONDS = 1.0


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


def _wait_for_probe_key(
    event_queue: queue.Queue[tuple[str, int]],
    expected: str,
    timeout_seconds: float,
    listener_available: bool,
    not_before_ns: Optional[int] = None,
) -> bool:
    """Wait until an absolute monotonic deadline; always time out conclusively."""
    deadline_ns = time.monotonic_ns() + int(timeout_seconds * 1_000_000_000)

    if not listener_available:
        # Preserve the full grace period even when the listener could not start,
        # then allow the caller to proceed to the second (short) probe.
        while True:
            remaining_ns = deadline_ns - time.monotonic_ns()
            if remaining_ns <= 0:
                return False
            time.sleep(remaining_ns / 1e9)

    while True:
        remaining_ns = deadline_ns - time.monotonic_ns()
        if remaining_ns <= 0:
            return False
        try:
            observed, observed_ns = event_queue.get(timeout=remaining_ns / 1e9)
        except queue.Empty:
            # Recheck the monotonic deadline rather than assuming a queue
            # timeout or OS wake-up happened at precisely the requested time.
            continue
        if (
            observed == expected
            and observed_ns <= deadline_ns
            and (not_before_ns is None or observed_ns >= not_before_ns)
        ):
            return True


def _startup_keyboard_check() -> bool:
    """Confirm terminal focus, then verify global capture of two harmless keys.

    The first synthetic Enter gets a 10-second grace period so the user can
    approve an OS permission prompt. A second Enter is then sent and must be
    observed within one second. Both waits use absolute monotonic deadlines;
    failure is non-fatal to the scheduler.
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

    print(
        f"[Startup] Sending first Enter probe (up to "
        f"{STARTUP_FIRST_GRACE_SECONDS:g} seconds for capture).",
        flush=True,
    )
    first_started_ns = time.monotonic_ns()
    first_injection_error = send_probe(STARTUP_FIRST_KEY)
    first_caught = _wait_for_probe_key(
        event_queue,
        STARTUP_FIRST_KEY,
        STARTUP_FIRST_GRACE_SECONDS,
        listener is not None,
        not_before_ns=first_started_ns,
    )

    if first_caught:
        print(f"[Startup] First probe ({STARTUP_FIRST_KEY.upper()}) captured.")
    else:
        print(
            f"[Startup] First probe was not observed within "
            f"{STARTUP_FIRST_GRACE_SECONDS:g} seconds; trying a second probe."
        )

    # This is intentionally sent immediately after the first probe's grace
    # period (or immediately after a successful first capture).
    print(
        f"[Startup] Sending second Enter probe (up to "
        f"{STARTUP_SECOND_GRACE_SECONDS:g} seconds for capture).",
        flush=True,
    )
    second_started_ns = time.monotonic_ns()
    second_injection_error = send_probe(STARTUP_SECOND_KEY)
    second_caught = _wait_for_probe_key(
        event_queue,
        STARTUP_SECOND_KEY,
        STARTUP_SECOND_GRACE_SECONDS,
        listener is not None,
        not_before_ns=second_started_ns,
    )

    listener_shutdown_error: Optional[str] = None
    if listener is not None:
        try:
            listener.stop()
            listener.join(timeout=STARTUP_LISTENER_JOIN_TIMEOUT_SECONDS)
            if listener.is_alive():
                listener_shutdown_error = (
                    "The global keyboard listener is still shutting down after "
                    f"{STARTUP_LISTENER_JOIN_TIMEOUT_SECONDS:g} second(s)."
                )
        except Exception:
            listener_shutdown_error = "Could not cleanly stop the global keyboard listener."

    if second_caught:
        print(f"[Startup] Second probe ({STARTUP_SECOND_KEY.upper()}) captured.", flush=True)

    if second_caught and listener_shutdown_error is None:
        if first_caught:
            print("[Startup] Keyboard injection and global capture verified.", flush=True)
        else:
            print(
                "[Startup] Second probe captured; the first probe was likely lost "
                "during permission/hook initialization.",
                flush=True,
            )
        if listener_shutdown_error:
            # Probe capture succeeded, so this is cleanup latency rather than
            # a keyboard-functionality failure. The listener is a daemon thread
            # and will not prevent process exit.
            print(
                "[Startup] Non-fatal: " + listener_shutdown_error + " "
                "Continuing; keyboard capture was already verified.",
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
    if listener_shutdown_error:
        problems.append(listener_shutdown_error)
    if not problems:
        problems.append(
            f"The listener did not observe the second Enter within "
            f"{STARTUP_SECOND_GRACE_SECONDS:g} seconds."
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

    # Complete any permissions/focus prompt before selecting today's target.
    if args.startup_key_test:
        _startup_keyboard_check()

    # Select the requested local wall-clock occurrence, then immediately
    # establish the system clock as the initial time source. NTP may replace
    # this model, but the scheduler never starts without a usable clock.
    target_utc_ns = next_local_target_utc_ns(args.time)
    clock = SyncedClock(MAX_DRIFT_PPM)
    clock.install_system_clock()

    target_local = datetime.fromtimestamp(target_utc_ns / 1e9).astimezone()
    target_utc = datetime.fromtimestamp(target_utc_ns / 1e9, tz=timezone.utc)
    print(f"[Scheduler] Target local: {target_local.isoformat(timespec='seconds')}")
    print(f"[Scheduler] Target UTC:   {target_utc.isoformat(timespec='seconds')}")
    print(f"[Scheduler] Key: {args.key}")

    selected_endpoint: Optional[Endpoint] = None
    offset_correction_ns = 0
    correction_uncertainty_ns = 0
    full_calibration_completed = False

    remaining_seconds = (target_utc_ns - clock.now_utc_ns()) / 1e9
    print(f"[Scheduler] Time remaining according to system clock: {remaining_seconds:.3f} s")

    # Full multi-server calibration is deliberately restricted to runs that
    # begin with more than one minute remaining. It also stops collecting at
    # T-60 s; request timeouts are bounded by that same cutoff.
    if args.calibrate and remaining_seconds > FULL_CALIBRATION_MIN_REMAINING_SECONDS:
        print(
            "[Calibration] More than 60 seconds remain; full multi-server "
            "calibration is permitted."
        )
        stop_before_mono_ns = (
            time.monotonic_ns()
            + int((remaining_seconds - FULL_CALIBRATION_MIN_REMAINING_SECONDS) * 1e9)
        )
        report_path = Path(args.calibration_report) if args.calibration_report else None
        try:
            selected = calibrate(
                CALIBRATION_SERVERS,
                samples_per_server=args.samples,
                spacing=CALIBRATION_SPACING_SECONDS,
                timeout=args.timeout,
                report_path=report_path,
                stop_before_mono_ns=stop_before_mono_ns,
            )
            selected_endpoint = selected.endpoint
            offset_correction_ns = selected.relative_bias_ns
            correction_uncertainty_ns = selected.relative_bias_uncertainty_ns
            clock.install_samples(
                selected.samples,
                offset_correction_ns=offset_correction_ns,
                correction_uncertainty_ns=correction_uncertainty_ns,
            )
            full_calibration_completed = True
        except Exception as exc:
            print(f"[Calibration] Full calibration failed: {exc}")
            print("[Clock] Retaining system time; a single-server sync may still be attempted.")
    elif args.calibrate:
        print(
            "[Calibration] Full calibration skipped: it is only allowed when "
            "more than 60 seconds remain."
        )

    # Use one server if full calibration was not requested, was too late, or
    # failed. This initial sync obeys the same T-10 s rule as every refresh.
    if not full_calibration_completed:
        remaining_seconds = (target_utc_ns - clock.now_utc_ns()) / 1e9
        if remaining_seconds > MIN_NTP_SYNC_REMAINING_SECONDS:
            print(
                f"[NTP] Using one server ({args.server}); single-server sync is "
                "permitted only while more than 10 seconds remain."
            )
            try:
                resolution_deadline = (
                    time.monotonic_ns()
                    + int(
                        (remaining_seconds - MIN_NTP_SYNC_REMAINING_SECONDS) * 1e9
                    )
                    - int(0.050 * 1e9)
                )
                selected_endpoint = resolve_endpoint_before(
                    args.server, resolution_deadline
                )
                if selected_endpoint is None:
                    print(
                        "[NTP] DNS resolution did not finish before the "
                        "10-second cutoff; retaining system time."
                    )
            except Exception as exc:
                selected_endpoint = None
                print(f"[NTP] Could not resolve {args.server}: {exc}")

            # DNS resolution can take time. Recheck the deadline before sending
            # the first UDP request.
            if selected_endpoint is not None:
                samples = collect_single_server_samples(
                    endpoint=selected_endpoint,
                    clock=clock,
                    target_utc_ns=target_utc_ns,
                    count=SYNC_SAMPLES,
                    spacing=SYNC_SPACING_SECONDS,
                    timeout=args.timeout,
                )
                if samples:
                    clock.install_samples(samples)
                else:
                    print("[NTP] No sample completed; retaining system time.")
                    clock.mark_failed_attempt()
            else:
                clock.mark_failed_attempt()
        else:
            print(
                "[NTP] Initial synchronization skipped: 10 seconds or less remain. "
                "Staying on system time."
            )

    uncertainty_ns = clock.uncertainty_ns()
    if uncertainty_ns is None:
        print("[Scheduler] Time source: system clock; uncertainty is not independently known.")
    else:
        print(f"[Scheduler] Time source: {clock.source}")
        print(
            "[Scheduler] Current uncertainty proxy: "
            f"{uncertainty_ns / 1e6:.3f} ms (not a certified bound)."
        )
    print(
        "[Scheduler] Selected NTP server: "
        f"{selected_endpoint.host if selected_endpoint else args.server}"
    )

    wait_until(
        clock=clock,
        target_utc_ns=target_utc_ns,
        endpoint=selected_endpoint,
        server_name=args.server,
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
