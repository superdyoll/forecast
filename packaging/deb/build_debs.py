#!/usr/bin/env python3
"""Build Scout's .deb packages from its wheels, plus a flat apt repository.

    packaging/deb/build_debs.py --wheels dist/wheels --out dist/debs

Usually run by packaging/build.sh. Needs dpkg-deb (Debian/Ubuntu; on macOS
build.sh runs this in a Debian container). Standard library otherwise.

One deb per app, all at the version of the wheels, each pinned to the others
with ``(= version)`` so apt never mixes releases:

  scout-forecast  energy model + `forecast`, `scout-data`, `scout-evaluate`
  scout-lowpower  the boat service: unit, /etc/scout/scout.env, `scout` user
  scout-web       the web page and its unit
  scout-sim       simulation only (SITL in docker + scout-sim); never on the boat
  scout           metapackage for the boat: forecast + lowpower + web

The apps are pure Python and standard library only, so they're unpacked into
/usr/lib/python3/dist-packages (read by every Debian python3), not a venv.
The output directory is also an apt repository (Packages, Packages.gz,
Release), usable offline:

    echo "deb [trusted=yes] file:/media/usb/scout ./" \
        > /etc/apt/sources.list.d/scout.list
    apt update && apt install scout
"""

from __future__ import annotations

import argparse
import email.parser
import gzip
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKAGING = HERE.parent
ROOT = PACKAGING.parent
SITE = "usr/lib/python3/dist-packages"
UNITS = "usr/lib/systemd/system"
PY = "python3 (>= 3.9)"
MAINTAINER = os.environ.get("DEB_MAINTAINER", "Scout developers <scout@invalid>")

# --------------------------------------------------------------- maintainer
# Shell snippets, kept short. Every systemctl call is guarded so installing in
# a chroot or a container without systemd running still works.

ADD_USER = """\
if ! getent passwd scout >/dev/null; then
  adduser --system --quiet --group --no-create-home --home /nonexistent \\
    --gecos "Scout services" scout >/dev/null
fi
# Serial autopilot links (e.g. serial:/dev/ttyACM0:115200).
if getent group dialout >/dev/null && ! id -nG scout | grep -qw dialout; then
  adduser scout dialout >/dev/null
fi
"""

SIM_ENV = """\
if [ ! -e /etc/scout/sim.env ]; then
  mkdir -p /etc/scout
  {
    echo "# Created by the scout-sim package; removed when it is purged."
    echo "# Simulated clock shared by SITL, scout-sim and the services. To restart"
    echo "# it: scout-sim clock --start 2025-07-23T06:00Z [--rate 20], paste below."
    echo "SCOUT_CLOCK=$(/usr/bin/scout-sim clock --start 2025-07-23T06:00Z)"
    echo "SCOUT_SIM_SITL=tcp:127.0.0.1:5762"
    echo "SCOUT_SIM_POWER_OUT=udpout:127.0.0.1:14601"
  } > /etc/scout/sim.env
fi
"""

# Put the boat services on the simulated clock while scout-sim is installed.
SIM_DROPIN = """\
# Installed by scout-sim: run this service on the simulated clock.
[Service]
EnvironmentFile=-/etc/scout/sim.env
"""

# The operator's command: same config and state directory as the service, so
# `scout-lowpower status` or `sudo -u scout scout-lowpower set-mission ...` act
# on the running service. The unit runs it too (exec keeps the PID for
# Type=notify).
LOWPOWER_WRAPPER = """\
#!/bin/sh
# scout-lowpower: reads /etc/scout/*.env (scout.env, and sim.env in the
# simulation) like the systemd unit, and defaults to the service's state.
set -a
for f in /etc/scout/*.env; do [ -r "$f" ] && . "$f"; done
set +a
: "${SCOUT_STATE_DIR:=/var/lib/scout-lowpower}"
export SCOUT_STATE_DIR
exec /usr/bin/python3 -I -m scout_lowpower "$@"
"""

