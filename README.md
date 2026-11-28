# Scout energy forecast and low-power mode

Battery forecast and low-power mission mode for Scout, a solar-electric robot
boat. Standard library only (Python 3.9+), so it runs on the boat's computer.
The one-page write-up is [WRITEUP.md](WRITEUP.md); the full report, with how
every figure was made, is [REPORT.md](REPORT.md).

## Quick start

```sh
git clone https://github.com/superdyoll/forecast.git && cd forecast
scripts/quickstart.sh          # installs into .venv, runs the simulation (20x; 5x on arm64),
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

## Install on the boat (Debian/Ubuntu, apt)

`scout-lowpower` reads the operator's mission (a local file: no shore link
needed), forecasts the battery for each way of operating and commands ArduPilot
over MAVLink. Its decisions and reasons go to journald and to the web page.

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
# /etc/scout/params.json is the 17-day fit; replace it if you have a newer one
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
| `scout-sim` | simulation only, never on the boat (see [Simulation](#simulation-ardupilot-sitl-and-web-page)) |

The packages pin each other to the same version. An upgrade restarts what was
running and keeps your `scout.env` edits; `apt purge` removes config and state.
Roll back by installing an older release's debs with `--allow-downgrades`.

| Unit | Runs | Notes |
| --- | --- | --- |
| `scout-lowpower.service` | the low-power mode | `Type=notify`, 30 s watchdog, `Restart=always`, never gives up; state in `/var/lib/scout-lowpower`; hardened (read-only system, no home, own user) |
| `scout-web.service` | the web page | reads the service's state dir; `127.0.0.1:8080` unless `SCOUT_WEB_HOST` says otherwise |
| `scout-sitl.service` | simulation: SITL in docker | restarting keeps the container, so SITL keeps its mission like a real autopilot |
| `scout-sim.service` | simulation: sea, sky, power | feeds `BATTERY_STATUS` to the service's power link |

## Releases

Every merge into `main` runs `.github/workflows/release.yml`. It bumps every
app's version together, runs the tests, builds the wheels and debs,
smoke-tests the debs under systemd (including an upgrade from the last
release), and only then tags and publishes a GitHub release. If `main` is
protected, add a `RELEASE_TOKEN` secret that may push to it.

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

### From a checkout (any OS with docker)

`scripts/quickstart.sh` (see [Quick start](#quick-start)), or run the pieces
directly with `SIM_RATE=20 apps/sim/run_sitl.sh` after installing.

### What runs

* **SITL:** ArduPilot 4.0 Rover (`radarku/ardupilot-sitl`, `sailboat-motor`
  hull) with `apps/sim/sitl/scout.parm`.
* **scout-sim:** current (mean plus tidal ellipse), gusty wind with hull
  leeway, and cloud, pushed into SITL as drift, and optionally a storm
  (`--storm 24:72`: days of overcast and gales). It also runs a power model
  that sends battery, load and solar to the service. Its "true" parameters are
  about 10 % off the fitted ones on purpose.
* **scout-sim fast:** the same environment, power and the real planner without
  SITL, for scenarios that last days: `scout-sim fast --days 5 --mission
  station --storm 24:72 --out sim-run/storm` writes a `summary.json` scoring
  every forecast.
* **scout-web:** the current decision and why, battery against earlier
  forecasts, forecast error by lead time, load and solar. It updates itself
  every few seconds (304 when nothing changed), keeps your choices and pauses
  in a background tab.

## Development

```sh
scripts/quickstart.sh --install-only    # .venv with every app, editable
scripts/quickstart.sh --test            # every app's unit tests
packaging/build.sh                      # dist/: wheels, debs and an apt index
packaging/test/smoke.sh                 # the debs under systemd in docker (~1 min)
```

The forecast commands (`forecast`, `scout-evaluate`, `scout-data`) need the
private telemetry CSV: pass `--csv` or set `SCOUT_TELEMETRY_CSV` (also read
from `.env`, see `.env.example`). The simulation doesn't need it.

Findings and accuracy: [REPORT.md](REPORT.md). Conventions: [AGENTS.md](AGENTS.md).
