# NTP-Synchronized One-Shot Keystroke Scheduler — Technical Specification

**Status:** Design and behavioural specification  
**Scope:** `scheduler.py`, `keystroke.py`, `test_listener.py`, and the tests in `tests/`  
**Target runtime:** Python 3.9+  
**Operating systems:** Windows, macOS, and Linux, subject to desktop input and global-listener permissions

## 1. Purpose

The program schedules one desktop keystroke for a specified local time of day. It begins with the operating system's clock so that it always has an immediately available time model, optionally improves that model through NTP, and then waits against a monotonic clock so subsequent wall-clock adjustments do not directly jump the countdown.

The design has four separate concerns:

1. **Clock acquisition and calibration** — query NTP servers, estimate directional path-delay variation, assess server quality, and optionally correct the selected server relative to an ensemble.
2. **Deadline scheduling** — map UTC to a monotonic clock, enforce strict cutoffs for network activity, and wait for the target.
3. **Input injection and CLI** — perform startup keyboard checks, parse options, and inject the requested key exactly once after the scheduler returns.
4. **Independent sanity testing** — listen for the target key using the system clock only and report the time at which the listener callback ran.

The system aims for low timing error, with 10 ms as an engineering target. It does not provide a hard real-time guarantee.

## 2. Terminology

| Term | Definition |
|---|---|
| Target time, `T` | The next occurrence of the configured local `HH:MM:SS` time. |
| System clock | The OS wall clock read using `time.time_ns()`. |
| Monotonic clock | The non-decreasing elapsed-time clock read using `time.monotonic_ns()`. |
| NTP offset, `m` | Estimated server time minus client wall-clock time. Positive means the server appears ahead. |
| RTT / delay, `δ` | NTP round-trip network-delay estimate after subtracting server processing time. |
| Forward delay, `d_f` | Client-to-server path delay. |
| Reverse delay, `d_r` | Server-to-client path delay. |
| Relative bias | A selected server's measured low-delay UTC/monotonic baseline displacement from the median baseline of the calibrated server set. |
| Uncertainty proxy | A heuristic error estimate assembled from network delay, server root-distance information, jitter, relative-bias dispersion, and assumed oscillator drift. It is not a certified maximum-error bound. |
| Cutoff `T−60 s` | Boundary at which full multi-server calibration must stop. |
| Cutoff `T−10 s` | Boundary inside which no NTP resolution/request/refresh may be started or continued intentionally. |

## 3. Functional requirements

### 3.1 One-shot execution

- The program MUST calculate the next local occurrence of the requested `HH:MM:SS` time.
- If that time has already passed today, the next day's occurrence MUST be selected.
- The program MUST inject the configured key once after the scheduler reaches the deadline.
- The program MUST exit after the injection attempt.
- The default target is `14:30:00`; the default key is `enter`.
- A requested local time that does not exist due to a daylight-saving transition MUST be rejected rather than silently normalized.

### 3.2 Separation of responsibilities

- `scheduler.py` MUST contain clock acquisition, NTP transport, calibration, clock-model, and waiting logic only. It MUST NOT parse CLI arguments or inject keyboard events.
- `keystroke.py` MUST own argument parsing, startup keyboard preflight, desktop input injection, and the application entry point.
- `test_listener.py` MUST be an independent system-clock-only listener for end-to-end timing checks.
- Unit tests MUST remain in `tests/`, with separate coverage for directional mathematics and startup keyboard preflight.

## 4. NTP measurement model

### 4.1 Four timestamps

For one NTP exchange:

- `T1`: client transmit timestamp (system wall clock).
- `T2`: server receive timestamp.
- `T3`: server transmit timestamp.
- `T4`: client receive timestamp (system wall clock).

The NTP offset estimate is:

\[
 m = \frac{(T_2-T_1)+(T_3-T_4)}{2}
\]

The network delay estimate is:

\[
 \delta = (T_4-T_1)-(T_3-T_2)
\]

