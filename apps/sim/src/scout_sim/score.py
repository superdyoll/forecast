"""Score a SITL run (apps/sim/run_sitl.sh or scripts/sim-batch.sh output).

    scout-sim score runs/overnight/*/

Reads the service's history (state/history.jsonl: every decision with its
forecast) and the simulator's truth (sim-truth.jsonl), and reports, as
`scout-sim fast` does: forecast error by lead time against the true battery,
hours below the reserve, arrival against the deadline; plus what only SITL
shows: how well the service estimated the drift (current + leeway), and the
speed ArduPilot made over ground against the speed it was commanded.
Writes score.json in each run directory.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from scout_forecast.clock import parse_iso
from scout_lowpower.mission import load as load_mission

from .fast import summarise


def _jsonl(path: Path) -> list[dict]:
    rows = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue  # a half-written last line
    return rows


def score(run: Path) -> dict:
    run = Path(run)
    truth = _jsonl(run / "sim-truth.jsonl")
    hist = [r for r in _jsonl(run / "state" / "history.jsonl") if r.get("forecast")]
    if not truth or not hist:
        return {"run": run.name, "error": "no truth or no decisions"}
    m = load_mission(run / "state" / "mission.json")
    minute = {int(parse_iso(t["time"]).timestamp() // 60): t for t in truth}
    actions = {}
    for r in hist:
        actions[r["action"]] = actions.get(r["action"], 0) + 1
    out = summarise(
        hist,
        {k: t["pct"] for k, t in minute.items()},
        m,
        actions,
        parse_iso(hist[0]["time"]),
    )

    # Drift: the service's estimate against the truth at the same minute.
    errs = []
    for r in hist:
        t = minute.get(int(parse_iso(r["time"]).timestamp() // 60))
        if t and r.get("current_ms"):
            errs.append(math.dist(r["current_ms"], (t["drift_n"], t["drift_e"])))
    # Speed over ground against the command, while transiting under AUTO.
    made = [
        (r["speed_ms"], r["sog"])
        for r in hist
        if r["action"] == "transit"
        and r.get("speed_ms")
        and r.get("sog") is not None
        and r.get("mode") == "AUTO"
    ]
    t0, t1 = parse_iso(truth[0]["time"]), parse_iso(truth[-1]["time"])
    sim_log = run / "sim.log"
    out.update(
        run=run.name,
        boat_hours=round((t1 - t0).total_seconds() / 3600, 2),
        decisions=len(hist),
        storm_max=max(t.get("storm", 0) for t in truth),
        drift_error_ms=round(sum(errs) / len(errs), 3) if errs else None,
        sog_over_command=(
            round(sum(s / c for c, s in made) / len(made), 2) if made else None
        ),
        pace_warnings=(
            sim_log.read_text().count("SITL runs at x") if sim_log.exists() else None
        ),
    )
    (run / "score.json").write_text(json.dumps(out, indent=1))
    return out


def table(scores: list[dict]) -> str:
    """One line per run: what to compare across a batch."""
    head = (
        f"{'run':22} {'h':>5} {'min%':>6} {'<res h':>6} {'arrived':>6} "
        f"{'1h':>5} {'3h':>5} {'3h stay':>7} {'drift':>6} {'sog/cmd':>7} {'storm':>5}"
    )
    lines = [head]
    for s in scores:
        if "error" in s:
            lines.append(f"{s['run']:22} {s['error']}")
            continue
        e = s["forecast_error"]

        def mae(lead, key="mae"):
            return f"{e[lead][key]:5.2f}" if lead in e else "    -"

        if s["arrive_by"] and s["arrived"]:
            late = (parse_iso(s["arrived"]) - parse_iso(s["arrive_by"])).total_seconds()
            arrived = f"{late / 60:+5.0f}m"
        else:
            arrived = "   yes" if s["arrived"] else "     -"
        lines.append(
            f"{s['run']:22} {s['boat_hours']:5.1f} {s['battery_min_pct']:6.1f} "
            f"{s['hours_below_reserve']:6.2f} {arrived:>6} {mae('1h')} {mae('3h')} "
            f"{mae('3h', 'mae_persistence'):>7} "
            f"{s['drift_error_ms'] if s['drift_error_ms'] is not None else '-':>6} "
            f"{s['sog_over_command'] if s['sog_over_command'] is not None else '-':>7} "
            f"{s['storm_max']:5.2f}"
        )
    return "\n".join(lines)
