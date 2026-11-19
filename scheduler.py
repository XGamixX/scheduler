#!/usr/bin/env python3
"""NTP calibration, clock modelling and monotonic waiting primitives.

This module deliberately contains no command-line interface and does not
inject keyboard events. Use keystroke.py as the command-line entry point.

Optional calibration probes multiple NTP servers repeatedly. It estimates
*changes* in forward and reverse path delay from paired RTT/offset changes,
then estimates a robust relative offset bias for each server against the
cross-server median. The selected server is used for later clock refreshes.

The calibration does not identify absolute one-way delays or absolute local
path asymmetry. Its relative-bias correction depends on the explicit heuristic
that independent servers' residual server/path biases are reasonably centred.

Python 3.9+. Standard-library only.
"""

from __future__ import annotations

import json
import queue
import socket
import statistics
import struct
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_SERVER = "time.cloudflare.com"
CALIBRATION_SERVERS = (
    "time.cloudflare.com",
    "time.google.com",
    "time.windows.com",
    "time.apple.com",
    "pool.ntp.org",
)

DEFAULT_CALIBRATION_SAMPLES = 12
CALIBRATION_SPACING_SECONDS = 0.15
SYNC_SAMPLES = 5
SYNC_SPACING_SECONDS = 0.15
NTP_TIMEOUT_SECONDS = 1.5
SYNC_INTERVAL_SECONDS = 30.0
RETRY_INTERVAL_SECONDS = 5.0
SPIN_WINDOW_SECONDS = 0.100
FULL_CALIBRATION_MIN_REMAINING_SECONDS = 60.0
MIN_NTP_SYNC_REMAINING_SECONDS = 10.0
NTP_REQUEST_CUTOFF_MARGIN_SECONDS = 0.050
SYSTEM_OFFSET_WARNING_MS = 1_000.0
MAX_DRIFT_PPM = 50.0  # Conservative growth allowance, not a hardware guarantee.
CLOCK_STEP_REJECTION_NS = 5_000_000
NTP_EPOCH = 2_208_988_800
NTP_ERA_SECONDS = 1 << 32


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Endpoint:
    host: str
    family: int
    socktype: int
    proto: int
    sockaddr: tuple

    @property
    def identity(self) -> tuple:
        # Deduplicate aliases that resolved to the same IP/port.
        return (self.family, self.sockaddr)


@dataclass(frozen=True)
class Sample:
    server: str
    offset_ns: int             # Server time minus client wall clock.
    delay_ns: int              # NTP round-trip network delay estimate.
    root_distance_ns: int      # Server-reported root distance approximation.
    wall_mid_ns: int
    mono_mid_ns: int

    @property
    def utc_minus_mono_ns(self) -> int:
        # A mapping from the monotonic clock to UTC, using this raw NTP sample.
        return self.wall_mid_ns + self.offset_ns - self.mono_mid_ns


@dataclass
class ServerCalibration:
    endpoint: Endpoint
    samples: list[Sample]
    median_offset_ns: int
    low_delay_reference_utc_minus_mono_ns: int
    median_delay_ns: int
    min_delay_ns: int
    root_distance_ns: int
    offset_jitter_ns: int
    forward_jitter_ns: int
    reverse_jitter_ns: int
    relative_bias_ns: int = 0
    relative_bias_uncertainty_ns: int = 0
    score_ns: int = 0


# ---------------------------------------------------------------------------
# NTP timestamp and transport
# ---------------------------------------------------------------------------

def ntp_timestamp_from_unix_ns(unix_ns: int) -> bytes:
    seconds, nanoseconds = divmod(unix_ns, 1_000_000_000)
    ntp_seconds = (seconds + NTP_EPOCH) & 0xFFFFFFFF
    ntp_fraction = (nanoseconds << 32) // 1_000_000_000
    return struct.pack("!II", ntp_seconds, ntp_fraction)


def ntp_timestamp_to_unix_ns(data: bytes) -> int:
    seconds32, fraction = struct.unpack("!II", data)
    # Resolve the 32-bit NTP seconds era nearest to local system time.
    current_ntp_seconds = time.time_ns() // 1_000_000_000 + NTP_EPOCH
    era = round((current_ntp_seconds - seconds32) / NTP_ERA_SECONDS)
    full_ntp_seconds = seconds32 + era * NTP_ERA_SECONDS
    fractional_ns = (fraction * 1_000_000_000) >> 32
    return (full_ntp_seconds - NTP_EPOCH) * 1_000_000_000 + fractional_ns


