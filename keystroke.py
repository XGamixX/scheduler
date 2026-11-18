#!/usr/bin/env python3
"""Command-line entry point for the NTP-synchronized one-shot keystroke.

Calibration and waiting live in scheduler.py. This module owns CLI parsing
and desktop keyboard injection.
"""

from __future__ import annotations

import argparse
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


def main() -> int:
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