The implementation SHOULD use `time.monotonic_ns()` for the client elapsed interval `T4−T1`, and subtract server processing time `T3−T2`. Wall-clock timestamps remain necessary for the offset calculation. If wall-clock elapsed time and monotonic elapsed time disagree by more than the configured clock-step rejection threshold, the sample MUST be rejected.

The NTP response MUST be checked for a valid server mode and supported version, synchronized leap state, valid stratum, a matching originate timestamp, non-zero receive/transmit timestamps, and ordered server timestamps. Invalid or implausible samples MUST be rejected.

NTP's 32-bit seconds field MUST be interpreted in the era nearest the local system time.

### 4.2 Local-to-monotonic UTC mapping

For each accepted sample, construct a mapping constant:

\[
 U = W_{mid} + m - M_{mid}
\]

where `W_mid` is the local wall-clock midpoint and `M_mid` is the monotonic midpoint. This gives an estimated UTC value for a monotonic timestamp:

\[
 UTC(M) = M + U
\]

The model installed by the scheduler is an anchor pair `(M₀, UTC₀)` and is evaluated as:

\[
 UTC_{est}(M)=UTC_0+(M-M_0)
\]

This mapping means an OS wall-clock step after model installation does not directly change the active countdown. It does not correct oscillator frequency; drift is accounted for only in the uncertainty proxy.

## 5. Directional-delay model and calibration mathematics

### 5.1 Path decomposition

For one client/server path, define:

- `L_o`: local outbound component.
- `L_i`: local inbound component.
- `R_{i,j}`: remote-side component on the inbound-to-server leg for server `j`.
- `R_{o,j}`: remote-side component on the server's outbound leg for server `j`.

Then:

\[
 d_{f,j}=L_o+R_{i,j}
\]

\[
 d_{r,j}=R_{o,j}+L_i
\]

The NTP delay and offset model are:

\[
 \delta_j=d_{f,j}+d_{r,j}
\]

\[
 m_j=\theta_j+\frac{d_{f,j}-d_{r,j}}{2}
\]

where `θ_j` is the server's true clock offset relative to the client clock.

### 5.2 Observable directional changes

For two observations of the same server, if `θ_j` is effectively constant across the observations:

\[
 \Delta\delta=\Delta d_f+\Delta d_r
\]

\[
 2\Delta m=\Delta d_f-\Delta d_r
\]

Therefore:

\[
 \boxed{\Delta d_f=\frac{\Delta\delta}{2}+\Delta m}
\]

\[
 \boxed{\Delta d_r=\frac{\Delta\delta}{2}-\Delta m}
\]

The implementation applies the same algebra to each sample after centring its RTT and offset against that server's calibration medians:

\[
 d_{f,dev}=\frac{\delta-\operatorname{median}(\delta)}{2}
             +(m-\operatorname{median}(m))
\]

\[
 d_{r,dev}=\frac{\delta-\operatorname{median}(\delta)}{2}
             -(m-\operatorname{median}(m))
\]

These values are **directional delay deviations from the calibration median**, not absolute one-way delays. Negative deviations are valid and mean that the inferred component is below its calibration median.

### 5.3 Assumptions and identifiability limits

The directional-deviation calculation relies on these assumptions:

1. The server's true clock offset `θ_j` does not change materially during that server's calibration window.
2. The local path and host-side timestamping behaviour remain sufficiently stable over the measurements.
3. Variations in NTP offset are predominantly associated with path-delay changes rather than server clock corrections or unrelated timestamping effects.

The equations identify changes in forward and reverse delay, not their absolute values. Repeated RTT/offset samples cannot by themselves identify a constant absolute one-way asymmetry or separate a common local asymmetry from a client clock error.

The model may be written with a common local asymmetry:

\[
 A_L=L_o-L_i
\]

and server-specific remote asymmetry:

\[
 A_{R,j}=R_{i,j}-R_{o,j}
\]

so that:

\[
 m_j=\theta_j+\frac{A_L+A_{R,j}}{2}
\]

If the local components are shared and stable across destinations, their common contribution is not removed by per-server centring. The cross-server median method below assumes the servers' remaining clock/path biases are reasonably centred; this is a heuristic, not a proof.

