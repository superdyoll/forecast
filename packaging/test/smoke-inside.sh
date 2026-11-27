#!/usr/bin/env bash
# Runs inside the systemd container started by smoke.sh. The apt repository
# (packaging/build.sh output) is at /repo, the checkout at /src (read-only).
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
step() { echo "--- $*"; }
fail() {
  echo "SMOKE FAIL: $*" >&2
  journalctl --no-pager -n 60 -u 'scout-*' -u fake-rover >&2 || true
  exit 1
}
wait_for() { # SECONDS COMMAND...: retry once a second
  local n="$1"; shift
  for _ in $(seq "$n"); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done
  return 1
}
journal_has() { journalctl --no-pager -o cat -u "$1" | grep -q "$2"; }
apt_quiet() { apt-get "$@" -y -qq -o Dpkg::Use-Pty=0 >/dev/null; }

use_repo() { echo "deb [trusted=yes] file:$1 ./" > /etc/apt/sources.list.d/scout.list; apt-get update -qq; }

if [ -f /previous/Packages ]; then
  step "install the previous release, edit its config"
  use_repo /previous
  apt_quiet install scout
  echo "previous: scout $(dpkg-query -W -f='${Version}' scout)"
  echo "SCOUT_SMOKE_EDIT=kept" >> /etc/scout/scout.env
  wait_for 20 systemctl is-active -q scout-lowpower || fail "previous release not running"
  old_pid="$(systemctl show -p MainPID --value scout-lowpower)"
  step "upgrade to this build with apt"
  use_repo /repo
  apt_quiet install -o Dpkg::Options::=--force-confold scout
  grep -q SCOUT_SMOKE_EDIT=kept /etc/scout/scout.env || fail "upgrade lost the config edit"
  wait_for 20 sh -c "[ \"\$(systemctl show -p MainPID --value scout-lowpower)\" != $old_pid ]" \
    || fail "service not restarted by the upgrade"
else
  step "install the boat packages from the apt repository"
  use_repo /repo
  apt_quiet install scout
fi
version="$(dpkg-query -W -f='${Version}' scout)"
for p in scout-forecast scout-lowpower scout-web; do
  [ "$(dpkg-query -W -f='${Version}' "$p")" = "$version" ] || fail "$p is not at $version"
done
[ -z "${EXPECT_VERSION:-}" ] || [ "$version" = "$EXPECT_VERSION" ] \
  || fail "installed $version, expected $EXPECT_VERSION"
echo "scout $version"

step "commands and modules"
for c in forecast scout-data scout-evaluate scout-lowpower scout-web; do
  "$c" --help >/dev/null || fail "$c --help"
done
python3 -I -c 'import scout_forecast.energy, scout_lowpower.service, scout_web.server' \
  || fail "import"
getent passwd scout >/dev/null || fail "no scout user"
[ -f /etc/scout/scout.env ] || fail "no /etc/scout/scout.env"
python3 -c 'import json; assert json.load(open("/etc/scout/params.json"))["capacity_wh"] > 0' \
  || fail "no fitted /etc/scout/params.json"

step "services were enabled and started by the package"
wait_for 20 systemctl is-active -q scout-lowpower scout-web || fail "services not active"
! journalctl -u scout-lowpower --no-pager -o cat | grep -q "can't read energy params" \
  || fail "service didn't load /etc/scout/params.json"
systemctl is-enabled -q scout-lowpower scout-web || fail "services not enabled"
wait_for 20 curl -fsS -o /dev/null http://127.0.0.1:8080/ || fail "web page"
curl -fsS http://127.0.0.1:8080/api/live | python3 -c 'import json, sys; json.load(sys.stdin)' \
  || fail "/api/live is not JSON"
unchanged_is_304() { # the data may change between the two requests: retried
  local tag
  tag="$(curl -fsS -o /dev/null -D - http://127.0.0.1:8080/api/live | tr -d '\r' | sed -n 's/^ETag: //Ip')"
  [ -n "$tag" ] && [ "$(curl -s -o /dev/null -w '%{http_code}' -H "If-None-Match: $tag" \
    http://127.0.0.1:8080/api/live)" = 304 ]
}
wait_for 5 unchanged_is_304 || fail "/api/live doesn't answer 304 when unchanged"

