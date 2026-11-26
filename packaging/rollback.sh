#!/usr/bin/env bash
# Roll back scout-forecast to an earlier installed release (Linux, root).
#
#   sudo packaging/rollback.sh --list        # releases, newest first; * = current
#   sudo packaging/rollback.sh               # back to the release before current
#   sudo packaging/rollback.sh --to 0.1.0-20261127T101500Z
#
# Only the code switches. Config (/etc/scout) and state (/var/lib/scout-lowpower:
# mission, learned factors, history) are kept; state files only gain optional
# fields between releases, so an older release reads a newer state.json.
set -euo pipefail
PREFIX="${SCOUT_PREFIX:-/opt/scout}"
die() { echo "rollback: $*" >&2; exit 1; }

[ -d "$PREFIX/releases" ] || die "no releases under $PREFIX"
current="$(basename "$(readlink "$PREFIX/current" 2>/dev/null)" 2>/dev/null || true)"
mapfile -t releases < <(ls -1 "$PREFIX/releases" | sort -r)   # names sort by time

if [ "${1:-}" = "--list" ]; then
  for r in "${releases[@]}"; do [ "$r" = "$current" ] && echo "* $r" || echo "  $r"; done
  exit 0
fi
[ "$(id -u)" = 0 ] || die "run as root (sudo)"

target=""
if [ "${1:-}" = "--to" ]; then
  target="${2:?--to needs a release name}"
else
  for i in "${!releases[@]}"; do
    if [ "${releases[$i]}" = "$current" ]; then target="${releases[$((i + 1))]:-}"; break; fi
  done
fi
[ -n "$target" ] || die "no earlier release than $current"
[ -x "$PREFIX/releases/$target/bin/scout-lowpower" ] || die "release $target is incomplete"

ln -sfn "$PREFIX/releases/$target" "$PREFIX/current.new"
mv -T "$PREFIX/current.new" "$PREFIX/current"
echo "current -> $target (was $current)"
# Restart what runs release code; not scout-sitl (the simulated autopilot).
mapfile -t active < <(systemctl list-units --plain --no-legend 'scout-*.service' \
  | awk '{print $1}' | grep -vx 'scout-sitl.service')
[ "${#active[@]}" = 0 ] || systemctl restart "${active[@]}"
systemctl --no-pager --lines=0 status scout-lowpower.service || true