def resolve_endpoint(host: str) -> Endpoint:
    infos = socket.getaddrinfo(host, 123, type=socket.SOCK_DGRAM)
    for family, socktype, proto, _canonname, sockaddr in infos:
        if family in (socket.AF_INET, socket.AF_INET6):
            return Endpoint(host, family, socktype, proto, sockaddr)
    raise OSError(f"No usable UDP address found for {host}")


def resolve_endpoint_before(host: str, deadline_mono_ns: int) -> Optional[Endpoint]:
    """Resolve a host without letting a blocking system DNS call cross a deadline.

    getaddrinfo() has no portable timeout parameter. Run it in a daemon thread
    and stop waiting at the supplied monotonic deadline. A slow resolver thread
    may finish later, but cannot hold up the scheduled keystroke.
    """
    if deadline_mono_ns <= time.monotonic_ns():
        return None

    result_queue: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def worker() -> None:
        try:
            result_queue.put((True, resolve_endpoint(host)))
        except Exception as exc:
            result_queue.put((False, exc))

    threading.Thread(
        target=worker,
        name=f"dns-resolve-{host}",
        daemon=True,
    ).start()

    while True:
        remaining_ns = deadline_mono_ns - time.monotonic_ns()
        if remaining_ns <= 0:
            return None
        try:
            succeeded, value = result_queue.get(
                timeout=min(0.050, remaining_ns / 1e9)
            )
        except queue.Empty:
            continue
        if succeeded:
            assert isinstance(value, Endpoint)
            return value
        assert isinstance(value, Exception)
        raise value


def query_ntp(endpoint: Endpoint, timeout: float) -> Sample:
    """Perform one NTPv4 exchange; use monotonic elapsed time for RTT."""
    packet = bytearray(48)
    packet[0] = 0x23  # LI=0, VN=4, Mode=3 (client)

    with socket.socket(endpoint.family, endpoint.socktype, endpoint.proto) as sock:
        sock.settimeout(timeout)
        sock.connect(endpoint.sockaddr)

        mono_t1 = time.monotonic_ns()
        wall_t1 = time.time_ns()
        transmit_stamp = ntp_timestamp_from_unix_ns(wall_t1)
        packet[40:48] = transmit_stamp
        sock.send(packet)
        response = sock.recv(512)
        wall_t4 = time.time_ns()
        mono_t4 = time.monotonic_ns()

    if len(response) < 48:
        raise ValueError("Short NTP response")

    li = response[0] >> 6
    version = (response[0] >> 3) & 0x07
    mode = response[0] & 0x07
    stratum = response[1]

    if li == 3:
        raise ValueError("Server reports itself unsynchronized (LI=3)")
    if version not in (3, 4) or mode != 4:
        raise ValueError("Invalid NTP version or server mode")
    if not 1 <= stratum <= 15:
        raise ValueError(f"Invalid NTP stratum: {stratum}")
    if response[24:32] != transmit_stamp:
        raise ValueError("Originate timestamp does not match request")
    if response[32:40] == b"\x00" * 8 or response[40:48] == b"\x00" * 8:
        raise ValueError("Server receive/transmit timestamp is zero")

    wall_elapsed = wall_t4 - wall_t1
    mono_elapsed = mono_t4 - mono_t1
    if abs(wall_elapsed - mono_elapsed) > CLOCK_STEP_REJECTION_NS:
        raise ValueError("System wall clock changed during NTP measurement")

    t2 = ntp_timestamp_to_unix_ns(response[32:40])
    t3 = ntp_timestamp_to_unix_ns(response[40:48])
    if t3 < t2:
        raise ValueError("Server transmit timestamp precedes receive timestamp")

    # Standard NTP four-timestamp estimates:
    # offset = ((T2-T1) + (T3-T4))/2
    # delay  = (T4-T1) - (T3-T2)
    offset_ns = ((t2 - wall_t1) + (t3 - wall_t4)) // 2
    server_processing_ns = t3 - t2
    delay_ns = mono_elapsed - server_processing_ns
    if delay_ns < -1_000_000:
        raise ValueError("Calculated network delay is materially negative")
    delay_ns = max(0, delay_ns)

    # NTP root delay is signed 16.16 and root dispersion is unsigned 16.16.
    # Root distance uses max(0, root_delay)/2 + root_dispersion, plus local
    # peer delay/2 when forming the sample's total uncertainty proxy.
    root_delay_raw = struct.unpack("!i", response[4:8])[0]
    root_dispersion_raw = struct.unpack("!I", response[8:12])[0]
    positive_root_delay_ns = max(0, root_delay_raw) * 1_000_000_000 // 65536
    root_dispersion_ns = root_dispersion_raw * 1_000_000_000 // 65536
    precision_exp = struct.unpack("!b", response[3:4])[0]
    precision_ns = max(1, int(1_000_000_000 * (2.0 ** precision_exp)))
    precision_ns = min(precision_ns, 1_000_000_000)
    root_distance_ns = positive_root_delay_ns // 2 + root_dispersion_ns + precision_ns

    return Sample(
        server=endpoint.host,
        offset_ns=offset_ns,
        delay_ns=delay_ns,
        root_distance_ns=root_distance_ns,
        wall_mid_ns=(wall_t1 + wall_t4) // 2,
        mono_mid_ns=(mono_t1 + mono_t4) // 2,
    )