### 5.4 Multi-server calibration procedure

When requested and eligible, calibration MUST:

1. Resolve the configured candidate hostnames to UDP endpoints.
2. Deduplicate endpoints that resolve to the same address/family/port identity, so aliases do not receive multiple votes.
3. Query endpoints in round-robin order to reduce time-trend bias from probing one server in a long contiguous block.
4. Collect the configured number of samples per endpoint, subject to the `T−60 s` stop boundary.
5. Retain raw accepted samples, including wall/monotonic midpoints, NTP offset, delay, and server root-distance information.
6. Require at least three valid samples to analyse an endpoint.
7. Calculate per-server median offset, median/minimum RTT, offset jitter, and forward/reverse directional-deviation jitter.
8. Build a low-delay UTC/monotonic reference from the median mapping constant of that endpoint's three lowest-RTT samples (or all available samples if fewer than three, provided the endpoint otherwise passes the minimum-sample rule).
9. Calculate an ensemble reference as the median of the per-endpoint low-delay references.
10. Calculate each endpoint's relative bias as its low-delay reference minus the ensemble reference.
11. Enable automatic relative-bias correction only when at least three distinct endpoints have valid analyses. Otherwise correction MUST be disabled.
12. Select the endpoint with the lowest selection score and retain its samples, correction, and correction-uncertainty estimate for later refreshes.
13. Save a JSON report when a report path is configured.

The candidate set in the current implementation is:

- `time.cloudflare.com`
- `time.google.com`
- `time.windows.com`
- `time.apple.com`
- `pool.ntp.org`

Defaults are 12 requested samples per endpoint and 150 ms spacing between probes.

### 5.5 Relative-bias correction

For endpoint `j`, let:

\[
 B_j=\operatorname{median}(U_{j,k})
\]

where `U` is calculated from that endpoint's three lowest-RTT samples. The cross-server ensemble reference is:

\[
 B_{ens}=\operatorname{median}_j(B_j)
\]

The endpoint's relative bias is:

\[
 b_j=B_j-B_{ens}
\]

When correction is enabled, the selected server's mapping samples are corrected by subtracting `b_j`:

\[
 U_{corrected}=U_{raw}-b_j
\]

This aligns the selected server's low-delay baseline to the cross-server median. It does **not** estimate the common local asymmetry, does not prove the median is true UTC, and may absorb real differences in server clock error or remote path asymmetry. It is an optional empirical ensemble correction under the assumption that at least three distinct endpoints provide a reasonably centred reference.

### 5.6 Server ranking

The current ranking score is a heuristic:

\[
 score_j = \frac{\delta_{min,j}}{2}
           + rootDistance_j
           + \max(jitter_{f,j},jitter_{r,j})
           + jitter_{offset,j}
\]

Jitter is estimated using median absolute deviation scaled by 1.4826. The score favours low minimum RTT, low server root-distance information, and stable offset/directional-deviation measurements. It is not a formal or guaranteed time-error bound.

The relative-correction uncertainty proxy combines the endpoint's score with the median score across analysed endpoints. This is a ranking/uncertainty heuristic, not a rigorous statistical confidence interval.

### 5.7 Calibration report

The optional JSON report SHOULD preserve:

- Report creation time in UTC.
- Ensemble UTC-minus-monotonic reference.
- Selected endpoint and whether relative correction was enabled.
- The method and its limitations.
- Per-endpoint resolved address, median offset, median/minimum RTT, root-distance estimate, offset jitter, directional-deviation jitter, relative bias, relative-bias uncertainty, and selection score.
- Every raw sample and its derived forward/reverse deviations.

Default report path: `ntp_calibration.json`. An empty report-path option disables report writing.

## 6. Clock acquisition and time-source policy

### 6.1 Initial source

The local OS system clock MUST be installed first, immediately after the target has been selected. This ensures a usable schedule exists even if DNS or NTP is unavailable.

The system-clock model has no independently established numeric uncertainty. The program MUST NOT fabricate one; it should report that the system-clock uncertainty is unknown.

### 6.2 Full calibration eligibility

