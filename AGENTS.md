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
- Pre-commit runs **detect-secrets** (`.secrets.baseline`). If it flags a false
  positive, add `pragma: allowlist secret` on that line (or `pragma: allowlist
  nextline secret` on the line above, for formats without trailing comments
  such as systemd units), or refresh and audit the baseline:
  `detect-secrets scan --baseline .secrets.baseline && detect-secrets audit .secrets.baseline`.
  Never "fix" a real finding by adding it to the baseline.

## Current state

| Path | Status |
| --- | --- |
| `scripts/quickstart.sh` | **Fresh clone to running simulation**: `.venv` + editable install, docker/port checks, SITL image pull, `apps/sim/run_sitl.sh` at 20x, prints `READY <url>`. `--install-only`, `--test`. Used by the `install-and-run` skill (`.claude/skills/`); `CLAUDE.md` imports this file. |
| `apps/` | One Python app per folder, each its own wheel with `pyproject.toml`, `src/<package>/` and `tests/`. Dependencies only point down: `forecast` <- `lowpower` <- `sim`, and `forecast` <- `web`. `web` and `lowpower` share only the state files, never imports. |
| `apps/forecast/` | **`scout-forecast`** (`scout_forecast`, stdlib only, no dependencies): `data.py` (load/clean/resample the Tether CSV), `energy.py` (physical model: grid, fit, simulate, project), `evaluate.py` (blocked, purged k-fold), `forecast.py`. Also what the other apps share: `env.py` (`.env` and `--csv`), `clock.py` (simulated clock, `SCOUT_CLOCK`). Commands: `forecast`, `scout-evaluate`, `scout-data`. Best model so far (3.1 points over 12 h with a known plan). Its tests check it never imports the apps above it. |
| `apps/lowpower/` | **`scout-lowpower`** (`scout_lowpower`, the boat service, stdlib + scout-forecast): `planner.py` (pure decision logic), `mission.py` (operator mission file), `mavlink.py` (MAVLink v2 codec + self-reconnecting tcp/udp/serial links), `service.py` (the run loop: telemetry, decisions, commands, recovery), `cli.py` (`scout-lowpower run / set-mission / status / plan`). Survives link loss, autopilot restarts (resync, mission check, re-arm), operator overrides and its own restarts. Tests: planner decisions, codec (byte-for-byte vs pymavlink if installed), recovery against `tests/fake_autopilot.py` (~35 s). |
| `apps/web/` | **`scout-web`** (`scout_web`, stdlib + scout-forecast): `server.py` + `static/index.html`, forecast vs actual page (no external assets, localhost by default). Reads the service's `decision.json` / `history.jsonl`; `/api/hindcast` forecasts past periods from the CSV. |
| `apps/sim/` | **`scout-sim`** (never on the boat): environment (current, wind, cloud) pushed into ArduPilot 4.0 SITL as `SIM_TIDE` drift, and a power model publishing `BATTERY_STATUS` to the service's `--power-link`. `apps/sim/run_sitl.sh` runs SITL (docker `radarku/ardupilot-sitl`) + sim + service + web. |
| `model/lstm/train_lstm.py` | Experiment, not an app. Keras LSTM forecasting the change in SoC, checked with blocked, purged k-fold. No app may import or mention it. Needs scout-forecast plus `model/lstm/requirements.txt`; training only, not for the boat. Run with `python3 -m model.lstm.train_lstm`; output goes to `runs/lstm/`. |
| `packaging/` | **Debs, installed with apt.** `build.sh` builds the wheels and, from them, `deb/build_debs.py` builds `scout-forecast`, `scout-lowpower`, `scout-web`, `scout-sim` and the `scout` metapackage (code in `/usr/lib/python3/dist-packages`, units in `/usr/lib/systemd/system`, `/etc/scout/scout.env` conffile, generated maintainer scripts) plus a flat apt repository. `systemd/` has the units (`scout-lowpower` notify+watchdog, `scout-web`, simulation-only `scout-sitl` and `scout-sim`). `version.py` keeps every app at one version. `test/smoke.sh` installs the debs under systemd in a container and checks them (~1 min). |
| `.github/workflows/release.yml` | Runs only on pushes to `main`: bump patch version of every app, unit tests, build, smoke test (with upgrade from the previous release), then push `Release vX.Y.Z [skip ci]` + tag and publish a GitHub release. |

Update this table when you add components.

## Environment and constraints

- Python 3.9 (system `python3` on macOS). Do not use syntax newer than 3.9, such as
  `match` or `X | Y` types at runtime. `from __future__ import annotations` is fine.