step "the service drives an autopilot (fake rover on tcp:5760)"
# 24.5 V = 90 %: the service plans on the real clock (a winter night has no sun
# for its 48 h look-ahead), and this step is about driving the autopilot.
systemd-run --quiet --unit fake-rover python3 /src/apps/lowpower/tests/fake_autopilot.py 5760 24.5
runuser -u scout -- scout-lowpower set-mission --transit 60.30,2.20 60.50,2.20 --reserve 30 \
  --arrive-in 48 >/dev/null || fail "set-mission"
grep -q '"arrive_by": "20' /var/lib/scout-lowpower/mission.json || fail "--arrive-in"
wait_for 90 journal_has scout-lowpower "mission uploaded" || fail "mission not uploaded"
wait_for 60 journal_has fake-rover "mode=10 " || fail "autopilot not put in AUTO"
wait_for 30 sh -c 'scout-lowpower status | grep -q "\"mode\": \"AUTO\""' \
  || fail "status doesn't show AUTO"

step "systemd restarts the service after kill -9"
pid="$(systemctl show -p MainPID --value scout-lowpower)"
systemctl kill -s KILL scout-lowpower
wait_for 30 sh -c "[ \"\$(systemctl show -p MainPID --value scout-lowpower)\" != 0 ] \
  && [ \"\$(systemctl show -p MainPID --value scout-lowpower)\" != $pid ] \
  && systemctl is-active -q scout-lowpower" || fail "not restarted"

step "scout-sim needs docker; its SITL script says so when it's missing"
apt-get install -s scout-sim | grep -q '^Inst docker.io' || fail "scout-sim doesn't pull in docker"
out="$(cd /tmp && apt-get download -qq scout-sim >/dev/null && dpkg-deb -x scout-sim_*.deb /tmp/sim \
  && bash /tmp/sim/usr/share/scout-sim/sitl-container.sh 2>&1 || true)"
echo "$out" | grep -q "docker is not installed" || fail "sitl-container.sh: no clear docker error: $out"
rm -rf /tmp/sim /tmp/scout-sim_*.deb

step "scout-sim installs next to it (stand-in docker.io: enabled, not started)"
# Real docker can't run in this container; a stand-in satisfies the dependency.
mkdir -p /tmp/fake-docker/DEBIAN
printf 'Package: docker.io\nVersion: 0~smoke\nArchitecture: all\nMaintainer: smoke <smoke@invalid>\nDescription: stand-in for the smoke test\n' \
  > /tmp/fake-docker/DEBIAN/control
dpkg-deb -b /tmp/fake-docker /tmp/fake-docker.deb >/dev/null && dpkg -i /tmp/fake-docker.deb >/dev/null
apt_quiet install --no-install-recommends scout-sim
[ -f /etc/scout/sim.env ] || fail "no /etc/scout/sim.env"
systemctl show -p DropInPaths scout-lowpower | grep -q scout-sim.conf || fail "no drop-in"
systemctl is-enabled -q scout-sim scout-sitl || fail "sim units not enabled"
wait_for 20 systemctl is-active -q scout-lowpower || fail "lowpower down after sim install"
apt_quiet purge scout-sim
[ ! -e /etc/scout/sim.env ] || fail "sim.env left after purge"
! systemctl show -p DropInPaths scout-lowpower | grep -q scout-sim.conf || fail "drop-in left"
wait_for 20 systemctl is-active -q scout-lowpower || fail "lowpower down after sim purge"

step "purge removes everything"
systemctl stop fake-rover
apt_quiet purge scout scout-forecast scout-lowpower scout-web
! dpkg -l 'scout*' 2>/dev/null | grep -q '^[ih]i' || fail "packages left"
for f in /usr/lib/systemd/system/scout-lowpower.service /var/lib/scout-lowpower /etc/scout \
         /usr/bin/scout-lowpower; do
  [ ! -e "$f" ] || fail "$f left after purge"
done
for f in /usr/lib/python3/dist-packages/scout*; do [ ! -e "$f" ] || fail "$f left after purge"; done
echo "SMOKE OK: scout $version"