Full multi-server calibration is optional and is requested with `--calibrate`.

- It MAY start only when the system-clock model indicates strictly more than 60 seconds remain before the target.
- It MUST stop resolving/collecting measurements at the `T−60 s` boundary.
- Request timeouts and spacing MUST be bounded so calibration does not intentionally cross that boundary.
- If calibration is not requested, is too late to start, or fails, the program falls back to a single-server synchronization attempt, subject to the `T−10 s` rule.
- If full calibration succeeds, its selected endpoint and correction are retained for later single-server refreshes.

### 6.3 Single-server synchronization eligibility

Initial single-server synchronization and every later refresh follow the same rule:

- A DNS resolution or NTP request MUST NOT be started when 10 seconds or less remain.
- No request should intentionally remain outstanding at `T−10 s`.
- The implementation shortens individual UDP timeouts to fit before the cutoff, leaving a 50 ms safety margin.
- Because the standard-library resolver does not expose a portable timeout, DNS resolution runs in a daemon worker and the calling thread stops waiting at a monotonic deadline. A late resolver worker MUST NOT hold up the target action.
- If a first NTP synchronization is not completed before the cutoff, the scheduler MUST continue using the system-clock model.
- If a later refresh fails, the last successful clock model MUST remain installed.

The default single-server host is `time.cloudflare.com`; it can be changed using `--server`.

### 6.4 Single-server sample procedure

The current implementation requests up to five samples per synchronization, with 150 ms spacing and the configured per-request timeout. The request count/spacing MUST be shortened or stopped as needed to respect `T−10 s`.

When installing a model, use the three lowest-delay samples (or all available if fewer than three) and take the median UTC-minus-monotonic mapping constant. A one-sample model receives an additional conservative uncertainty penalty and a warning.

After a successful NTP model installation, print the signed offset between the model time and the OS system wall clock:

\[
 offset_{system}=UTC_{model}-UTC_{system}
\]

A magnitude of at least 1,000 ms MUST produce a warning. The program MUST continue.

### 6.5 Refresh and retry policy

Defaults:

- Successful refresh interval: 30 seconds.
- Minimum interval between attempts / failed-sync retry interval: 5 seconds.
- Maximum assumed oscillator drift for uncertainty growth: 50 ppm.

When a prior NTP synchronization exists, a refresh is due when the successful-sync interval has elapsed and the minimum attempt interval has elapsed. When still using the system-clock fallback, failed/initial attempts are retried at the retry interval. All attempts remain subject to the `T−10 s` cutoff.

A failed refresh MUST NOT replace the last good model. The uncertainty proxy grows with elapsed monotonic time according to the configured drift allowance.

## 7. Deadline scheduler

### 7.1 Clock reference

All countdowns, retry deadlines, and final waits MUST use `time.monotonic_ns()` and the installed UTC-to-monotonic mapping. The scheduler MUST NOT rely on repeated calls to `datetime.now()` for the final countdown.

### 7.2 Waiting algorithm

1. Calculate remaining time from `target_utc_ns - clock.now_utc_ns()`.
2. While more than 100 ms remain, sleep rather than spin.
3. When a refresh or retry becomes due, wake and perform it only if more than 10 seconds remain.
4. Never start new NTP work inside the final 10 seconds.
5. During the final 100 ms, busy-poll the monotonic UTC mapping until the target is reached.
6. Return to `keystroke.py`, which injects the requested key.

The 100 ms spin window reduces sleep-wakeup latency at the cost of temporarily using one CPU core. OS scheduling, interpreter execution, and desktop input delivery remain outside the scheduler's control.

### 7.3 Failure behaviour

- DNS failure, NTP timeout, invalid response, rejected sample, or failed calibration MUST be reported.
- Initial failure MUST leave the system-clock model active.
- Later failure MUST leave the last successful NTP model active.
- The program MUST continue toward the target unless the user interrupts it or a fatal local desktop-initialization error prevents input injection.
- The NTP/model-vs-system offset warning is diagnostic only and MUST NOT terminate execution.

## 8. Startup keyboard preflight