# ---------------------------------------------------------------------------
# Robust statistics and directional-delay calibration
# ---------------------------------------------------------------------------

def median_absolute_deviation(values: Iterable[int]) -> int:
    values = list(values)
    if not values:
        return 0
    centre = statistics.median(values)
    return int(statistics.median(abs(value - centre) for value in values))


def robust_jitter(values: Iterable[int]) -> int:
    """MAD scaled to Gaussian sigma; a dispersion score, not a hard bound."""
    return int(1.4826 * median_absolute_deviation(values))


def directional_deviation_samples(
    samples: Iterable[Sample],
) -> tuple[list[int], list[int]]:
    """Return per-sample forward/reverse delay deviations from their medians.

    For stable server clock offset theta during calibration:
        delta = d_forward + d_reverse
        offset = theta + (d_forward - d_reverse)/2

    Therefore, centred changes obey:
        dev_forward = dev_delta/2 + dev_offset
        dev_reverse = dev_delta/2 - dev_offset

    These are deviations, not absolute one-way delays. Negative values are
    valid because they mean 'below the calibration median'.
    """
    samples = list(samples)
    if not samples:
        return [], []

    median_delay = int(statistics.median(s.delay_ns for s in samples))
    median_offset = int(statistics.median(s.offset_ns for s in samples))

    forward: list[int] = []
    reverse: list[int] = []
    for sample in samples:
        delta_delay = sample.delay_ns - median_delay
        delta_offset = sample.offset_ns - median_offset
        half_delta_delay = round(delta_delay / 2)
        forward.append(half_delta_delay + delta_offset)
        reverse.append(half_delta_delay - delta_offset)

    return forward, reverse


def analyse_server(endpoint: Endpoint, samples: list[Sample]) -> Optional[ServerCalibration]:
    if len(samples) < 3:
        return None

    median_offset = int(statistics.median(s.offset_ns for s in samples))
    median_delay = int(statistics.median(s.delay_ns for s in samples))
    low_delay_samples = sorted(samples, key=lambda sample: sample.delay_ns)[:min(3, len(samples))]
    # This is the offset baseline corresponding to the same low-delay sample
    # policy used later when installing the clock model.
    low_delay_reference_utc_minus_mono = int(statistics.median(
        sample.utc_minus_mono_ns for sample in low_delay_samples
    ))
    best = min(samples, key=lambda sample: sample.delay_ns)

    forward_devs, reverse_devs = directional_deviation_samples(samples)
    forward_jitter = robust_jitter(forward_devs)
    reverse_jitter = robust_jitter(reverse_devs)
    offset_jitter = robust_jitter(s.offset_ns for s in samples)

    # The score is a ranking heuristic, not a certified UTC error bound.
    # It rewards low minimum RTT, a healthy server root distance and stable
    # inferred directional-delay variations.
    score = (
        best.delay_ns // 2
        + best.root_distance_ns
        + max(forward_jitter, reverse_jitter)
        + offset_jitter
    )

    return ServerCalibration(
        endpoint=endpoint,
        samples=samples,
        median_offset_ns=median_offset,
        low_delay_reference_utc_minus_mono_ns=low_delay_reference_utc_minus_mono,
        median_delay_ns=median_delay,
        min_delay_ns=best.delay_ns,
        root_distance_ns=best.root_distance_ns,
        offset_jitter_ns=offset_jitter,
        forward_jitter_ns=forward_jitter,
        reverse_jitter_ns=reverse_jitter,
        score_ns=score,
    )