- **Standard library only for code that runs on the boat** (data loading, forecast,
  service). Keep heavier dependencies such as numpy, pymavlink or a web framework out
  of that code path. If you add any, list them in a requirements file and say which
  component needs them.
- The finished system runs on Linux with systemd. **Don't hard-code paths**: no data
  file names, no repo location (`Path(__file__).parents[...]`), no `sys.path` edits.
  Take inputs via `--csv` / `SCOUT_TELEMETRY_CSV` (`scout_forecast.env.csv_arg`,
  which also reads a gitignored `.env`; shell variables win), and write outputs
  relative to the current directory. Fixed install paths (`/usr/bin`,
  `/etc/scout`, `/var/lib/scout-lowpower`) belong only in `packaging/` (units,
  debs). Boat-side code goes in an app under `apps/`
  and is exposed through `[project.scripts]` in that app's `pyproject.toml`.
- A new app gets its own folder in `apps/`, depends only on apps below it, and is
  added to `requirements-dev.txt`, `packaging/build.sh` and (as a deb)
  `PACKAGES` in `packaging/deb/build_debs.py`.
- **Versions:** never edit `__version__` or the `==` pins by hand; CI bumps them
  on every merge into `main`. Use `packaging/version.py set X.Y.Z` if you must.

## Commands

```sh
scripts/quickstart.sh                # fresh clone -> .venv -> simulation at 20x, prints READY <url>
scripts/quickstart.sh --test         # every app's unit tests
packaging/build.sh                   # wheels + debs + apt index in ./dist
packaging/test/smoke.sh              # the debs under systemd in docker, ~1 min

# Need the private CSV (--csv, SCOUT_TELEMETRY_CSV or .env); outputs go under ./runs/
scout-data --summary                 # links, modes, flags, restarts, value ranges
forecast --from 2025-07-23T00:00Z --hours 24 [--plan persist]
scout-evaluate                       # blocked, purged k-fold; writes params.json fitted on all data

scout-lowpower plan --lat 60.2 --lon 2.2 --pct 55   # what-if, no autopilot
python3 -m model.lstm.train_lstm     # LSTM experiment (model/lstm/requirements.txt)
```

macOS's system pip (21.2) can't install these projects; `quickstart.sh` uses a
venv with a current pip.

As a library:

```python
from scout_forecast.data import load, resample
rows = load("scout_telemetry_2025-07.csv")  # list of dicts, sorted by time, UTC datetimes
buckets = resample(rows, 300)
```

## Data facts to keep in mind

These come from inspecting the raw file. `scout_forecast.data` already handles them, so don't
work around them a second time downstream.

- **Timestamps are receipt times** (`created_at`), not sample times. The fractional
  part varies in length, so Python 3.9's `fromisoformat` fails on it. Use
  `scout_forecast.data.parse_time`.
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

## ArduPilot / SITL facts (learned the hard way)

- The `radarku/ardupilot-sitl` image is ArduPilot 4.0.3 (amd64, emulated on Apple
  silicon). It has **no JSON external-physics backend**, so the boat physics are
  SITL's own `sailboat-motor` model.
- That model applies `SIM_TIDE_SPEED` / `SIM_TIDE_DIR` (direction it comes **from**)
  **only while armed**. Wind only acts on the sail, so `scout-sim` folds hull
  leeway into the tide vector and sets `SIM_WIND_SPD 0`.
- Mission items must use `MAV_FRAME_GLOBAL_RELATIVE_ALT` (3) even in
  `MISSION_ITEM_INT`; 4.0 rejects frame 6 with code 2. The fake rover in `apps/lowpower/tests/`
  enforces this.
- Stock Rover mode 17 is only `ROVER_MODE_ENUM_END`, so Scout's MODE_17 is a
  SentryOS mode. The service only commands AUTO, HOLD and LOITER.
- **Never boot systemd in a privileged container without masking
  `systemd-binfmt`**: it clears the shared Docker VM's binfmt handlers and kills
  x86-64 emulation for every container (SITL then fails with "exec format
  error"; restarting Docker Desktop restores it). `packaging/test/Dockerfile.systemd`
  masks it.
- Right after arming, SITL briefly reports "EKF variance / EKF failsafe" and may
  reject one mode change; it clears within seconds and the service retries.
- Message intervals are per MAVLink link: each client requests its own streams.
- **ArduPilot's speed target is over ground**; the energy model and the planner
  think in speed through the water. With current the two differ a lot (see
  REPORT.md, Open issues). The `sailboat-motor` hull tops out near 0.8 m/s
  through the water.
- A reboot shorter than the 5 s heartbeat timeout shows only in the boot clock
  (`time_boot_ms`); keep tests giving the fake autopilot a few seconds of uptime
  before rebooting it.

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
