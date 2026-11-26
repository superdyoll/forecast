#!/usr/bin/env bash
# Install and run the units under real systemd in a throwaway Debian container,
# talking to a SITL container on the host. For dev machines without systemd.
#
#   packaging/test/test-systemd.sh            # build image + wheels, install, start
#   docker exec -it scout-systemd journalctl -u scout-lowpower -f
#   docker rm -f scout-systemd                # clean up
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
DOCKER="${DOCKER:-docker}"
PYTHON="${PYTHON:-python3}"
work="$(mktemp -d)"
trap 'rm -rf -- "$work"' EXIT

"$DOCKER" build -q -t scout-systemd-test -f "$here/Dockerfile.systemd" "$here" >/dev/null
"$PYTHON" -m pip wheel --quiet --no-deps -w "$work/wheels" "$repo" "$repo/sim"
"$DOCKER" run -d --name scout-systemd --privileged --cgroupns=host \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock \
  -v "$repo:/src:ro" --add-host host.docker.internal:host-gateway \
  scout-systemd-test >/dev/null
"$DOCKER" cp "$work/wheels" scout-systemd:/wheels
sleep 3
params=()
if [ -n "${SCOUT_PARAMS:-}" ]; then
  "$DOCKER" cp "$SCOUT_PARAMS" scout-systemd:/params.json
  params=(--params /params.json)
fi
"$DOCKER" exec scout-systemd bash /src/packaging/install.sh --wheels /wheels --with-sim --no-start "${params[@]}"
# No docker inside: use the host's SITL container instead of scout-sitl.service.
"$DOCKER" exec scout-systemd bash -c '
  systemctl disable -q scout-sitl
  sed -i "s#^SCOUT_MAVLINK=.*#SCOUT_MAVLINK=tcp:host.docker.internal:5760#" /etc/scout/scout.env
  printf "SCOUT_SIM_SITL=tcp:host.docker.internal:5762\n" >> /etc/scout/scout.env
  scout-lowpower set-mission --transit 60.215,2.210 60.230,2.240 --reserve 30
  systemctl start scout-lowpower scout-web scout-sim
  sleep 5
  systemctl --no-pager --lines=0 status scout-lowpower scout-web scout-sim'
