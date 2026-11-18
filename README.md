> [!NOTE]
>
> <table>
>   <tr>
>     <td align="center">
>       <a href="https://0rwx.com" target="_blank" rel="noopener noreferrer"
>         title="AI:X (0rwx.com)">
>         <picture>
>           <source srcset="https://cdn.by-cra.net/static/0rwx.com/AI-X-dark.svg"
>                   media="(prefers-color-scheme: dark)">
>           <img src="https://cdn.by-cra.net/static/0rwx.com/AI-X-light.svg"
>                alt="AI:X (0rwx.com)"
>                height="32">
>         </picture>
>       </a>
>     </td>
>     <td>AI:X Code written by ChatGPT, idea for calibration process by me</td>
>   </tr>
> </table>

NTP-SYNCHRONIZED KEYSTROKE SCHEDULER
===================================

Files
-----
scheduler.py               Core library: NTP queries, calibration, clock model,
                           and waiting. No CLI and no keyboard injection.
keystroke.py               CLI entry point and one-shot keystroke injection.
test_listener.py            System-clock-only global key listener for sanity checks.
tests/test_directional_math.py      Synthetic tests for directional-delay equations.
tests/test_startup_keyboard_check.py  Tests for the Enter-based startup keyboard check.
requirements.txt            Dependencies for the command-line and test tools.
README.md                   Usage documentation.
SPEC.md                     Technical specification for algorithms and procedures.

Install
-------
python -m pip install -r requirements.txt

Run the listener first
----------------------
python test_listener.py --time 14:30:00 --key enter

Startup keyboard preflight
--------------------------
The preflight is enabled by default. First, focus the terminal and press Enter
when prompted. The program then starts a global keyboard listener and sends an
Enter keypress. It allows up to 10 seconds for capture so you can approve an
OS permission prompt if necessary. It then sends a second Enter and allows up
to 1 second for immediate capture. A failure produces a prominent warning but
does not stop scheduling. The injected Enter keypresses go to the focused
application; keep the terminal focused during this check.

Disable the preflight (including the terminal-focus prompt) with:
    python keystroke.py --no-startup-key-test

The equivalent alias is --skip-startup-key-test.

Run the keystroke entry point with multi-server calibration
-----------------------------------------------------------
python keystroke.py --time 14:30:00 --key enter --calibrate

Calibration is optional. Disable it and use one server directly:
    python keystroke.py --time 14:30:00 --key enter
    python keystroke.py --time 14:30:00 --key f5 --server time.google.com

Adjust calibration samples (default 12 per server):
    python keystroke.py --time 14:30:00 --key enter --calibrate --samples 20

Disable the JSON calibration report:
    python keystroke.py --time 14:30:00 --key enter --calibrate --calibration-report ""

Run unit tests
----------------------------
python -m unittest discover -s tests -v

Using scheduler.py as a library
-------------------------------
scheduler.py can be imported without importing PyAutoGUI or parsing command-line
arguments. Its main public pieces are:

- calibrate(...): collect multi-server measurements and select a server.
- SyncedClock: maintain an NTP-derived UTC-to-monotonic clock model.
- next_local_target_utc_ns(...): resolve a local HH:MM:SS target.
- wait_until(...): wait for a target while refreshing the selected NTP server.

Calibration mathematics
-----------------------
For one server, let delta be the measured NTP round-trip delay and m be the
measured NTP clock offset. If the server's true clock offset is effectively
constant over the short calibration window:

    delta = d_forward + d_reverse
    m     = theta + (d_forward - d_reverse)/2

For changes relative to the per-server calibration medians:

    forward_deviation = (delta - median(delta))/2 + (m - median(m))
    reverse_deviation = (delta - median(delta))/2 - (m - median(m))

These are directional-delay DEVIATIONS, not absolute one-way delays. If the
server clock itself changes during calibration, that change is confounded with
the directional-delay changes. Round-robin probing reduces time-trend
confounding but cannot eliminate it.

The calibration computes a low-delay reference for each server (the median
UTC-minus-monotonic mapping among that server's three lowest-RTT samples) and
uses the median of those per-server references as an ensemble reference. For
server j:

    baseline_j = median(wall_mid + NTP_offset - monotonic_mid)
                  over that server's three lowest-RTT samples
    relative_bias_j = baseline_j - median(server_baselines)
    corrected_live_mapping = live_mapping - relative_bias_j

This aligns the selected server to the cross-server median. It is only applied
when at least three distinct resolved endpoints produce enough valid samples.
It assumes the servers have reasonably independent time/path errors whose
median is a useful reference. It cannot identify or eliminate common local
outbound-vs-inbound asymmetry, and it cannot prove absolute UTC accuracy.
Servers may share infrastructure, and remote asymmetry need not be zero-centred.

The JSON report ntp_calibration.json preserves raw samples and derived
forward/reverse deviations for later analysis.

Selection score
---------------
The server selection score combines minimum RTT/2, server root-distance
information, offset jitter and the larger of the forward/reverse variation
jitter estimates. It is a practical ranking heuristic, not a formal error
bound. The cross-server relative-bias correction uncertainty is included in
the displayed uncertainty proxy.

Scheduling and limitations
--------------------------
- The target is the next local HH:MM:SS occurrence. If today's time has
  already passed when the entry point starts, it schedules tomorrow.
- The local system clock is installed as the first time source, so a usable
  schedule exists even when NTP is unavailable.
- Full multi-server calibration runs only when more than 60 seconds remain.
  Its measurements stop at the T-60 s boundary. If full calibration is not
  eligible (or fails), the program uses one NTP server instead.
- No initial NTP request or later single-server refresh is started with 10
  seconds or less remaining. Individual UDP timeouts are shortened to fit
  before the T-10 s boundary. DNS resolution is also bounded by a daemon
  resolver thread, so a slow system resolver cannot hold up the keypress. If
  no first NTP sync can be completed, the scheduler stays on system time
  rather than delaying or exiting.
- After every successful NTP model installation (full calibration or
  single-server refresh), the program prints the NTP/model offset relative to
  the system clock. An absolute difference of 1 second or more produces a
  warning, but does not stop execution.
- The scheduler uses time.monotonic_ns() for countdowns and busy-polls during
  the final 100 ms. System wall-clock steps do not directly jump the countdown.
- Failed refreshes retain the last successful clock model and increase the
  displayed uncertainty proxy with the configured oscillator-drift allowance.
- If initial synchronization fails, the scheduler continues with system time
  and retries at the configured retry interval, provided more than 10 seconds
  remain.
- The 10 ms target is not guaranteed. NTP path asymmetry, server clock errors,
  timestamping, OS scheduling and input-injection latency remain.
- test_listener.py intentionally uses only the system wall clock for its
  arming wait. It reports callback time, not a hardware/driver event timestamp.
- macOS may require Accessibility/Input Monitoring permissions. Linux Wayland
  may restrict global keyboard monitoring and synthetic input; X11 is usually
  more compatible. The keystroke entry point requires an interactive desktop.


## Startup keyboard preflight

The two Enter probes have independent 10-second and 1-second capture deadlines.
After successful capture, listener shutdown is given up to one second. A slow
shutdown is reported as a non-fatal cleanup notice, not as a keyboard test
failure; the listener runs as a daemon and cannot prevent process exit.
