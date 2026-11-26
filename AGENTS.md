# AGENTS.md

Guidance for coding agents (and people) working in this repository.

## What this is

An energy forecast and a low-power mission mode for **Scout**, a small solar-electric
uncrewed surface vessel that runs ArduPilot Rover. The project has four parts:

1. An energy model fitted to real telemetry, with a CLI forecast
   (`forecast --from <ISO time> --hours <n>`).
2. A service that runs on the boat. It talks MAVLink to ArduPilot and decides how the
   boat operates so it meets an arrival time or keeps a minimum battery reserve.
3. A simulation run against ArduPilot SITL (Rover), with a small web page that compares
   the forecast with what actually happened.
4. systemd packaging, plus tests of the decision logic and of recovery from failures.

The full requirements are in `BRIEF.md` and the column reference is in `DATA.md`.

## Files you must not commit or publish

`.gitignore` excludes `*.csv`, `BRIEF.md` and `DATA.md` on purpose. They are private
inputs and may not exist in a fresh clone.

- Never `git add -f` them, paste their contents into commits or PRs, or send them to
  outside services.
- Do not copy large parts of `BRIEF.md` into tracked files. Summarise it instead.
- Generated data goes in `data/`. It is `.csv`, so it is ignored too.
- Code must not assume the raw CSV is present. Take its path as an argument.

## Current state

| Path | Status |
| --- | --- |
| `scout_data.py` | Done. Loads, cleans and resamples the raw Tether CSV export. |
| `model/train_lstm.py` | Experimental. Keras LSTM forecasting the change in SoC, checked with blocked, purged k-fold. Needs `model/requirements.txt`; training only, not for the boat. Runs go to `model/runs/` (gitignored). |
| forecast, service, SITL sim, web page, systemd units, tests | Not started yet |

Update this table when you add components.

## Environment and constraints

- Python 3.9 (system `python3` on macOS). Do not use syntax newer than 3.9, such as
  `match` or `X | Y` types at runtime. `from __future__ import annotations` is fine.
- **Standard library only for code that runs on the boat** (data loading, forecast,
  service). Keep heavier dependencies such as numpy, pymavlink or a web framework out
  of that code path. If you add any, list them in a requirements file and say which
  component needs them.
- The finished system runs on Linux with systemd. Don't hard-code macOS paths.

## Commands

```sh
# Summary of the raw data: links, modes, flags, restarts, value ranges
python3 scout_data.py scout_telemetry_2025-07.csv --summary

# One cleaned row per message
python3 scout_data.py scout_telemetry_2025-07.csv -o data/clean.csv

# Fixed-interval averages (in seconds), with backlog rows excluded
python3 scout_data.py scout_telemetry_2025-07.csv -o data/5min.csv --resample 300
```

As a library:

```python
from scout_data import load, resample
rows = load("scout_telemetry_2025-07.csv")  # list of dicts, sorted by time, UTC datetimes
buckets = resample(rows, 300)
```

## Data facts to keep in mind

These come from inspecting the raw file. `scout_data.py` already handles them, so don't
work around them a second time downstream.

- **Timestamps are receipt times** (`created_at`), not sample times. The fractional
  part varies in length, so Python 3.9's `fromisoformat` fails on it. Use
  `scout_data.parse_time`.
- **Three links:**
  - `direct` (Starlink) has every field and arrives in bursts at about 1 Hz.
  - `imt` (Iridium) arrives about every 5 minutes. It has no solar readings, no battery
    current and no thruster voltage, and its speed is rounded to 0.2 m/s.
  - `globalstar` is position only. Its `battery_pct` is always a placeholder 0.
- **Starlink stops at 2025-07-27 02:05Z.** After that only Iridium data exists, so
  solar and battery current can't be checked against data for the last ~3.5 days.
- **Iridium backlog:** rows that arrive less than 2 s after the previous Iridium row are
  a flushed queue, flagged `backlog`. Their timestamps and order can't be trusted.
- **Autopilot restarts** (mode 16, INITIALISING) at 07-25 19:38 and 07-27 19:00 come with
  zeroed fields, flagged `reboot`. These are useful real examples for the failure tests.
- **Units and signs:**
  - `temperature_c` arrives in hundredths of a degree; the loader divides by 100.
  - Battery current is positive when charging and negative when discharging.
  - **The "thruster" channel (`thruster_a`/`thruster_w`) is the whole boat's load**,
    not just the thruster. At night it equals battery discharge to within 0.07 A. Its
    idle reading (≈5 W disarmed, ≈9 W armed) is the hotel load. The column names are
    kept for compatibility. See `REPORT.md`.
  - **`battery_pct` is 20 × V − 400** (a voltage reading, not stored energy). It sags
    under load and sticks at 100 % at 25 V. Don't use it as an energy target.
  - Solar power is V×A on the solar channel. Whether that's before or after the charge
    controller is unconfirmed; the small `other_w` suggests losses are small.
  - The load channel shares the battery bus, so load power uses the battery voltage
    when the thruster voltage is missing.
  - `other_w` = solar − load − battery: conversion losses plus timing mismatch. It
    averages ≈2 W and can be slightly negative.
- **Mode 17** is not a stock ArduPilot Rover mode number. It is labelled `MODE_17` until
  confirmed. Don't guess its meaning in code or in write-ups.
- **Missing values are `None`.** In resampled output, a missing bucket is a gap, not a
  zero.

## Working conventions

- **Validate on data you didn't fit to.** Split by time, not by random rows, and report
  forecast error only on the held-out window.
- **Write numbers with their source**, e.g. "idle load ≈ 9 W, from the load channel when
  armed with no thrust and no sun". The write-up needs this for every figure.
- **Keep the decision logic separate from MAVLink I/O and pure**, so it can be
  unit-tested without SITL.
- **The service must survive a shore-link outage and an autopilot restart.** Keep state
  on local disk, reconnect with backoff, and log to journald.
- Commit on small changes, if you're on `main`, create a branch first.
- Keep changes small. The brief values judgement over volume, so prefer a simple model
  you can explain over a complex one you can't.
