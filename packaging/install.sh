#!/usr/bin/env bash
# Install scout-forecast as systemd services (Linux, run as root).
#
#   sudo packaging/install.sh                        # build from this checkout and install
#   sudo packaging/install.sh --wheels dist/         # install pre-built wheels (no network)
#   sudo packaging/install.sh --params runs/physical/<ts>/params.json
#   sudo packaging/install.sh --with-sim             # also SITL (docker) + scout-sim
#   sudo packaging/install.sh --no-start             # install and enable, don't (re)start
#
# Layout (every install is a new release; nothing is overwritten in place):
#   /opt/scout/releases/<version>-<UTC timestamp>/   a self-contained venv
#   /opt/scout/current -> releases/...                switched atomically
#   /etc/scout/scout.env, /etc/scout/params.json     config (kept across releases)
#   /var/lib/scout-lowpower                           service state (kept)
#   /etc/systemd/system/scout-*.service
#   /usr/local/bin/scout-lowpower                     operator wrapper (set-mission, status)
# Roll back with packaging/rollback.sh.
#
# Override locations with SCOUT_PREFIX, SCOUT_ETC, SCOUT_UNITDIR, PYTHON.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(dirname "$here")"
PREFIX="${SCOUT_PREFIX:-/opt/scout}"
ETC="${SCOUT_ETC:-/etc/scout}"
UNITDIR="${SCOUT_UNITDIR:-/etc/systemd/system}"
PYTHON="${PYTHON:-python3}"
SIM_START="${SIM_START:-2025-07-23T06:00Z}"
wheels="" params="" with_sim=0 start=1

die() { echo "install: $*" >&2; exit 1; }
while [ $# -gt 0 ]; do
  case "$1" in
    --wheels) wheels="$2"; shift 2 ;;
    --params) params="$2"; shift 2 ;;
    --with-sim) with_sim=1; shift ;;
    --no-start) start=0; shift ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *) die "unknown option $1" ;;
  esac
done

[ "$(id -u)" = 0 ] || die "run as root (sudo)"
command -v systemctl >/dev/null || die "systemd not found"
"$PYTHON" -c 'import sys; assert sys.version_info >= (3, 9)' 2>/dev/null \
  || die "$PYTHON must be Python 3.9+"
"$PYTHON" -m venv --help >/dev/null 2>&1 || die "python venv missing (apt install python3-venv)"
[ -z "$params" ] || [ -f "$params" ] || die "no such params file: $params"

# --- 1. wheels: pre-built (boat, no network) or built from this checkout
tmp="$(mktemp -d)"
trap 'rm -rf -- "$tmp"' EXIT
if [ -n "$wheels" ]; then
  cp "$wheels"/*.whl "$tmp"/ || die "no wheels in $wheels"
else
  echo "building wheels from $repo"
  "$PYTHON" -m pip wheel --quiet --no-deps -w "$tmp" "$repo"
  [ "$with_sim" = 0 ] || "$PYTHON" -m pip wheel --quiet --no-deps -w "$tmp" "$repo/sim"
fi
whl="$(ls "$tmp"/scout_forecast-*.whl | head -n1)" || die "no scout_forecast wheel"
version="$(basename "$whl" | cut -d- -f2)"

# --- 2. a new, self-contained release
release="$PREFIX/releases/$version-$(date -u +%Y%m%dT%H%M%SZ)"
echo "installing release $release"
mkdir -p "$PREFIX/releases"
"$PYTHON" -m venv "$release"
pkgs=("$whl")
[ "$with_sim" = 0 ] || pkgs+=("$(ls "$tmp"/scout_sim-*.whl | head -n1)")
"$release/bin/pip" install --quiet --no-index --find-links "$tmp" "${pkgs[@]}"
install -d "$release/share/scout/sitl"
install -m 0644 "$repo/README.md" "$release/share/scout/README.md"
install -m 0755 "$here/sitl-container.sh" "$release/share/scout/sitl-container.sh"
install -m 0644 "$repo/sim/sitl/scout.parm" "$release/share/scout/sitl/scout.parm"
"$release/bin/scout-lowpower" --help >/dev/null || die "new release doesn't run; current unchanged"

# --- 3. user, config, wrapper
id scout >/dev/null 2>&1 || useradd --system --user-group --home-dir /var/lib/scout-lowpower \
  --shell /usr/sbin/nologin scout
getent group dialout >/dev/null && usermod -aG dialout scout   # serial autopilot links
install -d -m 0755 "$ETC"
[ -f "$ETC/scout.env" ] || install -m 0644 "$here/scout.env.example" "$ETC/scout.env"
[ -z "$params" ] || install -m 0644 "$params" "$ETC/params.json"
if [ "$with_sim" = 1 ] && ! grep -q '^SCOUT_CLOCK=' "$ETC/scout.env"; then
  echo "SCOUT_CLOCK=$("$release/bin/scout-sim" clock --start "$SIM_START")" >> "$ETC/scout.env"
fi
cat > /usr/local/bin/scout-lowpower <<EOF
#!/bin/sh
# Operator wrapper: same config and state as the service.
set -a; [ -f "$ETC/scout.env" ] && . "$ETC/scout.env"; set +a
export SCOUT_STATE_DIR="\${SCOUT_STATE_DIR:-/var/lib/scout-lowpower}"
exec "$PREFIX/current/bin/scout-lowpower" "\$@"
EOF
chmod 0755 /usr/local/bin/scout-lowpower

# --- 4. units (templated with this install's paths)
units=(scout-lowpower.service scout-web.service)
[ "$with_sim" = 0 ] || units+=(scout-sitl.service scout-sim.service)
for u in "${units[@]}"; do
  sed -e "s#@PREFIX@#$PREFIX#g" -e "s#@ETC@#$ETC#g" "$here/systemd/$u" > "$UNITDIR/$u"
  chmod 0644 "$UNITDIR/$u"
done
systemctl daemon-reload

# --- 5. switch to the new release atomically, then (re)start
previous="$(readlink "$PREFIX/current" 2>/dev/null || true)"
ln -sfn "$release" "$PREFIX/current.new"
mv -T "$PREFIX/current.new" "$PREFIX/current"
echo "current -> $release${previous:+ (was $previous)}"
systemctl enable "${units[@]}" >/dev/null
if [ "$start" = 1 ]; then
  systemctl restart "${units[@]}"
  sleep 3
  systemctl --no-pager --lines=0 status "${units[@]}" || true
fi
echo "done. Mission: scout-lowpower set-mission ...   Logs: journalctl -u scout-lowpower -f"
