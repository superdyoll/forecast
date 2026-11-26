#!/usr/bin/env bash
# SIMULATION ONLY. Run ArduPilot Rover SITL in docker in the foreground, for
# systemd (scout-sitl.service). Re-uses the container if it exists, so a
# restart keeps SITL's stored mission and parameters like a real autopilot.
set -euo pipefail
share="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
name="${SCOUT_SITL_CONTAINER:-scout-sitl}"
image="${SCOUT_SITL_IMAGE:-radarku/ardupilot-sitl}"
home="${SCOUT_SITL_HOME:-60.2,2.2,0,0}"

if docker container inspect "$name" >/dev/null 2>&1; then
  started=$SECONDS
  docker start -a "$name" && exit 0 || rc=$?
  # Killed after running a while (the "autopilot dies" test): let systemd
  # restart us. Failed at once (e.g. amd64 emulation glitch on arm64 hosts):
  # recreate it; SITL then loses its stored mission and the service re-uploads.
  [ $((SECONDS - started)) -gt 5 ] && exit "$rc"
  echo "container $name failed to start (rc $rc); recreating" >&2
  docker rm -f "$name" >/dev/null
fi
exec docker run --name "$name" --platform linux/amd64 \
  -p 127.0.0.1:5760:5760 -p 127.0.0.1:5762:5762 -p 127.0.0.1:5763:5763 \
  -v "$share/sitl:/scout:ro" \
  --entrypoint /ardupilot/build/sitl/bin/ardurover "$image" \
  -S -I0 --model sailboat-motor --speedup 1 --home "$home" \
  --defaults /ardupilot/Tools/autotest/default_params/rover.parm,/ardupilot/Tools/autotest/default_params/sailboat-motor.parm,/scout/scout.parm