### 8.1 Purpose and default

The startup keyboard preflight is enabled by default. Its purpose is to give the user an opportunity to focus the terminal, allow OS input permissions to be granted, and check whether synthetic key injection can be observed by a global keyboard listener.

The user may disable it using either:

- `--no-startup-key-test`
- `--skip-startup-key-test`

### 8.2 Procedure

1. If standard input is an interactive terminal, ask the user to focus that terminal and press Enter. If standard input is not a TTY, report that focus confirmation was skipped.
2. Start a `pynput` global keyboard listener.
3. Allow a short listener-startup interval (currently 200 ms).
4. Inject the first synthetic Enter key.
5. Wait for that Enter for up to 10 seconds, using an absolute `time.monotonic_ns()` deadline.
6. If the first Enter is captured, send the second Enter immediately. If not, send the second Enter after the first 10-second wait expires.
7. Wait for the second Enter for up to 1 second, again using an absolute monotonic deadline.
8. Stop the listener and perform a bounded join.
9. If the second probe is captured and the listener shuts down cleanly, report success. The first probe may fail without failing the overall preflight if the second succeeds.
10. If the second probe is not captured, injection fails, listener setup fails, or listener shutdown fails, print a prominent warning.
11. A preflight warning MUST NOT terminate scheduling.

Both probes use Enter because it is broadly supported by the current injection/capture backends. The terminal should remain focused: Enter is not a neutral key and can submit a command or insert a newline in whichever application has focus.

### 8.3 Timeout semantics

Each probe wait MUST have its own independent monotonic deadline. A missing first event MUST NOT prevent the second probe from being attempted. The listener MUST be stopped after the second probe, including on unsuccessful capture. A queue wait MUST always be bounded by the corresponding deadline.

## 9. Independent system-clock listener

`test_listener.py` is a manual integration/sanity tool and does not use NTP or the scheduler clock model.

Procedure:

1. Start it before the main keystroke program.
2. Resolve the next local occurrence of the requested target time using the OS system clock.
3. Sleep until approximately 100 ms before that target.
4. Arm the global keyboard listener and wait for the specified key (default Enter; `*` may be used to accept any key).
5. On the first matching callback, record `time.time_ns()`, stop listening, and print the key name, local timestamp, UTC timestamp, Unix nanoseconds, and signed lateness relative to the target.

The printed timestamp is when the Python callback ran. It is not a hardware or device-driver timestamp of when the physical/synthetic event first entered the operating system. The listener uses only the system wall clock for target arming and is intended to compare observed delivery with the scheduler's intended target.

## 10. Command-line interface

Current options:

| Option | Meaning | Current default |
|---|---|---|
| `--time HH:MM:SS` | Target local time | `14:30:00` |
| `--key KEY` | Key to inject once | `enter` |
| `--server HOST` | Single NTP server / refresh server | `time.cloudflare.com` |
| `--calibrate` | Enable optional multi-server calibration | Disabled |
| `--samples N` | Requested samples per calibration endpoint | `12` |
| `--sync-interval SEC` | Successful-sync refresh interval | `30` |
| `--retry-interval SEC` | Minimum interval between attempts | `5` |
| `--timeout SEC` | Maximum per-request NTP timeout before deadline shortening | `1.5` |
| `--calibration-report PATH` | JSON calibration report path | `ntp_calibration.json` |
| `--calibration-report ""` | Disable report writing | — |
| `--no-startup-key-test` | Disable preflight and focus prompt | Enabled by default |
| `--skip-startup-key-test` | Alias for disabling preflight | — |

Examples:

```bash
# One-shot Enter at 14:30, single-server sync when permitted
python keystroke.py --time 14:30:00 --key enter

# Optional full multi-server calibration, then keep the selected server
python keystroke.py --time 14:30:00 --key enter --calibrate

# Use a different single NTP server
python keystroke.py --time 14:30:00 --key enter --server time.google.com

# Disable startup keyboard check
python keystroke.py --time 14:30:00 --key enter --no-startup-key-test

# Run the independent system-clock listener before the scheduler
python test_listener.py --time 14:30:00 --key enter
```