def collect_calibration_round_robin(
    endpoints: list[Endpoint],
    count: int,
    spacing: float,
    timeout: float,
    stop_before_mono_ns: Optional[int] = None,
) -> dict[str, list[Sample]]:
    """Interleave probes, optionally stopping before a monotonic cutoff.

    Every UDP request is given a timeout that fits before the cutoff with a
    small margin. No new request is started once the cutoff is reached.
    """
    result = {endpoint.host: [] for endpoint in endpoints}
    for sample_index in range(count):
        for endpoint in endpoints:
            request_timeout = timeout
            if stop_before_mono_ns is not None:
                remaining_ns = stop_before_mono_ns - time.monotonic_ns()
                if remaining_ns <= int(NTP_REQUEST_CUTOFF_MARGIN_SECONDS * 1e9):
                    print("[Calibration] Stopping: the one-minute safety window has begun.")
                    return result
                request_timeout = min(
                    timeout,
                    remaining_ns / 1e9 - NTP_REQUEST_CUTOFF_MARGIN_SECONDS,
                )
            try:
                sample = query_ntp(endpoint, request_timeout)
                result[endpoint.host].append(sample)
                print(
                    f"  [{endpoint.host}] {sample_index + 1}/{count}: "
                    f"offset={sample.offset_ns / 1e6:+.3f} ms, "
                    f"RTT={sample.delay_ns / 1e6:.3f} ms"
                )
            except Exception as exc:
                print(
                    f"  [{endpoint.host}] {sample_index + 1}/{count} failed: {exc}"
                )
            if spacing > 0:
                if stop_before_mono_ns is None:
                    time.sleep(spacing)
                else:
                    remaining_ns = stop_before_mono_ns - time.monotonic_ns()
                    sleep_ns = remaining_ns - int(
                        NTP_REQUEST_CUTOFF_MARGIN_SECONDS * 1e9
                    )
                    if sleep_ns <= 0:
                        print("[Calibration] Stopping before the one-minute safety window.")
                        return result
                    time.sleep(min(spacing, sleep_ns / 1e9))
    return result


def save_calibration_report(
    path: Path,
    calibrations: list[ServerCalibration],
    ensemble_reference_ns: int,
    selected_host: str,
    relative_correction_enabled: bool,
) -> None:
    report = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "ensemble_utc_minus_monotonic_reference_ns": ensemble_reference_ns,
        "selected_server": selected_host,
        "relative_bias_correction_enabled": relative_correction_enabled,
        "method": {
            "directional_deviation": "df_dev = (dRTT/2) + dOffset; dr_dev = (dRTT/2) - dOffset",
            "relative_bias": "server_low_delay_reference_utc_minus_monotonic - median(server_baselines)",
            "warning": (
                "Directional estimates are changes relative to each server's median, "
                "not absolute one-way delays. Relative-bias correction assumes the "
                "cross-server median is a useful reference; it cannot identify the "
                "common local asymmetry or prove absolute UTC accuracy."
            ),
        },
        "servers": [],
    }

    for item in calibrations:
        forward_devs, reverse_devs = directional_deviation_samples(item.samples)
        report["servers"].append({
            "host": item.endpoint.host,
            "resolved_address": repr(item.endpoint.sockaddr),
            "median_offset_ns": item.median_offset_ns,
            "low_delay_reference_utc_minus_monotonic_ns": item.low_delay_reference_utc_minus_mono_ns,
            "median_delay_ns": item.median_delay_ns,
            "minimum_delay_ns": item.min_delay_ns,
            "root_distance_ns": item.root_distance_ns,
            "offset_jitter_ns": item.offset_jitter_ns,
            "forward_variation_jitter_ns": item.forward_jitter_ns,
            "reverse_variation_jitter_ns": item.reverse_jitter_ns,
            "relative_bias_ns": item.relative_bias_ns,
            "relative_bias_uncertainty_ns": item.relative_bias_uncertainty_ns,
            "selection_score_ns": item.score_ns,
            "raw_samples": [
                {
                    **asdict(sample),
                    "forward_delay_deviation_ns": forward_devs[index],
                    "reverse_delay_deviation_ns": reverse_devs[index],
                }
                for index, sample in enumerate(item.samples)
            ],
        })

    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[Calibration] Raw measurements and analysis saved to {path}")