PACKAGES = {
    "scout-forecast": dict(
        wheel="scout_forecast",
        depends=[PY],
        description="Scout energy forecast: telemetry cleaning and energy model",
    ),
    "scout-lowpower": dict(
        wheel="scout_lowpower",
        depends=[PY, "scout-forecast (= {v})", "adduser"],
        description="Scout low-power mission mode: planner and ArduPilot service",
        units=["scout-lowpower.service"],
        files={"etc/scout/scout.env": PACKAGING / "scout.env.example"},
        conffiles=["/etc/scout/scout.env"],
        scripts={"scout-lowpower": LOWPOWER_WRAPPER},
        postinst=ADD_USER,
        purge="rm -rf /var/lib/scout-lowpower",
    ),
    "scout-web": dict(
        wheel="scout_web",
        depends=[PY, "scout-forecast (= {v})", "adduser"],
        recommends=["scout-lowpower (= {v})"],
        description="Scout forecast-against-actual web page",
        units=["scout-web.service"],
        postinst=ADD_USER,
    ),
    "scout-sim": dict(
        wheel="scout_sim",
        depends=[PY, "scout-forecast (= {v})", "scout-lowpower (= {v})"],
        recommends=["docker.io | docker-ce"],
        description="Scout simulation around ArduPilot SITL (not for the boat)",
        units=["scout-sitl.service", "scout-sim.service"],
        # SITL pulls and runs a docker image: only start it where docker exists.
        start_if="command -v docker >/dev/null",
        files={
            "usr/share/scout-sim/sitl-container.sh": PACKAGING / "sitl-container.sh",
            "usr/share/scout-sim/sitl/scout.parm": ROOT / "apps/sim/sitl/scout.parm",
            f"{UNITS}/scout-lowpower.service.d/scout-sim.conf": SIM_DROPIN,
            f"{UNITS}/scout-web.service.d/scout-sim.conf": SIM_DROPIN,
        },
        postinst=SIM_ENV,
        # The drop-ins change the boat services' environment: restart them.
        restart_others=["scout-lowpower.service", "scout-web.service"],
        purge="rm -f /etc/scout/sim.env\nrm -rf /var/lib/scout-sim",
    ),
    "scout": dict(
        depends=[
            "scout-forecast (= {v})",
            "scout-lowpower (= {v})",
            "scout-web (= {v})",
        ],
        description="Scout boat software: energy forecast, low-power mode, web page",
    ),
}


def maintainer_scripts(name: str, spec: dict) -> dict[str, str]:
    """postinst, prerm and postrm for one package (only those with work to do)."""
    units = " ".join(spec.get("units", []))
    others = " ".join(spec.get("restart_others", []))
    pkg = spec.get("wheel")
    site = "/" + SITE

    configure = []
    if pkg:
        configure.append(f"python3 -m compileall -q {site}/{pkg} >/dev/null || true\n")
    if spec.get("postinst"):
        configure.append(spec["postinst"])
    if units:
        start = spec.get("start_if", "true")
        configure.append(
            f"""\
[ -z "$have_systemd" ] || systemctl daemon-reload
if [ -z "$2" ]; then
  # First install: enable, and start now unless there's no systemd.
  systemctl enable {units} >/dev/null 2>&1 || true
  if [ -n "$have_systemd" ] && {start}; then systemctl start {units} || true; fi
elif [ -n "$have_systemd" ]; then
  # Upgrade: restart what was running; keep the admin's enable/disable.
  systemctl try-restart {units} || true
fi
"""
        )
    if others:
        configure.append(
            f'[ -z "$have_systemd" ] || systemctl try-restart {others} || true\n'
        )

    remove, prerm_all = [], []
    if units:  # prerm runs while the old package's files are still there
        remove.append(
            f"""\
[ -z "$have_systemd" ] || systemctl stop {units} || true
systemctl disable {units} >/dev/null 2>&1 || true
"""
        )
    if pkg:
        prerm_all.append(
            f"find {site}/{pkg} -name __pycache__ -type d -prune "
            "-exec rm -rf {} + 2>/dev/null || true\n"
        )

    postrm = []
    if units or others:
        postrm.append('[ -z "$have_systemd" ] || systemctl daemon-reload || true\n')
    if others:
        postrm.append(
            'if [ "$1" = remove ] && [ -n "$have_systemd" ]; then\n'
            f"  systemctl try-restart {others} || true\nfi\n"
        )
    if spec.get("purge"):
        postrm.append(f'if [ "$1" = purge ]; then\n{_indent(spec["purge"])}fi\n')

    def script(body: list[str]) -> str:
        text = "".join(body)
        detect = '[ -d /run/systemd/system ] && have_systemd=1 || have_systemd=""\n'
        if "have_systemd" not in text:
            detect = ""
        return "#!/bin/sh\nset -e\n" + detect + text + "exit 0\n"

    out = {}
    if configure:
        out["postinst"] = script(
            ['if [ "$1" = configure ]; then\n', _indent("".join(configure)), "fi\n"]
        )
    prerm = []
    if remove:
        prerm += ['if [ "$1" = remove ]; then\n', _indent("".join(remove)), "fi\n"]
    prerm += prerm_all
    if prerm:
        out["prerm"] = script(prerm)
    if postrm:
        out["postrm"] = script(postrm)
    return out


