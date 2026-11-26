# scout-forecast

Battery forecast for Scout, a solar-electric robot boat. Standard library only
(Python 3.9+), so it runs on the boat's computer.

## Install

```sh
pip install .                     # or: pip wheel . -w dist && pip install dist/scout_forecast-*.whl
pip install -e .                  # development (needs pip >= 21.3)
```

## Configure

The telemetry CSV is never built in. Pass `--csv`, set `SCOUT_TELEMETRY_CSV`,
or put it in a `.env` (see `.env.example`). Shell variables override `.env`.
Relative paths in `.env` are relative to the `.env` file.

## Commands

```sh
forecast --from 2025-07-23T00:00Z --hours 24            # battery forecast vs what happened
forecast --from 2025-07-23T00:00Z --hours 24 --plan persist --json out.json
scout-evaluate                                          # blocked, purged k-fold; writes ./runs/physical/
scout-data --summary                                    # clean / resample the raw Tether export

scout-lowpower set-mission --transit 60.30,2.20 60.50,2.20 --arrive-by 2025-07-25T12:00Z --reserve 30
scout-lowpower set-mission --station 60.50,2.20 --radius 50 --drift-radius 500 --reserve 35
scout-lowpower run --mavlink tcp:127.0.0.1:5760 --params params.json
scout-lowpower status
```

## Low-power mode

`scout-lowpower run` reads the operator's mission (local file, so no shore link
needed), forecasts the battery for each way of operating, and commands
ArduPilot Rover over MAVLink: mission upload, AUTO/HOLD, cruise speed
(`WP_SPEED` + `DO_CHANGE_SPEED`), loiter radius, and optionally a Starlink
power relay. Decisions, reasons and forecasts go to the log (journald) and to
`decision.json` / `history.jsonl` in the state directory.

Settings: flags, else `SCOUT_MAVLINK`, `SCOUT_STATE_DIR` (or systemd's
`$STATE_DIRECTORY`), `SCOUT_MISSION`, `SCOUT_PARAMS`, also read from `.env`.

## Simulation (ArduPilot SITL) and web page

```sh
pip install -e . -e sim                 # sim/ is a separate package, never on the boat
sim/run_sitl.sh                         # SITL in docker + scout-sim + scout-lowpower + scout-web
SIM_RATE=20 sim/run_sitl.sh             # 20x clock: ~4 simulated hours in 12 minutes
open http://127.0.0.1:8080
```

* **SITL**: the `radarku/ardupilot-sitl` image (ArduPilot 4.0, Rover,
  `sailboat-motor` boat model), started with `sim/sitl/scout.parm`.
* **scout-sim** (`sim/`): current (mean + M2 tidal ellipse), wind (gusty,
  hours-long weather) with hull leeway, and cloud are pushed into SITL as the
  `SIM_TIDE` drift vector, so ArduPilot's own controllers fight them. Its power
  model turns speed *through the water* into thruster load, adds hotel and
  Starlink, computes solar from sun position and cloud, runs the battery, and
  sends `BATTERY_STATUS` (battery, load, solar) to the service's power link.
  Its "true" parameters are ~10 % off the fitted ones on purpose.
* A shared clock (`SCOUT_CLOCK`, from `scout-sim clock`) lets the service and
  the simulator agree on a simulated July date and rate.
* **scout-web**: stdlib server, no external assets (works offline on the
  boat). Shows the current decision and why, battery readings against any
  earlier forecast, forecast error by lead time against "stays the same",
  load and solar, and forecasts of historical periods from the telemetry CSV.

## Tests

```sh
pip install -e .
python -m unittest discover -s tests
python -m unittest discover -s sim/tests     # with sim installed
```

See `REPORT.md` for findings and accuracy, and `AGENTS.md` for project conventions.