def calibrate(
    server_names: tuple[str, ...],
    samples_per_server: int,
    spacing: float,
    timeout: float,
    report_path: Optional[Path],
    stop_before_mono_ns: Optional[int] = None,
) -> ServerCalibration:
    print("[Calibration] Resolving candidate NTP servers...")
    endpoints: list[Endpoint] = []
    seen_addresses: set[tuple] = set()

    for host in server_names:
        try:
            if stop_before_mono_ns is None:
                endpoint = resolve_endpoint(host)
            else:
                endpoint = resolve_endpoint_before(
                    host,
                    stop_before_mono_ns
                    - int(NTP_REQUEST_CUTOFF_MARGIN_SECONDS * 1e9),
                )
                if endpoint is None:
                    print(
                        "[Calibration] Stopping server resolution before "
                        "the one-minute safety window."
                    )
                    break
        except Exception as exc:
            print(f"  [Calibration] {host}: resolution failed: {exc}")
            continue
        if endpoint.identity in seen_addresses:
            print(
                f"  [Calibration] {host}: same resolved endpoint as an earlier entry; skipped"
            )
            continue
        seen_addresses.add(endpoint.identity)
        endpoints.append(endpoint)

    if not endpoints:
        raise RuntimeError("No calibration servers could be resolved")

    print(
        f"[Calibration] Collecting {samples_per_server} round-robin samples "
        f"from {len(endpoints)} distinct resolved endpoints."
    )
    sample_map = collect_calibration_round_robin(
        endpoints, samples_per_server, spacing, timeout,
        stop_before_mono_ns=stop_before_mono_ns,
    )

    calibrations: list[ServerCalibration] = []
    for endpoint in endpoints:
        stats = analyse_server(endpoint, sample_map[endpoint.host])
        if stats is None:
            print(f"[Calibration] {endpoint.host}: insufficient successful samples")
            continue
        calibrations.append(stats)

    if not calibrations:
        raise RuntimeError("Calibration failed: no server produced at least 3 valid samples")

    # One vote per distinct endpoint, so servers with more successful
    # samples do not dominate the ensemble reference. Three distinct
    # endpoints are the minimum for a median that can tolerate one outlier;
    # actual infrastructure independence cannot be established here.
    ensemble_reference = int(statistics.median(
        item.low_delay_reference_utc_minus_mono_ns for item in calibrations
    ))

    if len(calibrations) >= 3:
        # Relative correction only: remove each candidate's low-delay baseline
        # displacement from the cross-server median. This does not identify
        # the common local asymmetry or any absolute server error.
        ensemble_uncertainty = int(statistics.median(
            item.score_ns for item in calibrations
        ))
        for item in calibrations:
            item.relative_bias_ns = item.low_delay_reference_utc_minus_mono_ns - ensemble_reference
            item.relative_bias_uncertainty_ns = item.score_ns + ensemble_uncertainty
        correction_enabled = True
    else:
        # With fewer than three unique endpoints, a cross-server median is too
        # fragile to justify an automatic bias correction.
        for item in calibrations:
            item.relative_bias_ns = 0
            item.relative_bias_uncertainty_ns = 0
        correction_enabled = False
        print(
            "[Calibration] WARNING: fewer than three distinct valid endpoints; "
            "relative-bias correction disabled."
        )

    print("\n[Calibration] Server analysis:")
    for item in sorted(calibrations, key=lambda candidate: candidate.score_ns):
        print(
            f"  {item.endpoint.host:24s} "
            f"score={item.score_ns / 1e6:8.3f} ms  "
            f"minRTT={item.min_delay_ns / 1e6:7.3f} ms  "
            f"F-var={item.forward_jitter_ns / 1e6:7.3f} ms  "
            f"R-var={item.reverse_jitter_ns / 1e6:7.3f} ms  "
            f"relative-bias={item.relative_bias_ns / 1e6:+8.3f} ms"
        )

    selected = min(calibrations, key=lambda candidate: candidate.score_ns)
    print(f"\n[Calibration] Ensemble UTC-minus-monotonic reference: {ensemble_reference / 1e6:+.3f} ms")
    print(f"[Calibration] Selected server: {selected.endpoint.host}")
    if correction_enabled:
        print(
            f"[Calibration] Applying relative bias correction: "
            f"{selected.relative_bias_ns / 1e6:+.3f} ms"
        )
        print(
            "[Calibration] Relative correction aligns the selected server to "
            "the cross-server median; common local asymmetry remains."
        )
    else:
        print("[Calibration] Using the selected server's raw NTP offsets.")

    if report_path is not None:
        try:
            save_calibration_report(
                report_path, calibrations, ensemble_reference, selected.endpoint.host,
                correction_enabled,
            )
        except OSError as exc:
            print(f"[Calibration] Could not write report {report_path}: {exc}")

    return selected


