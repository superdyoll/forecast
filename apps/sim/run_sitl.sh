#!/usr/bin/env bash
# Run the whole simulation on one machine:
#   ArduPilot Rover SITL (docker, radarku/ardupilot-sitl) <- scout-sim (sea, sky, power)
#   scout-lowpower (the boat service)  and  scout-web (http://127.0.0.1:8080)
#
#   apps/sim/run_sitl.sh                     # real time, July sun at 60 N
#   SIM_RATE=20 apps/sim/run_sitl.sh         # 20x: ~4 simulated hours in 12 minutes
#   SIM_MISSION=station apps/sim/run_sitl.sh # station-keeping instead of a transit
#
# Settings (environment or .env): SIM_START, SIM_RATE, SIM_WORK (output dir),
# SIM_MISSION (transit|station|keep), SIM_PCT0, SIM_FRESH (1: new SITL), DOCKER (for SIM_FRESH),
# PYTHON, SCOUT_PARAMS.
# Needs: pip install -r requirements-dev.txt (all apps, editable), and docker.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKER="${DOCKER:-docker}"
PYTHON="${PYTHON:-python3}"
SIM_START="${SIM_START:-2025-07-23T06:00Z}"
SIM_RATE="${SIM_RATE:-1}"
SIM_WORK="${SIM_WORK:-$PWD/sim-run}"
SIM_MISSION="${SIM_MISSION:-transit}"
SIM_PCT0="${SIM_PCT0:-70}"

mkdir -p "$SIM_WORK/state"
export SCOUT_STATE_DIR="$SIM_WORK/state"
SCOUT_CLOCK="$("$PYTHON" -m scout_sim.run clock --start "$SIM_START" --rate "$SIM_RATE")"
export SCOUT_CLOCK
echo "clock: $SCOUT_CLOCK   output: $SIM_WORK"

# --- Processes; stop them all on exit
pids=()
cleanup() { trap - INT TERM EXIT; kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup INT TERM EXIT

# --- ArduPilot SITL in docker, run by the same script as scout-sitl.service:
# Rover, motor-sailboat model (the 4.0 boat model with water current), at the
# rate of $SCOUT_CLOCK. It keeps the container between runs, like a real
# autopilot; SIM_FRESH=1 starts a new one (position and mission from an
# earlier run would contradict the new clock).
if [ "${SIM_FRESH:-0}" = 1 ]; then "$DOCKER" rm -f scout-sitl >/dev/null 2>&1 || true; fi
SCOUT_SITL_PARM_DIR="$here/sitl" "$here/../../packaging/sitl-container.sh" \
  >"$SIM_WORK/sitl.log" 2>&1 & pids+=($!)

# --- Demo mission (times are on the simulated clock)
case "$SIM_MISSION" in
  transit) "$PYTHON" -m scout_lowpower set-mission \
             --transit 60.215,2.210 60.230,2.240 60.240,2.200 --arrive-in 4 --reserve 30 ;;
  station) "$PYTHON" -m scout_lowpower set-mission \
             --station 60.205,2.205 --radius 50 --drift-radius 400 --reserve 35 ;;
  keep) ;;
esac

"$PYTHON" -m scout_sim.run run --arm --pct0 "$SIM_PCT0" --truth "$SIM_WORK/sim-truth.jsonl" \
  >"$SIM_WORK/sim.log" 2>&1 & pids+=($!)
"$PYTHON" -m scout_lowpower run --power-link udpin:0.0.0.0:14601 \
  >"$SIM_WORK/lowpower.log" 2>&1 & pids+=($!)
"$PYTHON" -m scout_web >"$SIM_WORK/web.log" 2>&1 & pids+=($!)
echo "running: sim, lowpower, web http://127.0.0.1:${SCOUT_WEB_PORT:-8080}"
echo "logs in $SIM_WORK/*.log   Ctrl-C to stop (the SITL container may keep running)"
tail -n +1 -F "$SIM_WORK/lowpower.log" & pids+=($!)
wait