## 11. Dependencies and platform considerations

The current dependency file contains:

- `pyautogui` — synthetic key injection.
- `pynput` — global key capture for startup checks and the independent listener.

NTP packet construction, UDP transport, statistics, monotonic timing, and JSON reporting use the Python standard library.

Platform limitations:

- Linux Wayland sessions may restrict global key capture and synthetic input. X11 is generally more compatible with these libraries.
- macOS may require Accessibility and Input Monitoring permissions.
- Windows may be subject to focus, privilege, desktop-session, and security restrictions.
- PyAutoGUI injects into the currently focused desktop application; the scheduler does not select or verify the intended destination window.

## 12. Test plan

The project keeps three distinct validation areas:

### 12.1 Directional mathematics unit tests

`tests/test_directional_math.py` MUST verify at least:

- Forward-only delay increase.
- Symmetrical delay increase.
- Reverse-only delay increase.

The expected values must follow the centred directional-deviation equations.

### 12.2 Startup keyboard preflight unit tests

`tests/test_startup_keyboard_check.py` MUST verify at least:

- Both Enter probes are captured.
- The second probe can succeed even if the first is lost.
- Failure prints a warning but returns control rather than exiting.
- Both timeout windows expire and cancel independently.
- Preflight is enabled by default.
- Both CLI disable flags disable it.

Tests SHOULD replace the real listener and injection API with deterministic fakes and shorten grace periods, so they do not require desktop permissions or wait 11 real seconds.

### 12.3 End-to-end listener sanity check

`test_listener.py` is run manually alongside `keystroke.py` to observe the injected key near the target. This test exercises real desktop input capture and reports callback-time deviation. It is not a unit test and cannot establish hardware-level event timing.

Run unit tests from the project directory:

```bash
python -m unittest discover -s tests -v
```

## 13. Non-functional requirements and explicit limitations

- The design SHOULD minimize expected timing error and avoid unnecessary work near the target.
- Full calibration and single-server synchronization MUST obey their respective cutoffs.
- Calibration and time-model failures SHOULD degrade to the best available prior source rather than stop execution.
- Network operations MUST be timeout-bounded; DNS waiting MUST be bounded independently of the platform resolver's blocking behaviour.
- The final 100 ms spin period SHOULD be configurable only if a later implementation explicitly exposes that tuning.
- Raw calibration evidence SHOULD be retained so the mathematical assumptions can be evaluated against real network measurements.

No implementation based on ordinary Python, public Internet NTP, standard desktop scheduling, and PyAutoGUI can guarantee that the input event is delivered within 10 ms. Sources of irreducible or uncontrolled error include:

1. Unknown forward/reverse network-delay asymmetry.
2. Server clock error and server/path correlation.
3. Local NIC, driver, kernel, and user-space timestamping delays.
4. OS scheduler preemption and Python runtime latency.
5. Input injection backend and desktop event-queue latency.
6. Global listener callback scheduling latency in the test harness.

The reported NTP uncertainty and calibration score are estimates for diagnostics and server ranking. They MUST NOT be described as formal worst-case error guarantees.

## 14. Acceptance criteria

A release is acceptable when:

- The scheduler module can be imported without importing PyAutoGUI or parsing CLI arguments.
- The system clock is installed before any NTP work.
- Full calibration is optional and never intentionally crosses `T−60 s`.
- No DNS resolution or NTP request is initiated/continued intentionally inside `T−10 s`.
- Initial and later NTP failures preserve the best available time model.
- Successful model installations print signed offset versus system time and warn at the configured large-offset threshold without exiting.
- The final wait uses a monotonic clock and transitions to a final short busy-wait.
- The startup keyboard test is enabled by default, uses two Enter probes with independent 10 s and 1 s deadlines, warns non-fatally on failure, and can be disabled by CLI flag.
- The standalone listener remains system-clock-only.
- Directional mathematics and keyboard preflight unit tests pass.
- The documentation clearly distinguishes measured directional deviations and relative ensemble correction from absolute one-way delay or guaranteed UTC accuracy.