# ---------------------------------------------------------------------------
# Monotonic UTC model
# ---------------------------------------------------------------------------

class SyncedClock:
    """UTC estimate mapped to monotonic_ns; failed refreshes retain last model."""

    def __init__(self, max_drift_ppm: float):
        self.max_drift_ppm = max_drift_ppm
        self.anchor_mono_ns: Optional[int] = None
        self.anchor_utc_ns: Optional[int] = None
        self.base_uncertainty_ns: Optional[int] = None
        self.last_success_mono_ns: Optional[int] = None
        self.last_attempt_mono_ns: Optional[int] = None
        self.source = "uninitialized"

    def install_system_clock(self) -> None:
        """Install local system time as the initial, immediately available source."""
        mono_before = time.monotonic_ns()
        wall_ns = time.time_ns()
        mono_after = time.monotonic_ns()

        self.anchor_mono_ns = (mono_before + mono_after) // 2
        self.anchor_utc_ns = wall_ns
        # System-clock accuracy is not independently known here, so don't
        # present a fabricated numeric uncertainty.
        self.base_uncertainty_ns = None
        self.last_success_mono_ns = None  # No successful NTP sample yet.
        self.last_attempt_mono_ns = None
        self.source = "system clock"
        print("[Clock] Initial time source: local system clock.")

    def install_samples(
        self,
        samples: list[Sample],
        offset_correction_ns: int = 0,
        correction_uncertainty_ns: int = 0,
    ) -> None:
        if not samples:
            raise RuntimeError("At least one successful NTP sample is required")

        corrected_constants = [
            sample.utc_minus_mono_ns - offset_correction_ns
            for sample in samples
        ]
        # Use the three lowest-delay samples, then take their median.
        selected_indices = sorted(
            range(len(samples)), key=lambda index: samples[index].delay_ns
        )[:min(3, len(samples))]
        selected = [samples[index] for index in selected_indices]
        selected_constants = [corrected_constants[index] for index in selected_indices]
        utc_minus_mono = int(statistics.median(selected_constants))

        jitter_ns = robust_jitter(selected_constants)
        sample_uncertainty_ns = (
            int(statistics.median(s.delay_ns for s in selected)) // 2
            + int(statistics.median(s.root_distance_ns for s in selected))
            + jitter_ns
        )
        if len(selected) == 1:
            # A single late-window sample is usable, but less statistically
            # robust than a multi-sample estimate.
            sample_uncertainty_ns += 5_000_000

        self.anchor_mono_ns = time.monotonic_ns()
        self.anchor_utc_ns = self.anchor_mono_ns + utc_minus_mono
        self.base_uncertainty_ns = sample_uncertainty_ns + correction_uncertainty_ns
        self.last_success_mono_ns = self.anchor_mono_ns
        self.last_attempt_mono_ns = self.anchor_mono_ns
        self.source = "NTP"

        print(
            "[Clock] Model updated: estimated uncertainty proxy "
            f"{self.base_uncertainty_ns / 1e6:.3f} ms "
            "(not a guaranteed maximum-error bound)."
        )
        if len(samples) == 1:
            print("[Clock] WARNING: clock model uses only one NTP sample.")
        self.report_system_offset("NTP synchronization")

    def now_utc_ns(self) -> int:
        if self.anchor_mono_ns is None or self.anchor_utc_ns is None:
            raise RuntimeError("Clock has not been synchronized")
        return self.anchor_utc_ns + (time.monotonic_ns() - self.anchor_mono_ns)

    def uncertainty_ns(self) -> Optional[int]:
        if self.anchor_mono_ns is None:
            raise RuntimeError("Clock has not been synchronized")
        if self.base_uncertainty_ns is None:
            return None
        elapsed_ns = max(0, time.monotonic_ns() - self.anchor_mono_ns)
        drift_ns = int(elapsed_ns * self.max_drift_ppm / 1_000_000)
        return self.base_uncertainty_ns + drift_ns

    def report_system_offset(self, context: str) -> int:
        """Print this clock model's offset relative to the OS wall clock."""
        model_utc_ns = self.now_utc_ns()
        system_utc_ns = time.time_ns()
        offset_ns = model_utc_ns - system_utc_ns
        offset_ms = offset_ns / 1_000_000
        print(
            f"[Clock] Offset to system time after {context}: "
            f"{offset_ms:+.3f} ms (NTP/model minus system clock)."
        )
        if abs(offset_ms) >= SYSTEM_OFFSET_WARNING_MS:
            print(
                "[Clock] WARNING: NTP/model time differs from system time by "
                f"at least {SYSTEM_OFFSET_WARNING_MS:.0f} ms. Continuing anyway."
            )
        return offset_ns

    def needs_sync(self, interval_seconds: float, retry_seconds: float) -> bool:
        now = time.monotonic_ns()
        if self.last_attempt_mono_ns is None:
            return True

        since_attempt = now - self.last_attempt_mono_ns
        if self.last_success_mono_ns is None:
            # While still on the system-clock fallback, retry at the retry
            # interval instead of spinning on every scheduler loop.
            return since_attempt >= int(retry_seconds * 1e9)

        since_success = now - self.last_success_mono_ns
        return (
            since_success >= int(interval_seconds * 1e9)
            and since_attempt >= int(retry_seconds * 1e9)
        )

    def mark_failed_attempt(self) -> None:
        self.last_attempt_mono_ns = time.monotonic_ns()


