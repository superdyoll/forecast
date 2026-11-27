---
name: install-and-run
description: Install this repository's apps and run the Scout simulation (ArduPilot SITL in docker + simulator + low-power service + web page), then give the user the page's URL. Use when the user asks to install, set up, run, start, launch, demo or "get it working", or to run the tests, in a fresh clone or otherwise.
---

# Install and run Scout

Everything goes through one script, `scripts/quickstart.sh`. Don't reinvent its
steps by hand. It creates `.venv`, installs every app editable, checks docker
and ports, pulls the SITL image on first run, starts the simulation at 20x and
prints `READY <url>` once the page is up and the service is planning.

## Run it

1. Start it in the background (it runs until stopped):
   `scripts/quickstart.sh` with `run_in_background: true`.
2. Wait for its `READY http://127.0.0.1:<port>` line, or for it to exit with
   `quickstart: ERROR: ...`. Don't sleep-poll; wait on the output with a
   short until-loop (`until grep -qE '^READY|ERROR' <output file>; do sleep 2; done`)
   run in the background. The first run pulls a large docker image: allow
   several minutes; later runs are ready in about a minute.
3. Give the user the URL. Say the clock runs 20x (about 4 simulated hours in
   12 minutes), the page updates itself every few seconds, and the
   forecast-against-actual chart fills in over the first ~10 minutes.
4. To stop: stop that background task. The `scout-sitl` container keeps
   running; `docker rm -f scout-sitl` removes it (only if the user asks).

Other requests:

- "Install" only: `scripts/quickstart.sh --install-only` (foreground, ~1 min).
- "Run the tests": `scripts/quickstart.sh --test` (foreground, ~1 min).
- Real time instead of 20x: `SIM_RATE=1 scripts/quickstart.sh`.
- Install as Linux system services instead (debs, apt, systemd): follow
  README.md, "Install on the boat" and "Simulation > From the debs". That needs
  sudo on a Debian/Ubuntu machine; ask before running sudo commands.

## When it fails

The script's `ERROR:` line says what's wrong. Common ones:

| Error | What to do |
| --- | --- |
| `docker isn't running` | Ask the user to start Docker Desktop (or `sudo systemctl start docker`), then rerun. Don't try to start Docker Desktop yourself. |
| `docker not found` | Tell the user to install Docker Desktop (macOS/Windows) or `docker.io` (Linux). |
| `port 8080 is in use` | Rerun with `SCOUT_WEB_PORT=8090`. If it's an earlier quickstart, stop that task instead. |
| `port 5760 ... in use` | Something else holds SITL's port; find it (`lsof -nP -iTCP:5760`) and ask before stopping it. |
| `Python 3.9 or newer is needed` | Rerun with `PYTHON=/path/to/python3.x`. |
| `exec format error` in `sim-run/*/sim.log` or SITL never answers | The SITL image is x86-64 only. On Linux arm64: `sudo apt install qemu-user-static binfmt-support`. Docker Desktop on Apple silicon emulates it already. |
| `the page or the first plan didn't appear` | Read the log tails it printed (`sim-run/<timestamp>/lowpower.log`, `sim.log`, `web.log`). |

Never commit or publish `*.csv`, `.env`, `BRIEF.md` or `DATA.md`; the
simulation doesn't need them (see AGENTS.md).
