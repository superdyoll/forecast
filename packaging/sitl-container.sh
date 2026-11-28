#!/usr/bin/env bash
# SIMULATION ONLY. Run ArduPilot Rover SITL in docker in the foreground, for
# systemd (scout-sitl.service, from the scout-sim deb) and apps/sim/run_sitl.sh.
#
# * Pulls the SITL image on first run (large; progress is in the journal).
# * Runs SITL at the simulated clock's rate (the "xN" of $SCOUT_CLOCK from
#   /etc/scout/sim.env), so SITL, scout-sim and the services agree on time.
# * Re-uses the container if it exists, so a restart keeps SITL's stored
#   mission and parameters like a real autopilot. It is recreated when the
#   rate changes (the rate is fixed when a container is created).
set -euo pipefail
share="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
parm="${SCOUT_SITL_PARM_DIR:-$share/sitl}"  # holds scout.parm (apps/sim/run_sitl.sh sets it)
name=scout-sitl  # also in scout-sitl.service (ExecStop) and the deb's purge
image="${SCOUT_SITL_IMAGE:-radarku/ardupilot-sitl}"
home="${SCOUT_SITL_HOME:-60.2,2.2,0,0}"
rate="${SCOUT_CLOCK:-x1}"
rate="${rate##*x}"
case "$rate" in '' | *[!0-9.]*) rate=1 ;; esac

die() { echo "scout-sitl: $*" >&2; exit 1; }
# Can't work until someone changes the machine: exit 78 (EX_CONFIG), which
# scout-sitl.service doesn't restart on (RestartPreventExitStatus).
fatal() { echo "scout-sitl: $*" >&2; exit 78; }
command -v docker >/dev/null || fatal "docker is not installed (apt install docker.io)"
docker info >/dev/null 2>&1 || die "the docker daemon isn't running or reachable (systemctl start docker)"

if ! docker image inspect "$image" >/dev/null 2>&1; then
  echo "first run: pulling $image (linux/amd64); this can take a few minutes"
  docker pull --platform linux/amd64 "$image"
fi
# The image is x86-64 only. Elsewhere the kernel needs x86-64 emulation
# (qemu-user-binfmt or qemu-user-static registers it with binfmt_misc; see
# build_debs.py's scout-sim Recommends). Try it rather than guess
# from handler names: without it every start fails with "exec format error".
if [ "$(uname -m)" != x86_64 ] \
  && ! out="$(docker run --rm --platform linux/amd64 --entrypoint /bin/true "$image" 2>&1)" \
  && echo "$out" | grep -q "exec format error"; then
  fatal "this $(uname -m) machine can't run the x86-64 SITL image: no x86-64 emulation." \
    "Install it: sudo apt install qemu-user-binfmt on Debian 13 and Ubuntu 25.10 or later," \
    "sudo apt install qemu-user-static before them. Check that" \
    "/proc/sys/fs/binfmt_misc/qemu-x86_64 exists, then sudo systemctl restart scout-sitl." \
    "Emulated SITL manages about 5x real time at most."
fi

if docker container inspect "$name" >/dev/null 2>&1; then
  have="$(docker container inspect -f '{{ index .Config.Labels "scout.rate" }}' "$name" 2>/dev/null || true)"
  if [ "$have" != "$rate" ]; then
    echo "container $name runs at x${have:-?}, clock wants x$rate: recreating (SITL forgets its mission; the service re-uploads it)"
    docker rm -f "$name" >/dev/null
  else
    started=$SECONDS
    docker start -a "$name" && exit 0 || rc=$?
    # Killed after running a while (the "autopilot dies" test): let systemd
    # restart us. Failed at once (e.g. amd64 emulation glitch on arm64 hosts):
    # recreate it; SITL then loses its stored mission and the service re-uploads.
    [ $((SECONDS - started)) -gt 5 ] && exit "$rc"
    echo "container $name failed to start (rc $rc); recreating" >&2
    docker rm -f "$name" >/dev/null
  fi
fi
echo "starting SITL x$rate (MAVLink tcp:127.0.0.1:5760 service, :5762 simulator)"
exec docker run --name "$name" --label "scout.rate=$rate" --platform linux/amd64 \
  -p 127.0.0.1:5760:5760 -p 127.0.0.1:5762:5762 -p 127.0.0.1:5763:5763 \
  -v "$parm:/scout:ro" \
  --entrypoint /ardupilot/build/sitl/bin/ardurover "$image" \
  -S -I0 --model sailboat-motor --speedup "$rate" --home "$home" \
  --defaults /ardupilot/Tools/autotest/default_params/rover.parm,/ardupilot/Tools/autotest/default_params/sailboat-motor.parm,/scout/scout.parm
