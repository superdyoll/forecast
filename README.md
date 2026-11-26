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

## Tests

```sh
pip install -e .
python -m unittest discover -s tests
```

See `REPORT.md` for findings and accuracy, and `AGENTS.md` for project conventions.