def _indent(s: str) -> str:
    return "".join(f"  {line}\n" if line else "\n" for line in s.splitlines())


# ------------------------------------------------------------------- wheels
def unpack_wheel(whl: Path, stage: Path) -> tuple[str, dict, str]:
    """Unpack a pure-Python wheel into stage/SITE. Returns version, console
    scripts {name: "module:func"} and the summary line."""
    site = stage / SITE
    site.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(whl) as z:
        names = z.namelist()
        if any(".data/" in n for n in names):
            raise SystemExit(f"{whl.name}: .data directories aren't supported")
        z.extractall(site)
    info = next(site.glob("*.dist-info"))
    meta = email.parser.Parser().parsestr((info / "METADATA").read_text())
    scripts = {}
    ep = info / "entry_points.txt"
    if ep.exists():
        section = None
        for line in ep.read_text().splitlines():
            line = line.strip()
            if line.startswith("["):
                section = line.strip("[]")
            elif section == "console_scripts" and "=" in line:
                k, v = (x.strip() for x in line.split("=", 1))
                scripts[k] = v
    # dpkg owns these files: pip must not try to uninstall or upgrade them.
    (info / "RECORD").unlink(missing_ok=True)
    (info / "INSTALLER").write_text("dpkg\n")
    return meta["Version"], scripts, meta.get("Summary", "")


def launcher(target: str) -> str:
    mod, func = target.split(":")
    return (
        "#!/usr/bin/python3 -I\n"
        "import sys\n\n"
        f"from {mod} import {func}\n\n"
        f"sys.exit({func}())\n"
    )