def next_local_target_utc_ns(target_text: str) -> int:
    """Resolve the next local wall-clock HH:MM:SS occurrence to Unix ns."""
    try:
        parsed = datetime.strptime(target_text, "%H:%M:%S").time()
    except ValueError as exc:
        raise ValueError("Target time must use HH:MM:SS, e.g. 14:30:00") from exc

    now_ns = time.time_ns()
    local_now = datetime.now()
    candidate = datetime.combine(local_now.date(), parsed)
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


def collect_single_server_samples(
    endpoint: Endpoint,
    clock: SyncedClock,
    target_utc_ns: int,
    count: int,
    spacing: float,
    timeout: float,
) -> list[Sample]:
    """Collect samples without starting any request inside the final 10 s.

    Each UDP timeout is shortened to fit before the 10-second cutoff. The
    50 ms margin reduces the chance that a request remains outstanding at the
    cutoff because of normal timeout/scheduler overhead.
    """
    samples: list[Sample] = []
    cutoff_ns = int(MIN_NTP_SYNC_REMAINING_SECONDS * 1e9)
    margin_ns = int(NTP_REQUEST_CUTOFF_MARGIN_SECONDS * 1e9)

    for index in range(count):
        remaining_ns = target_utc_ns - clock.now_utc_ns()
        available_ns = remaining_ns - cutoff_ns - margin_ns
        if available_ns <= 0:
            print(
                "[NTP] Stopping synchronization: no new requests are allowed "
                "within 10 seconds of the target."
            )
            break

        request_timeout = min(timeout, available_ns / 1e9)
        if request_timeout <= 0:
            break

        try:
            sample = query_ntp(endpoint, request_timeout)
            samples.append(sample)
            print(
                f"  [NTP] {index + 1}/{count}: "
                f"offset={sample.offset_ns / 1e6:+.3f} ms, "
                f"RTT={sample.delay_ns / 1e6:.3f} ms"
            )
        except Exception as exc:
            print(f"  [NTP] {index + 1}/{count} failed: {exc}")

        if index + 1 < count and spacing > 0:
            remaining_ns = target_utc_ns - clock.now_utc_ns()
            sleep_ns = remaining_ns - cutoff_ns - margin_ns
            if sleep_ns <= 0:
                break
            time.sleep(min(spacing, sleep_ns / 1e9))

    return samples


