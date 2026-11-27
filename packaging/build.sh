#!/usr/bin/env bash
# Build every Scout wheel and deb, and a flat apt repository of the debs.
#
#   packaging/build.sh [OUT]          # default ./dist
#     OUT/wheels/*.whl                # scout_forecast, scout_lowpower, scout_web, scout_sim
#     OUT/debs/*.deb + Packages, Release   (apt repository, see packaging/deb/build_debs.py)
#
# Needs Python 3.9+ with pip >= 21.3 (PYTHON=...), and dpkg-deb. Without
# dpkg-deb (macOS) the debs are built in a Debian container
# (python:3.11-slim-bookworm; DOCKER=...).
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="${1:-$PWD/dist}"
mkdir -p "$out"
out="$(cd "$out" && pwd)"
PYTHON="${PYTHON:-python3}"
DOCKER="${DOCKER:-docker}"

"$PYTHON" "$repo/packaging/version.py" check
rm -rf "$out/wheels"
"$PYTHON" -m pip wheel --quiet --no-deps -w "$out/wheels" \
  "$repo/apps/forecast" "$repo/apps/lowpower" "$repo/apps/web" "$repo/apps/sim"
rm -rf "$repo"/apps/*/build   # setuptools leaves these behind

if command -v dpkg-deb >/dev/null; then
  "$PYTHON" "$repo/packaging/deb/build_debs.py" --wheels "$out/wheels" --out "$out/debs"
else
  echo "no dpkg-deb here: building the debs in a Debian container"
  "$DOCKER" run --rm -v "$repo:/src:ro" -v "$out:/out" -e DEB_MAINTAINER \
    python:3.11-slim-bookworm python3 /src/packaging/deb/build_debs.py --wheels /out/wheels --out /out/debs
fi
ls -1 "$out/debs"