# -------------------------------------------------------------------- build
def build(name: str, spec: dict, wheels: dict, version: str, out: Path, tmp: Path):
    stage = tmp / name
    summary = ""
    if spec.get("wheel"):
        v, scripts, summary = unpack_wheel(wheels[spec["wheel"]], stage)
        assert v == version, (name, v, version)
        scripts = {k: launcher(t) for k, t in scripts.items()}
        scripts.update(spec.get("scripts", {}))
        for k, text in scripts.items():
            _put(stage / "usr/bin" / k, text, 0o755)
        readme = ROOT / "apps" / spec["wheel"].replace("scout_", "") / "README.md"
        if readme.exists():
            _put(stage / f"usr/share/doc/{name}/README.md", readme.read_text())
    for unit in spec.get("units", []):
        _put(stage / UNITS / unit, (PACKAGING / "systemd" / unit).read_text())
    for dest, src in spec.get("files", {}).items():
        text = src.read_text() if isinstance(src, Path) else src
        mode = 0o755 if text.startswith("#!") else 0o644
        _put(stage / dest, text, mode)

    for d, _, files in os.walk(stage):
        os.chmod(d, 0o755)
        for f in files:
            p = Path(d) / f
            if not p.stat().st_mode & 0o100:
                os.chmod(p, 0o644)

    size_kib = sum(f.stat().st_size for f in stage.rglob("*") if f.is_file()) // 1024
    control = {
        "Package": name,
        "Version": version,
        "Architecture": "all",
        "Maintainer": MAINTAINER,
        "Installed-Size": str(max(size_kib, 1)),
        "Section": "misc",
        "Priority": "optional",
        "Depends": ", ".join(d.format(v=version) for d in spec.get("depends", [])),
        "Recommends": ", ".join(
            d.format(v=version) for d in spec.get("recommends", [])
        ),
        "Description": spec["description"] + _long(summary),
    }
    deb = stage / "DEBIAN"
    deb.mkdir(parents=True, exist_ok=True)
    (deb / "control").write_text(
        "".join(f"{k}: {v}\n" for k, v in control.items() if v)
    )
    if spec.get("conffiles"):
        (deb / "conffiles").write_text("".join(f"{c}\n" for c in spec["conffiles"]))
    for script, text in maintainer_scripts(name, spec).items():
        _put(deb / script, text, 0o755)

    target = out / f"{name}_{version}_all.deb"
    subprocess.run(
        ["dpkg-deb", "--root-owner-group", "-Zxz", "--build", str(stage), str(target)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return target


def _long(summary: str) -> str:
    return f"\n {summary}" if summary else ""


def _put(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    os.chmod(path, mode)


# ---------------------------------------------------------------- apt repo
def apt_index(out: Path) -> None:
    """Packages, Packages.gz and an unsigned Release: a flat repository."""
    entries = []
    for deb in sorted(out.glob("*.deb")):
        control = subprocess.run(
            ["dpkg-deb", "-f", str(deb)], check=True, capture_output=True, text=True
        ).stdout.rstrip("\n")
        data = deb.read_bytes()
        entries.append(
            f"{control}\nFilename: ./{deb.name}\nSize: {len(data)}\n"
            f"MD5sum: {hashlib.md5(data).hexdigest()}\n"
            f"SHA256: {hashlib.sha256(data).hexdigest()}\n"
        )
    packages = "\n".join(entries).encode()
    (out / "Packages").write_bytes(packages)
    (out / "Packages.gz").write_bytes(gzip.compress(packages, mtime=0))
    date = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S UTC")
    rel = ["Origin: Scout", "Label: Scout", f"Date: {date}"]
    for field, algo in (("MD5Sum", hashlib.md5), ("SHA256", hashlib.sha256)):
        rel.append(f"{field}:")
        for f in ("Packages", "Packages.gz"):
            b = (out / f).read_bytes()
            rel.append(f" {algo(b).hexdigest()} {len(b)} {f}")
    (out / "Release").write_text("\n".join(rel) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--wheels", required=True, type=Path, help="directory of wheels")
    ap.add_argument("--out", required=True, type=Path, help="output directory")
    a = ap.parse_args(argv)
    if not shutil.which("dpkg-deb"):
        raise SystemExit("dpkg-deb not found (Debian/Ubuntu; see packaging/build.sh)")

    wheels, versions = {}, set()
    for w in sorted(a.wheels.glob("scout_*.whl")):
        m = re.match(r"(scout_[a-z]+)-([^-]+)-", w.name)
        if m:
            wheels[m.group(1)] = w
            versions.add(m.group(2))
    missing = {s["wheel"] for s in PACKAGES.values() if "wheel" in s} - set(wheels)
    if missing:
        raise SystemExit(f"missing wheels in {a.wheels}: {', '.join(sorted(missing))}")
    if len(versions) != 1:
        raise SystemExit(f"wheels have different versions: {sorted(versions)}")
    version = versions.pop()

    a.out.mkdir(parents=True, exist_ok=True)
    for old in a.out.glob("*.deb"):
        old.unlink()
    with tempfile.TemporaryDirectory() as tmp:
        for name, spec in PACKAGES.items():
            print(build(name, spec, wheels, version, a.out, Path(tmp)))
    apt_index(a.out)
    print(f"apt repository: {a.out} (Packages, Release)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