def wait_until(
    clock: SyncedClock,
    target_utc_ns: int,
    endpoint: Optional[Endpoint],
    server_name: str,
    offset_correction_ns: int,
    correction_uncertainty_ns: int,
    sync_interval: float,
    retry_interval: float,
    timeout: float,
) -> None:
    print(
        f"[Scheduler] Waiting using {clock.source} time mapped to monotonic time."
    )

    countdown_milestones = (60, 30, 10, 5)
    pending_milestones = set(countdown_milestones)

    while True:
        remaining_ns = target_utc_ns - clock.now_utc_ns()
        if remaining_ns <= 0:
            return
        remaining_seconds = remaining_ns / 1e9

        for milestone in countdown_milestones:
            if milestone in pending_milestones and remaining_seconds <= milestone:
                if milestone == 5:
                    print(
                        "[Scheduler] T-5: 5 seconds remaining; final live-check "
                        "phase approaching. The last 100 ms will be silent to "
                        "avoid console-output latency.",
                        flush=True,
                    )
                else:
                    print(f"[Scheduler] T-{milestone}: {milestone} seconds remaining.", flush=True)
                pending_milestones.remove(milestone)

        # Every initial sync and refresh obeys the same hard 10-second rule.
        if (
            remaining_seconds > MIN_NTP_SYNC_REMAINING_SECONDS
            and clock.needs_sync(sync_interval, retry_interval)
        ):
            clock.last_attempt_mono_ns = time.monotonic_ns()
            if endpoint is None:
                try:
                    remaining_ns = target_utc_ns - clock.now_utc_ns()
                    resolution_deadline = (
                        time.monotonic_ns()
                        + remaining_ns
                        - int(MIN_NTP_SYNC_REMAINING_SECONDS * 1e9)
                        - int(NTP_REQUEST_CUTOFF_MARGIN_SECONDS * 1e9)
                    )
                    endpoint = resolve_endpoint_before(
                        server_name, resolution_deadline
                    )
                    if endpoint is None:
                        print(
                            "[NTP] DNS resolution did not finish before the "
                            "10-second cutoff; retaining the current clock model."
                        )
                        clock.mark_failed_attempt()
                        continue
                except Exception as exc:
                    print(f"[NTP] Could not resolve {server_name}: {exc}")
                    clock.mark_failed_attempt()
                    continue

            print(f"[NTP] Refreshing selected server: {endpoint.host}")
            samples = collect_single_server_samples(
                endpoint=endpoint,
                clock=clock,
                target_utc_ns=target_utc_ns,
                count=SYNC_SAMPLES,
                spacing=SYNC_SPACING_SECONDS,
                timeout=timeout,
            )

            if samples:
                clock.install_samples(
                    samples,
                    offset_correction_ns=offset_correction_ns,
                    correction_uncertainty_ns=correction_uncertainty_ns,
                )
            else:
                print("[NTP] Refresh failed; retaining last successful clock model.")
                clock.mark_failed_attempt()
            continue

        remaining_ns = target_utc_ns - clock.now_utc_ns()
        if remaining_ns <= 0:
            return

        if remaining_ns > int(SPIN_WINDOW_SECONDS * 1e9):
            sleep_ns = remaining_ns - int(SPIN_WINDOW_SECONDS * 1e9)

            # Also wake at the next countdown milestone rather than sleeping
            # through it. Milestones already printed are removed above.
            next_milestone = max(
                (milestone for milestone in pending_milestones if remaining_seconds > milestone),
                default=None,
            )
            if next_milestone is not None:
                until_milestone_ns = remaining_ns - int(next_milestone * 1e9)
                if until_milestone_ns > 0:
                    sleep_ns = min(sleep_ns, until_milestone_ns)

            # Wake for the next retry/refresh, including while still on the
            # system-clock fallback with no successful NTP sample yet.
            if clock.last_attempt_mono_ns is not None:
                retry_due_ns = (
                    clock.last_attempt_mono_ns + int(retry_interval * 1e9)
                )
                if clock.last_success_mono_ns is None:
                    next_sync_ns = retry_due_ns
                else:
                    regular_due_ns = (
                        clock.last_success_mono_ns + int(sync_interval * 1e9)
                    )
                    next_sync_ns = max(regular_due_ns, retry_due_ns)
                until_sync_ns = next_sync_ns - time.monotonic_ns()
                if until_sync_ns > 0:
                    sleep_ns = min(sleep_ns, until_sync_ns)

            time.sleep(max(0.0005, sleep_ns / 1e9))
        else:
            # Final 100 ms: busy-poll only the monotonic clock mapping.
            while clock.now_utc_ns() < target_utc_ns:
                pass
            return
