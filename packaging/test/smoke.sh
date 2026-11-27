#!/usr/bin/env bash
# Smoke-test the debs under real systemd in a throwaway Debian container:
# install from the apt repository, drive a fake autopilot, survive kill -9,
# add and purge scout-sim, purge everything. About two minutes. CI runs it.
#
#   packaging/build.sh && packaging/test/smoke.sh [DEBS_DIR]   # default dist/debs
#   KEEP=1 packaging/test/smoke.sh      # leave the container (scout-smoke) running
#   EXPECT_VERSION=0.1.5 ...            # also check the installed version
#   PREVIOUS=dir ...                    # first install that older apt repository,
#                                       # then upgrade to DEBS_DIR
#
# Needs docker and network (Debian's python3 and adduser come from apt).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
debs="$(cd "${1:-$repo/dist/debs}" && pwd)"
DOCKER="${DOCKER:-docker}"
name="${SMOKE_CONTAINER:-scout-smoke}"
[ -f "$debs/Packages" ] || { echo "no apt repository in $debs (run packaging/build.sh)" >&2; exit 1; }
previous=()
if [ -n "${PREVIOUS:-}" ]; then
  previous=(-v "$(cd "$PREVIOUS" && pwd):/previous:ro")
fi

"$DOCKER" build -q -t scout-systemd-test -f "$here/Dockerfile.systemd" "$here" >/dev/null
"$DOCKER" rm -f "$name" >/dev/null 2>&1 || true
"$DOCKER" run -d --name "$name" --privileged --cgroupns=host \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock \
  -v "$repo:/src:ro" -v "$debs:/repo:ro" "${previous[@]}" --add-host host.docker.internal:host-gateway \
  scout-systemd-test >/dev/null
[ "${KEEP:-0}" = 1 ] || trap '"$DOCKER" rm -f "$name" >/dev/null' EXIT

for _ in $(seq 30); do
  state="$("$DOCKER" exec "$name" systemctl is-system-running 2>/dev/null || true)"
  case "$state" in running|degraded) break ;; esac
  sleep 1
done
"$DOCKER" exec -e EXPECT_VERSION="${EXPECT_VERSION:-}" "$name" \
  bash /src/packaging/test/smoke-inside.sh
