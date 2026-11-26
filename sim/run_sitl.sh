#!/usr/bin/env bash
# Run the whole simulation on one machine:
#   ArduPilot Rover SITL (docker, radarku/ardupilot-sitl) <- scout-sim (sea, sky, power)
#   scout-lowpower (the boat service)  and  scout-web (http://127.0.0.1:8080)
#
#   sim/run_sitl.sh                         # real time, July sun at 60 N
#   SIM_RATE=20 sim/run_sitl.sh             # 20x: ~4 simulated hours in 12 minutes
#   SIM_MISSION=station sim/run_sitl.sh     # station-keeping instead of a transit
#
# Settings (environment or .env): SIM_START, SIM_RATE, SIM_WORK (output dir),
# SIM_MISSION (transit|station|keep), SIM_PCT0, DOCKER, PYTHON, SCOUT_PARAMS.
# Needs: pip install -e . -e sim (or PYTHONPATH=src:sim/src), and docker.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKER="${DOCKER:-docker}"
PYTHON="${PYTHON:-python3}"
SIM_START="${SIM_START:-2025-07-23T06:00Z}"
SIM_RATE="${SIM_RATE:-1}"
SIM_WORK="${SIM_WORK:-$PWD/sim-run}"
SIM_MISSION="${SIM_MISSION:-transit}"
SIM_PCT0="${SIM_PCT0:-70}"
CONTAINER="${SIM_CONTAINER:-scout-sitl}"

mkdir -p "$SIM_WORK/state"
export SCOUT_STATE_DIR="$SIM_WORK/state"
export SCOUT_CLOCK="$("$PYTHON" -m scout_sim.run clock --start "$SIM_START" --rate "$SIM_RATE")"
echo "clock: $SCOUT_CLOCK   output: $SIM_WORK"

# --- ArduPilot SITL: Rover, motor-sailboat model (the 4.0 boat model with water current)
if "$DOCKER" container inspect "$CONTAINER" >/dev/null 2>&1 \
   && "$DOCKER" start "$CONTAINER" >/dev/null && sleep 2 \
   && [ "$("$DOCKER" container inspect -f '{{.State.Running}}' "$CONTAINER")" = true ]; then
  :
else
  # Missing, or won't restart (seen with amd64 emulation on arm64): recreate.
  "$DOCKER" container inspect "$CONTAINER" >/dev/null 2>&1 && "$DOCKER" rm -f "$CONTAINER" >/dev/null
  "$DOCKER" run -d --name "$CONTAINER" --platform linux/amd64 \
    -p 5760:5760 -p 5762:5762 -p 5763:5763 -v "$here/sitl:/scout:ro" \
    --entrypoint /ardupilot/build/sitl/bin/ardurover radarku/ardupilot-sitl \
    -S -I0 --model sailboat-motor --speedup "$SIM_RATE" --home 60.2,2.2,0,0 \
    --defaults /ardupilot/Tools/autotest/default_params/rover.parm,/ardupilot/Tools/autotest/default_params/sailboat-motor.parm,/scout/scout.parm \
    >/dev/null
fi
echo "SITL container $CONTAINER running (MAVLink tcp:5760 service, tcp:5762 sim)"

# --- Demo mission (times are on the simulated clock)
arrive="$("$PYTHON" -c "from datetime import timedelta; from scout_forecast import clock; print((clock.now()+timedelta(hours=4)).strftime('%Y-%m-%dT%H:%MZ'))")"
case "$SIM_MISSION" in
  transit) "$PYTHON" -m scout_forecast.lowpower.service set-mission \
             --transit 60.215,2.210 60.230,2.240 60.240,2.200 --arrive-by "$arrive" --reserve 30 ;;
  station) "$PYTHON" -m scout_forecast.lowpower.service set-mission \
             --station 60.205,2.205 --radius 50 --drift-radius 400 --reserve 35 ;;
  keep) ;;
esac

# --- Processes; stop them all on exit
pids=()
cleanup() { trap - INT TERM EXIT; kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup INT TERM EXIT

interval="$("$PYTHON" -c "print(max(15, 300 / float('$SIM_RATE')))")"
"$PYTHON" -m scout_sim.run run --arm --pct0 "$SIM_PCT0" --truth "$SIM_WORK/sim-truth.jsonl" \
  >"$SIM_WORK/sim.log" 2>&1 & pids+=($!)
"$PYTHON" -m scout_forecast.lowpower.service run --power-link udpin:0.0.0.0:14601 \
  --interval-s "$interval" >"$SIM_WORK/lowpower.log" 2>&1 & pids+=($!)
"$PYTHON" -m scout_forecast.web >"$SIM_WORK/web.log" 2>&1 & pids+=($!)
echo "running: sim, lowpower (decides every ${interval}s), web http://127.0.0.1:${SCOUT_WEB_PORT:-8080}"
echo "logs in $SIM_WORK/*.log   Ctrl-C to stop (SITL container keeps running)"
tail -n +1 -F "$SIM_WORK/lowpower.log" & pids+=($!)
wait
