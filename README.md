# Scout energy forecast and low-power mode

Battery forecast and low-power mission mode for Scout, a solar-electric robot
boat. Standard library only (Python 3.9+), so it runs on the boat's computer.

## Quick start

```sh
git clone https://github.com/superdyoll/forecast.git && cd forecast
scripts/quickstart.sh          # installs into .venv, runs the simulation at 20x,
                               # prints "READY http://127.0.0.1:8080"
```

Needs Python 3.9+ and docker running (the first run pulls the ArduPilot SITL
image, several GB). `scripts/quickstart.sh --test` runs the tests instead. With
Claude Code, just ask it to install and run: the `install-and-run` skill in
`.claude/skills/` does the above.

## Apps

Each app in `apps/` is its own wheel. Dependencies only point down the table.

| App | Package | Commands | On the boat |
| --- | --- | --- | --- |
| `apps/forecast` | `scout-forecast`: telemetry cleaning, energy model, shared settings and clock | `forecast`, `scout-evaluate`, `scout-data` | yes |
| `apps/lowpower` | `scout-lowpower`: planner + MAVLink service (needs scout-forecast) | `scout-lowpower` | yes |
| `apps/web` | `scout-web`: forecast against actual (needs scout-forecast; reads lowpower's state files) | `scout-web` | yes |
| `apps/sim` | `scout-sim`: sea, sky and power around ArduPilot SITL | `scout-sim` | no |

`model/lstm/` is a training experiment, not an app.

## Install

```sh
pip install -r requirements-dev.txt     # development: all apps, editable (pip >= 21.3)
packaging/build.sh                      # dist/wheels/*.whl, dist/debs/*.deb + apt index
```

On the boat, install the debs with apt (next section).

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

## Install on the boat (Debian/Ubuntu, apt)

Every release (see [Releases](#releases)) publishes one deb per app and the
apt index for them. Download the latest release's boat debs (`scout` is a
metapackage, so it needs the three it depends on):

```sh
repo=https://github.com/superdyoll/forecast
# /releases/latest redirects to the newest tag, e.g. .../releases/tag/v0.1.1
tag="$(curl -fsSLo /dev/null -w '%{url_effective}' "$repo/releases/latest")"
tag="${tag##*/}"; version="${tag#v}"
mkdir -p "scout-$version" && cd "scout-$version"
for p in scout scout-forecast scout-lowpower scout-web; do
  curl -fsSLO "$repo/releases/download/$tag/${p}_${version}_all.deb"
done
```

Install them on the boat. If the boat has no network, download on shore and
copy the folder across: the only dependencies, `python3` and `adduser`, are on
every Debian.

```sh
sudo apt install ./scout*_"$version"_all.deb  # forecast + lowpower + web; units start
# or from a copy of all the release's files (debs + Packages + Release):
#   echo "deb [trusted=yes] file:/srv/scout ./" | sudo tee /etc/apt/sources.list.d/scout.list
#   sudo apt update && sudo apt install scout
sudo cp params.json /etc/scout/params.json    # energy model from scout-evaluate
sudoedit /etc/scout/scout.env                 # SCOUT_MAVLINK etc.; kept across upgrades
sudo systemctl restart scout-lowpower
sudo -u scout scout-lowpower set-mission --transit 60.30,2.20 60.50,2.20 --reserve 30
scout-lowpower status
journalctl -u scout-lowpower -f
```

| Package | Contents |
| --- | --- |
| `scout` | metapackage for the boat: the three below, at the same version |
| `scout-forecast` | `forecast`, `scout-data`, `scout-evaluate`; the energy model |
| `scout-lowpower` | `scout-lowpower`, `scout-lowpower.service`, `/etc/scout/scout.env` (conffile), the `scout` system user |
| `scout-web` | `scout-web`, `scout-web.service` |
| `scout-sim` | simulation only, never on the boat: `scout-sim`, `scout-sitl.service` (SITL in docker, started only if docker is installed), `scout-sim.service`, `/etc/scout/sim.env`; drop-ins put the boat units on the simulated clock |

The packages pin each other with `(= version)`, so apt never mixes releases.
Code goes in `/usr/lib/python3/dist-packages` (pure Python, standard library
only: no venv needed), units in `/usr/lib/systemd/system`. A fresh install
enables and starts the units; an upgrade restarts those that were running and
keeps your `scout.env` edits; `apt purge` removes config and state
(`/var/lib/scout-lowpower`). To roll back, install the older release's debs:
`sudo apt install --allow-downgrades ./scout*_0.1.4_all.deb`.

| Unit | Runs | Notes |
| --- | --- | --- |
| `scout-lowpower.service` | the low-power mode | `Type=notify`, 30 s watchdog, `Restart=always`, never gives up; state in `/var/lib/scout-lowpower`; hardened (read-only system, no home, own user) |
| `scout-web.service` | the web page | reads the service's state dir; `127.0.0.1:8080` unless `SCOUT_WEB_HOST` says otherwise |
| `scout-sitl.service` | simulation: SITL in docker | restarting keeps the container, so SITL keeps its mission like a real autopilot |
| `scout-sim.service` | simulation: sea, sky, power | feeds `BATTERY_STATUS` to the service's power link |

## Releases

`.github/workflows/release.yml` runs on every push to `main` (each merged pull
request) and nowhere else. It bumps the patch version of every app together
(`packaging/version.py bump patch`), runs the unit tests on Python 3.9, builds
the wheels and debs (`packaging/build.sh`), and smoke-tests the debs under
systemd (`packaging/test/smoke.sh`: install via apt, upgrade from the previous
release, drive a fake autopilot, `kill -9`, add and purge `scout-sim`, purge
everything). Only if all of that passes does it push `Release vX.Y.Z [skip ci]`
to `main`, tag `vX.Y.Z`, and publish a GitHub release with the debs, the apt
index and the wheels. Run it by hand (Actions > release) to bump minor or major.
If `main` is protected, give it a `RELEASE_TOKEN` secret that may push there.

Locally (Docker; on macOS the debs are built in a Debian container):

```sh
packaging/version.py                              # current version (all apps agree)
PYTHON=.venv/bin/python packaging/build.sh        # dist/
packaging/test/smoke.sh                           # ~1 min; KEEP=1 leaves scout-smoke running
PREVIOUS=old/debs packaging/test/smoke.sh         # also test the upgrade from old/debs
```

## Simulation (ArduPilot SITL) and web page

### From the debs (Linux, never on the boat)

The simulator is the boat install plus one package, `scout-sim`. It needs
docker (apt installs `docker.io` if no docker is present) and runs ArduPilot
Rover SITL in a container, which it pulls on first run. Use an x86-64 machine;
on arm64 also `apt install qemu-user-static binfmt-support` (the SITL image is
x86-64 only).

```sh
# with $repo, $tag and $version set as in "Install on the boat":
curl -fsSLO "$repo/releases/download/$tag/scout-sim_${version}_all.deb"
sudo apt install ./scout*_"$version"_all.deb  # boat packages + scout-sim (+ docker.io)
journalctl -u scout-sitl -f                   # first run: pulls the SITL image (several GB
                                              # unpacked), then "starting SITL x1"; Ctrl-C
sudo -u scout scout-lowpower set-mission \
  --transit 60.215,2.210 60.230,2.240 60.240,2.200 --arrive-in 4 --reserve 30
xdg-open http://127.0.0.1:8080                # updates itself every few seconds
```

Installing `scout-sim` puts the boat services on a simulated clock (July sun
at 60 N, starting 06:00Z) from `/etc/scout/sim.env`, and starts `scout-sitl`
(SITL in docker) and `scout-sim` (sea, sky and power). `--arrive-in` is on that
clock. To run 20x faster (about 4 simulated hours in 12 minutes), start the
clock again at the new rate; SITL is recreated to match, and the service then
decides every 15 s instead of every 5 min:

```sh
sudo sed -i "s|^SCOUT_CLOCK=.*|SCOUT_CLOCK=$(scout-sim clock --start 2025-07-23T06:00Z --rate 20)|" /etc/scout/sim.env
sudo rm -f /var/lib/scout-lowpower/history.jsonl  # history from the old clock
sudo systemctl restart scout-sitl scout-sim scout-lowpower scout-web
sudo -u scout scout-lowpower set-mission \
  --transit 60.215,2.210 60.230,2.240 60.240,2.200 --arrive-in 4 --reserve 30
```

`sudo apt purge scout-sim` puts the services back on the real clock and
removes the SITL container (`docker image rm radarku/ardupilot-sitl` frees the
image too).

### From a checkout (development, any OS with docker)

```sh
pip install -r requirements-dev.txt     # includes apps/sim, which never goes on the boat
apps/sim/run_sitl.sh                    # SITL in docker + scout-sim + scout-lowpower + scout-web
SIM_RATE=20 apps/sim/run_sitl.sh        # 20x clock: ~4 simulated hours in 12 minutes
open http://127.0.0.1:8080
```

The SITL container keeps the rate it was created with: after changing
`SIM_RATE`, `docker rm -f scout-sitl` first.

### What runs

* **SITL**: the `radarku/ardupilot-sitl` image (ArduPilot 4.0, Rover,
  `sailboat-motor` boat model), started with `apps/sim/sitl/scout.parm`.
* **scout-sim** (`apps/sim/`): current (mean + M2 tidal ellipse), wind (gusty,
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
  The page checks for new data every 5 s and redraws only when it changed
  (the server answers 304 otherwise, cheap over a satellite link). It keeps
  your choices, waits while you hover a chart, pauses in a background tab, and
  says when the server can't be reached. The compared forecast defaults to
  "auto" (made ~3 h before the latest), which moves forward by itself.

## Tests

```sh
pip install -r requirements-dev.txt
for d in apps/*/tests; do python -m unittest discover -s "$d" || break; done
```

See `REPORT.md` for findings and accuracy, and `AGENTS.md` for project conventions.
