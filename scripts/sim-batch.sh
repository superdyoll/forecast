#!/usr/bin/env bash
# Run a batch of SITL simulations one after another, sharing the time left
# before a deadline between them. Each run is apps/sim/run_sitl.sh with the
# settings on its line of the plan, in OUT/<name>/ (state, truth, logs).
#
#   scripts/sim-batch.sh apps/sim/batches/overnight.plan runs/overnight 06:50
#
# Plan lines: NAME [VAR=value ...] (run_sitl.sh settings); '#' comments.
# Settings for every run: SIM_RATE (default 5: what emulated SITL keeps up
# with), PYTHON (default .venv/bin/python). Writes OUT/batch.log and, per
# run, OUT/<name>/run.json (settings, real start and end). Score the runs
# afterwards with: scout-sim score OUT/*/
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
plan="${1:?usage: sim-batch.sh PLAN OUT DEADLINE(HH:MM)}"
out="${2:?usage: sim-batch.sh PLAN OUT DEADLINE(HH:MM)}"
until_hm="${3:?usage: sim-batch.sh PLAN OUT DEADLINE(HH:MM)}"
export SIM_RATE="${SIM_RATE:-5}" PYTHON="${PYTHON:-$repo/.venv/bin/python}"
if ! command -v docker >/dev/null && [ -x /Applications/Docker.app/Contents/Resources/bin/docker ]; then
  export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
fi
mkdir -p "$out"
out="$(cd "$out" && pwd)"
cp "$plan" "$out/plan.txt"
log() { echo "$(date '+%H:%M:%S') $*" | tee -a "$out/batch.log"; }

# The deadline today, or tomorrow if that time has passed.
deadline="$("$PYTHON" -c '
import sys, datetime as d
h, m = map(int, sys.argv[1].split(":")); now = d.datetime.now()
t = now.replace(hour=h, minute=m, second=0, microsecond=0)
print(int((t if t > now else t + d.timedelta(days=1)).timestamp()))' "$until_hm")"

runs=()  # bash 3.2 (macOS) has no mapfile
while IFS= read -r line; do runs+=("$line"); done < <(grep -vE '^[[:space:]]*(#|$)' "$plan")
log "batch: ${#runs[@]} runs at ${SIM_RATE}x until $until_hm -> $out"
for i in "${!runs[@]}"; do
  read -r -a words <<<"${runs[$i]}"
  name="${words[0]}"
  settings=()
  if [ "${#words[@]}" -gt 1 ]; then settings=("${words[@]:1}"); fi
  left=$(( ${#runs[@]} - i ))
  # Share what's left; 90 s per run for SITL to start and stop.
  secs=$(( (deadline - $(date +%s)) / left - 90 ))
  if [ "$secs" -lt 300 ]; then log "$name: skipped, ${secs}s is too short"; continue; fi
  work="$out/$name"
  rm -rf "${work:?}" && mkdir -p "$work"
  log "$name: $((secs / 60)) min (${settings[*]:-defaults})"
  start=$(date +%s)
  env ${settings[@]+"${settings[@]}"} SIM_WORK="$work" SIM_FRESH=1 "$repo/apps/sim/run_sitl.sh" \
    >"$work/run.log" 2>&1 &
  pid=$!
  # Wait out the run (or its early end), then stop it and SITL.
  end=$(( start + secs ))
  while kill -0 "$pid" 2>/dev/null && [ "$(date +%s)" -lt "$end" ]; do sleep 5; done
  kill "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  docker stop -t 5 scout-sitl >/dev/null 2>&1 || true
  "$PYTHON" -c 'import json, sys
name, rate, start, end, settings, path = sys.argv[1:]
json.dump({"name": name, "settings": settings.split(), "rate": float(rate),
           "real_start": int(start), "real_end": int(end)}, open(path, "w"), indent=1)' \
    "$name" "$SIM_RATE" "$start" "$(date +%s)" "${settings[*]:-}" "$work/run.json"
  log "$name: done ($(grep -c 'plan:' "$work/lowpower.log" 2>/dev/null || echo 0) decisions)"
done
log "batch: finished"
