#!/usr/bin/env bash
# From a fresh clone to the simulation in your browser, in one command.
#
#   scripts/quickstart.sh                  # install into .venv, run the simulation (20x; 5x on arm64)
#   scripts/quickstart.sh --install-only   # just install (and check the install works)
#   scripts/quickstart.sh --test           # install, then run every app's unit tests
#   SIM_RATE=1 scripts/quickstart.sh       # real time instead of 20x
#   SCOUT_WEB_PORT=8090 scripts/quickstart.sh   # if 8080 is taken
#
# Running prints one line "READY http://127.0.0.1:8080" once the page is up and
# the service is planning; then it keeps running (logs follow) until Ctrl-C.
# The SITL container may keep running after that (docker rm -f scout-sitl).
#
# Needs: Python 3.9+, and docker running (Docker Desktop on macOS/Windows,
# docker.io on Linux). The first run pulls the ArduPilot SITL image (several
# GB unpacked). No telemetry CSV or .env is needed; they are private inputs.
# Exit codes: 0 ok, 1 failed (the message says why), 2 bad usage.
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
say() { echo "quickstart: $*"; }
die() { echo "quickstart: ERROR: $*" >&2; exit 1; }

mode=run
case "${1:-}" in
  "") ;;
  --install-only) mode=install ;;
  --test) mode="test" ;;
  -h | --help) sed -n '2,17p' "$0"; exit 0 ;;
  *) echo "usage: scripts/quickstart.sh [--install-only|--test]" >&2; exit 2 ;;
esac

# --- 1. Python 3.9+ and a venv with a current pip (macOS's system pip 21.2
# can't install these projects).
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || die "$PY not found: install Python 3.9 or newer"
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 9))' \
  || die "$PY is $("$PY" -V 2>&1); Python 3.9 or newer is needed (set PYTHON=...)"
if [ ! -x .venv/bin/python ]; then
  say "creating .venv with $("$PY" -V 2>&1)"
  "$PY" -m venv .venv || die "couldn't create a venv (Debian/Ubuntu: apt install python3-venv)"
fi
VPY="$repo/.venv/bin/python"
say "installing the apps into .venv (editable)"
"$VPY" -m pip install --quiet --disable-pip-version-check --upgrade pip
"$VPY" -m pip install --quiet --disable-pip-version-check -r requirements-dev.txt
"$VPY" -c 'import scout_forecast, scout_lowpower, scout_web, scout_sim' \
  || die "the apps don't import after installing"
for c in forecast scout-lowpower scout-web scout-sim; do
  .venv/bin/"$c" --help >/dev/null || die "$c doesn't run"
done
say "installed: $(.venv/bin/python packaging/version.py) (forecast, scout-lowpower, scout-web, scout-sim in .venv/bin)"

if [ "$mode" = install ]; then exit 0; fi
if [ "$mode" = test ]; then
  for d in apps/*/tests; do
    say "testing $d"
    "$VPY" -m unittest discover -s "$d" || die "tests failed in $d"
  done
  say "all tests passed"
  exit 0
fi

# --- 2. Docker, running. Docker Desktop's CLI isn't always on PATH on macOS.
if ! command -v docker >/dev/null && [ -x /Applications/Docker.app/Contents/Resources/bin/docker ]; then
  export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
fi
command -v docker >/dev/null \
  || die "docker not found: install Docker Desktop (macOS/Windows) or docker.io (Linux)"
docker info >/dev/null 2>&1 \
  || die "docker isn't running: start Docker Desktop (or: sudo systemctl start docker), then rerun"
export DOCKER=docker

# --- 3. Ports: the web page, and SITL's unless our own container holds them.
port_busy() { "$VPY" -c 'import socket, sys; s = socket.socket(); s.settimeout(0.5); sys.exit(s.connect_ex(("127.0.0.1", int(sys.argv[1]))) != 0)' "$1"; }
web_port="${SCOUT_WEB_PORT:-8080}"
export SCOUT_WEB_PORT="$web_port"
port_busy "$web_port" && die "port $web_port is in use (another scout-web?): stop it, or rerun with SCOUT_WEB_PORT=8090"
ours="$(docker ps --filter name='^scout-sitl$' --format '{{.Names}}')"
for p in 5760 5762; do
  if [ -z "$ours" ] && port_busy "$p"; then
    die "port $p (SITL MAVLink) is in use by something other than the scout-sitl container"
  fi
done

# --- 4. The SITL image: pull it now so the progress is visible.
image=radarku/ardupilot-sitl
if ! docker image inspect "$image" >/dev/null 2>&1; then
  say "first run: pulling $image (linux/amd64, several GB unpacked; a few minutes)"
  docker pull --platform linux/amd64 "$image" || die "couldn't pull $image (network? disk space?)"
fi

# --- 5. Run: SITL + scout-sim + scout-lowpower + scout-web, fresh output dir.
# SITL's image is x86-64: emulated on arm64 (Apple silicon) it manages only
# ~5.5x, and a faster clock makes the boat lag it (scout-sim warns).
if [ -z "${SIM_RATE:-}" ]; then
  case "$(uname -m)" in x86_64 | amd64) SIM_RATE=20 ;; *) SIM_RATE=5 ;; esac
fi
export SIM_RATE
export SIM_FRESH="${SIM_FRESH:-1}"  # new SITL each run: an old one has the boat elsewhere
export SIM_WORK="${SIM_WORK:-$repo/sim-run/$(date +%Y%m%dT%H%M%S)}"
export PYTHON="$VPY"
say "starting the simulation at ${SIM_RATE}x (output: $SIM_WORK)"
apps/sim/run_sitl.sh &
sim=$!
trap 'kill "$sim" 2>/dev/null || true; wait "$sim" 2>/dev/null || true' INT TERM EXIT

url="http://127.0.0.1:$web_port"
for _ in $(seq 240); do
  kill -0 "$sim" 2>/dev/null || die "the simulation stopped; see $SIM_WORK/*.log"
  if curl -fsS -o /dev/null "$url/" 2>/dev/null \
     && grep -q "plan:" "$SIM_WORK/lowpower.log" 2>/dev/null; then
    echo "READY $url  (simulated clock ${SIM_RATE}x; logs in $SIM_WORK; Ctrl-C stops)"
    wait "$sim"
    exit 0
  fi
  sleep 1
done
for f in "$SIM_WORK"/*.log; do echo "--- $f"; tail -n 15 "$f"; done >&2
die "the page or the first plan didn't appear within 4 minutes (logs above)"
