"""scout-web: the forecast against what happened, in a browser.

    scout-web                              # http://127.0.0.1:8080
    scout-web --host 0.0.0.0 --port 8080   # expose on the network (no auth!)

Standard library only and no external assets, so it works on the boat with
no internet. It reads what the low-power service writes to its state
directory (decision.json, history.jsonl): every decision records the battery
it saw and the forecast it made, so each forecast can be scored against the
readings that followed. If a telemetry CSV is configured (--csv,
SCOUT_TELEMETRY_CSV or .env) it also forecasts historical periods on request.

Endpoints: /  /api/live?hours=48  /api/hindcast?from=ISO&hours=24&plan=actual
/api/config
"""

from __future__ import annotations

import argparse
import bisect
import json
import logging
import os
import sys
import threading
from datetime import timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import energy as E
from .data import CSV_ENV, csv_default, load_dotenv
from .forecast import hindcast, parse_when

log = logging.getLogger("scout_web")
LEADS_H = (1, 3, 6, 12, 24)
MATCH_S = 600  # an actual reading within 10 min of a forecast time counts


def read_history(state_dir: Path, hours: float | None = None) -> list[dict]:
    rows = []
    for name in ("history.jsonl.1", "history.jsonl"):  # rotated file first
        p = state_dir / name
        if not p.exists():
            continue
        with open(p) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    r["_t"] = parse_when(r["time"])
                    rows.append(r)
                except (ValueError, KeyError):
                    continue  # a half-written last line, or junk
    rows.sort(key=lambda r: r["_t"])
    if hours and rows:
        cutoff = rows[-1]["_t"] - timedelta(hours=hours)
        rows = [r for r in rows if r["_t"] >= cutoff]
    return rows


def score(rows: list[dict]) -> dict:
    """MAE and bias of past forecasts at each lead time, against the readings
    that followed, with 'battery stays the same' as the baseline."""
    times = [r["_t"] for r in rows]

    def actual_at(t):
        k = bisect.bisect_left(times, t)
        best = None
        for j in (k - 1, k):
            if 0 <= j < len(rows):
                d = abs((times[j] - t).total_seconds())
                if d <= MATCH_S and (best is None or d < best[0]):
                    best = (d, rows[j]["battery_pct"])
        return None if best is None else best[1]

    acc = {h: {"err": [], "base": []} for h in LEADS_H}
    for r in rows:
        made = r["_t"]
        for ts, pct in r.get("forecast") or []:
            t = parse_when(ts)
            lead = (t - made).total_seconds() / 3600
            h = round(lead)
            if h not in acc or abs(lead - h) > 0.01:
                continue
            a = actual_at(t)
            if a is None:
                continue
            acc[h]["err"].append(pct - a)
            acc[h]["base"].append(r["battery_pct"] - a)
    out = {}
    for h, v in acc.items():
        e, b = v["err"], v["base"]
        out[f"{h}h"] = {
            "n": len(e),
            "mae": sum(map(abs, e)) / len(e) if e else None,
            "bias": sum(e) / len(e) if e else None,
            "mae_persistence": sum(map(abs, b)) / len(b) if b else None,
        }
    return out


def live(state_dir: Path, hours: float) -> dict:
    try:
        decision = json.loads((state_dir / "decision.json").read_text())
    except (OSError, ValueError):
        decision = None
    rows = read_history(state_dir, hours)
    errors = score(read_history(state_dir))  # score over everything kept
    # Every forecast is kept for comparison, thinned to one per 30 min.
    forecasts, last = [], None
    for r in rows:
        if r.get("forecast") and (
            last is None or r["_t"] - last >= timedelta(minutes=30)
        ):
            forecasts.append(
                {
                    "made": r["time"],
                    "battery_pct": r["battery_pct"],
                    "points": r["forecast"],
                }
            )
            last = r["_t"]
    if (
        rows
        and rows[-1].get("forecast")
        and (not forecasts or forecasts[-1]["made"] != rows[-1]["time"])
    ):
        forecasts.append(
            {
                "made": rows[-1]["time"],
                "battery_pct": rows[-1]["battery_pct"],
                "points": rows[-1]["forecast"],
            }
        )
    keep = (
        "time",
        "battery_pct",
        "load_w",
        "solar_w",
        "sog",
        "mode",
        "action",
        "speed_ms",
        "min_pct",
    )
    return {
        "decision": decision,
        "history": [{k: r.get(k) for k in keep} for r in rows],
        "forecasts": forecasts,
        "errors": errors,
    }


class App:
    def __init__(self, state_dir: Path, csv: str | None):
        self.state_dir, self.csv = state_dir, csv
        self._grid = None
        self._lock = threading.Lock()

    def grid(self):
        with self._lock:
            if self._grid is None:
                self._grid = E.build_grid(self.csv)
            return self._grid


def make_handler(app: App):
    page = files("scout_forecast").joinpath("static/index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "scout-web"

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=HTTPStatus.OK):
            self._send(code, json.dumps(obj, default=str).encode(), "application/json")

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path in ("/", "/index.html"):
                    self._send(HTTPStatus.OK, page, "text/html; charset=utf-8")
                elif u.path == "/api/config":
                    g = app.grid() if app.csv else None
                    self._json(
                        {
                            "hindcast": bool(app.csv),
                            "data_from": g and g["times"][0].isoformat(),
                            "data_to": g and g["times"][-1].isoformat(),
                        }
                    )
                elif u.path == "/api/live":
                    hours = min(max(float(q.get("hours", 48)), 1), 24 * 14)
                    self._json(live(app.state_dir, hours))
                elif u.path == "/api/hindcast":
                    if not app.csv:
                        return self._json(
                            {"error": f"no telemetry CSV configured (${CSV_ENV})"},
                            HTTPStatus.NOT_FOUND,
                        )
                    hours = min(max(float(q.get("hours", 24)), 1), 96)
                    plan = q.get("plan", "actual")
                    if plan not in ("actual", "persist"):
                        raise ValueError("plan must be actual or persist")
                    self._json(hindcast(app.grid(), parse_when(q["from"]), hours, plan))
                else:
                    self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            except (ValueError, KeyError) as e:
                self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)

    return Handler


def main(argv=None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="scout-web", description=__doc__.split("\n")[0])
    ap.add_argument("--host", default=os.environ.get("SCOUT_WEB_HOST", "127.0.0.1"))
    ap.add_argument(
        "--port", type=int, default=int(os.environ.get("SCOUT_WEB_PORT", 8080))
    )
    ap.add_argument(
        "--state-dir",
        help="the service's state directory (default $SCOUT_STATE_DIR, "
        "$STATE_DIRECTORY or ./lowpower-state)",
    )
    ap.add_argument(
        "--csv",
        default=csv_default(),
        help=f"telemetry CSV for hindcasts (default ${CSV_ENV})",
    )
    a = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    state = (
        a.state_dir
        or os.environ.get("SCOUT_STATE_DIR")
        or os.environ.get("STATE_DIRECTORY")
        or "lowpower-state"
    )
    app = App(Path(state.split(":")[0]), a.csv)
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(app))
    log.info(
        "serving http://%s:%d (state %s, hindcast %s)",
        a.host,
        a.port,
        app.state_dir,
        "on" if a.csv else "off",
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
