# Code review

Scope: every tracked file under `apps/`, `packaging/`, `scripts/`, `model/` and
`.github/`, at commit `de8ad95`. Line numbers refer to that commit.

The guiding rule is that the best code is no code. Where something can be
deleted or merged, the suggested diff does that. Findings are in four groups,
most important first:

1. [Logic errors](#1-logic-errors): wrong results or behaviour. Fix these first.
2. [Dead code](#2-dead-code): delete it.
3. [Duplication](#3-duplication): one idea written two to five times.
4. [Style inconsistencies](#4-style-inconsistencies).

Overall the code is in good shape. The planner is pure and well tested, and
the service's recovery logic is careful. Most findings are left over from the
fast pace of change: a removed feature still threaded through four files, and
helpers copied into the simulator instead of shared.

---

## 1. Logic errors

### 1.1 The web page's battery table almost never shows the actual value

`apps/web/src/scout_web/static/index.html:304-305`

```js
const byT = new Map(actual.map((p) => [p[0], p[1]]));
table(..., fc.map((p) => [fmtTime(new Date(p[0])), fmt(p[1]), fmt(byT.get(p[0]))]));
```

Forecast points fall at `decided_at + k × 15 min`. History rows fall at each
later decision time, which comes from a monotonic timer and never lands on the
same millisecond. An exact `Map` lookup therefore misses, and the "Actual %"
column shows "–". The server already matches readings within 10 minutes
(`server.py:40`, `MATCH_S`); the page should do the same.

```diff
-  const byT = new Map(actual.map((p) => [p[0], p[1]]));
-  table($("battery-table"), ["Time (UTC)", "Forecast %", "Actual %"], fc.map((p) => [fmtTime(new Date(p[0])), fmt(p[1]), fmt(byT.get(p[0]))]));
+  // Nearest reading within 10 min, as the server scores them (MATCH_S).
+  const near = (t) => { let b = null; for (const p of actual) if (Math.abs(p[0] - t) <= 6e5 && (!b || Math.abs(p[0] - t) < Math.abs(b[0] - t))) b = p; return b && b[1]; };
+  table($("battery-table"), ["Time (UTC)", "Forecast %", "Actual %"], fc.map((p) => [fmtTime(new Date(p[0])), fmt(p[1]), fmt(near(p[0]))]));
```

### 1.2 Station-keeping ignores the learnt top speed

`apps/lowpower/src/scout_lowpower/planner.py:413-417`

Transit uses `_stw_max(s, p)`, which is the lower of the model's top speed and
the one learnt in practice. Station-keeping caps the stem at the model's
`E.max_stw_ms(p)` only. On a hull slower than the model, the planner therefore
prices holding station at more power than the motors can actually draw. It
then drifts when it could have held. The default argument also hides the
inconsistency.

```diff
-def _station_w(p, cfg, current=(0.0, 0.0)) -> float:
+def _station_w(p, cfg, current, stw_max) -> float:
     """Station-keeping propulsion: at least the observed MODE_17 draw, and
     enough to stem the current (capped at the hull's top speed)."""
-    stem = E.propulsion_w(p, min(math.hypot(*current), E.max_stw_ms(p)))
+    stem = E.propulsion_w(p, min(math.hypot(*current), stw_max))
     return max(E.load_component(p, "mode17_propulsion_w"), cfg.station_hold_w, stem)
```

There are two callers. At `planner.py:345` and `:426`, pass `_stw_max(s, p)`
(computed once, outside `load()`).

### 1.3 The Starlink relay runs on real time; everything else uses boat time

`apps/lowpower/src/scout_lowpower/service.py:905-912`

```python
phase = (time.time() % self.st.starlink_period_s) / self.st.starlink_period_s
```

Two other places assume the Starlink cycle is in boat seconds:

- `_observe_current` divides `starlink_period_s` by `clock.rate()`
  (`service.py:718`);
- scout-sim switches Starlink on the simulated clock (`power.py:61`,
  `t.timestamp()`).

On a 20× clock the relay's cycle is 20 times longer than the window that
averages it. On the boat the rate is 1, so this only affects the simulation.

```diff
-            phase = (
-                time.time() % self.st.starlink_period_s
-            ) / self.st.starlink_period_s
+            period = self.st.starlink_period_s
+            phase = (_now().timestamp() % period) / period
```

### 1.4 `scout-sim fast` doesn't estimate the current the way the service does

`apps/sim/src/scout_sim/fast.py:40, 175, 193`

The docstring says "The service's telemetry habits are copied". They aren't:

- **The service** blends every boat minute with weight 0.3
  (`service.py:495, 756`), which is a time constant of about 3 minutes.
- **The fast sim** blends every 15 minutes (`OBSERVE_S = 900`) with weight 0.3,
  which is about 45 minutes.

The A/B tables in REPORT.md therefore measure a much slower estimator than the
one on the boat. The fix is to share one function (see [3.3](#33-current-and-cloud-estimation-written-twice));
in the meantime:

```diff
-OBSERVE_S = 900  # cloud and current updates
+OBSERVE_S = 60  # cloud and current updates: once a boat minute, as the service
```

### 1.5 The web server parses the whole history twice on every poll

`apps/web/src/scout_web/server.py:110-111`

```python
rows = read_history(state_dir, hours)
errors = score(read_history(state_dir))  # score over everything kept
```

Each open tab polls every 5 s, and each poll reads and JSON-parses up to
2 × 5 MB, twice. That's fine on a laptop, but not on the boat's computer. Read
the history once and filter it:

```diff
-    rows = read_history(state_dir, hours)
-    errors = score(read_history(state_dir))  # score over everything kept
+    every = read_history(state_dir)
+    errors = score(every)  # score over everything kept
+    cutoff = every[-1]["_t"] - timedelta(hours=hours) if every else None
+    rows = [r for r in every if r["_t"] >= cutoff]
```

`read_history` can then lose its `hours` parameter (lines 43, 58-60).

### 1.6 `scout-sitl.service` stops a container that the script may not have started

`packaging/systemd/scout-sitl.service:16` and `packaging/sitl-container.sh:13`

The script honours `SCOUT_SITL_CONTAINER`, but `ExecStop` hard-codes
`docker stop scout-sitl`. Setting the variable also breaks the purge script
(`build_debs.py:164`) and `quickstart.sh:79`. Nothing sets it, so delete the
knob:

```diff
-name="${SCOUT_SITL_CONTAINER:-scout-sitl}"
+name=scout-sitl  # also in scout-sitl.service (ExecStop) and the deb's purge
```

### 1.7 Stale documentation that contradicts the code

| Where | Says | Fact |
| --- | --- | --- |
| `service.py:17` | state.json holds "the learned cloud and load factors" | the load factor was removed; it holds cloud observations, the current and the top speed |
| `service.py:696` | `_observe`: "Learn the cloud factor (solar) and load factor (propulsion)" | it learns cloud and current |
| `data.py:194` | `# panel side, before MPPT` | AGENTS.md: before or after the charge controller is unconfirmed |
| `mavlink.py:24` | defs are "`[(field, struct code, count)]`" | they are space-separated `field:code[*n]` strings |
| `planner.py:108` | `returning` "kept for state files" | `Plan.from_dict` already drops unknown keys (line 134), so it isn't needed (see 2.1) |

### 1.8 (Minor) A partial TCP write is recovered from only by luck

`apps/lowpower/src/scout_lowpower/mavlink.py:427`

`sendall` on a non-blocking socket raises `BlockingIOError` after a partial
write when the send buffer is full. AGENTS.md notes that SITL stalls on a port
whose client stops reading. The error is caught as `OSError` and closes the
link, so the half-sent frame doesn't corrupt the stream. That's correct, but
only by accident. Add a comment so nobody "fixes" the except clause.

---

## 2. Dead code

### 2.1 `Plan.returning` is always `False`: remove it everywhere

Since drifting became motors-off, nothing sets it. It still costs code in four
files:

| File | Lines |
| --- | --- |
| `apps/lowpower/src/scout_lowpower/planner.py` | 108, 144, 457-458, 474 |
| `apps/lowpower/src/scout_lowpower/service.py` | 735, 820, 842-843 |
| `apps/sim/src/scout_sim/fast.py` | 83, 151, 202 |
| `apps/web/src/scout_web/static/index.html` | 268 |

Old `state.json` files still load, because `Plan.from_dict` ignores unknown
keys.

```diff
 # planner.py
-    returning: bool  # always False now (drift is motors off); kept for state files
     feasible: bool
@@
             and other.starlink_duty == self.starlink_duty
-            and other.returning == self.returning
         )
@@ def _plan(...)
-    # Drifting is motors off; holding (LOITER at the station) brings it back.
-    returning = False
     eta = (
@@
-        returning=returning,
```

```diff
 # service.py
-        drifting = (
-            pl.action in ("pause", "drift")
-            and not pl.returning
-            and (self.ap.mode == M.MODE_HOLD or not self.ap.armed)
-        )
+        drifting = pl.action in ("pause", "drift") and (
+            self.ap.mode == M.MODE_HOLD or not self.ap.armed
+        )
@@ _decide
-            + (" (returning)" if new.returning else "")
@@
     def _desired_mode(self):
-        pl = self.plan
-        if pl.action == "transit" or pl.action == "hold":
-            return M.MODE_AUTO
-        if pl.action == "drift" and pl.returning:
-            return M.MODE_AUTO
-        return M.MODE_HOLD
+        # hold = AUTO: the mission ends in LOITER_UNLIM at the station.
+        return M.MODE_AUTO if self.plan.action in ("transit", "hold") else M.MODE_HOLD
```

```diff
 # fast.py
-        elif action in ("hold", "transit") or (action == "drift" and plan.returning):
-            home = ...
-            if action == "drift" or distance_m(pos, home) > m.radius_m:
+        elif action in ("hold", "transit"):
+            home = ...
+            if distance_m(pos, home) > m.radius_m:
@@
-        motors = (
-            plan is not None
-            and plan.action in ("transit", "hold")
-            or bool(plan and plan.returning)
-        )
+        motors = plan is not None and plan.action in ("transit", "hold")
@@
-                elif plan.action in ("pause", "drift") and not plan.returning:
+                elif plan.action in ("pause", "drift"):
```

```diff
 // index.html:268
-  ... `Starlink duty ${Math.round(plan.starlink_duty * 100)}%${plan.returning ? " · returning" : ""}` ...
+  ... `Starlink duty ${Math.round(plan.starlink_duty * 100)}%` ...
```

### 2.2 Unused or identity code

| Where | What | Suggestion |
| --- | --- | --- |
| `current.py:30-32` | `from_drift(ground)` returns `ground` | delete; callers use `ground` (`service.py:753`, `fast.py:203, 205`) |
| `mission.py:56-60` | `Mission.goal` | never used: delete |
| `service.py:106, 138` | `Telemetry.heading` | written, never read: delete (`headings` is the one used) |
| `service.py:922-923` | `_signature(None)` guard | both callers pass a list: delete |
| `service.py:353` | `decision.json["starlink_advice"]` | duplicates `plan.starlink_duty`; nothing reads it: delete |
| `service.py:783-784` | `if t.mission_seq > len(...): next_wp = len - 1` | already done by the `min(...)` on line 782: delete |
| `planner.py:438-439` | `_project(..., cfg=None)` plus `cfg = cfg or Config()` | every caller passes `cfg`: make it required |
| `mavlink.py:127` | `MAV_COMP_ID_AUTOPILOT1` | unused: delete |
| `power.py:57, 79` | `PowerModel.last` | never read: return the dict and drop the attribute; compute `starlink_on(t)` once per step |
| `run.py:180` | `getattr(self, "_pace_ref", None)` | set `self._pace_ref = None` in `__init__` |
| `build_debs.py:365-366` | `_long()` | one use: inline `+ (f"\n {summary}" if summary else "")` |

### 2.3 `run_sitl.sh` recomputes what the CLI already does

`apps/sim/run_sitl.sh:62, 76, 80`

The script computes the deadline in Python, and computes
`max(15, 300 / rate)` for `--interval-s`. Since `--arrive-in` was added and
the CLI defaults the interval from `SCOUT_CLOCK` (`cli.py:58-61`), both are
redundant:

```diff
-arrive="$("$PYTHON" -c "from datetime import timedelta; from scout_forecast import clock; print((clock.now()+timedelta(hours=4)).strftime('%Y-%m-%dT%H:%MZ'))")"
 case "$SIM_MISSION" in
   transit) "$PYTHON" -m scout_lowpower set-mission \
-             --transit 60.215,2.210 60.230,2.240 60.240,2.200 --arrive-by "$arrive" --reserve 30 ;;
+             --transit 60.215,2.210 60.230,2.240 60.240,2.200 --arrive-in 4 --reserve 30 ;;
@@
-interval="$("$PYTHON" -c "print(max(15, 300 / float('$SIM_RATE')))")"
@@
-"$PYTHON" -m scout_lowpower run --power-link udpin:0.0.0.0:14601 \
-  --interval-s "$interval" >"$SIM_WORK/lowpower.log" 2>&1 & pids+=($!)
+"$PYTHON" -m scout_lowpower run --power-link udpin:0.0.0.0:14601 \
+  >"$SIM_WORK/lowpower.log" 2>&1 & pids+=($!)
-echo "running: sim, lowpower (decides every ${interval}s), web ..."
+echo "running: sim, lowpower, web ..."
```

---

## 3. Duplication

### 3.1 ISO time parsing, three times

`forecast.py:29-36` (`parse_when`), `mission.py:89-95` (`parse_time`), and the
time-zone half of `clock.py:31-33`. All three do "replace Z, assume UTC,
convert to UTC". Two more problems:

- scout-sim imports `parse_time` from `scout_lowpower.mission` just to parse a
  `--start` flag (`run.py:38`, `fast.py:33`).
- `planner.py:128` imports it inside a method.

Keep one, in the bottom package:

```diff
 # scout_forecast/clock.py
+def parse_iso(s: str) -> datetime:
+    """ISO 8601, 'Z' allowed; naive times are UTC. Always returns UTC."""
+    t = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
+    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)
+
 def parse(spec: str):
     sim, _, rest = spec.partition("@")
     real, _, rate = rest.partition("x")
-    t = datetime.fromisoformat(sim.replace("Z", "+00:00"))
-    if t.tzinfo is None:
-        t = t.replace(tzinfo=timezone.utc)
-    return t, float(real), float(rate or 1.0)
+    return parse_iso(sim), float(real), float(rate or 1.0)
```

Then delete `forecast.parse_when` and `mission.parse_time`, and import
`clock.parse_iso` in their callers: `server.py`, `cli.py`, `planner.py`,
`mission.py`, `run.py` and `fast.py`. `data.parse_time` is different (Tether's
own format) and stays.

### 3.2 The base load, five times

`hotel + Starlink × duty` is spelled out at:

- `planner.py:325, 378, 421`;
- `service.py:742-745`;
- `fast.py:188-191`.

```diff
 # scout_forecast/energy.py
+def base_load_w(p: Params, starlink_duty: float) -> float:
+    """Hotel load plus Starlink at the given duty: everything but propulsion."""
+    return load_component(p, "hotel_w") + load_component(p, "starlink_w") * starlink_duty
```

```diff
 # planner.py (three places)
-    base = E.load_component(p, "hotel_w") + E.load_component(p, "starlink_w") * duty
+    base = E.base_load_w(p, duty)
```

### 3.3 Current and cloud estimation, written twice

`fast.py:175-213` re-implements `Service._observe_current` (`service.py:708-763`)
and `Service._cloud` (`service.py:765-769`). It does this with tuples instead
of deques, and already with a different cadence ([1.4](#14-scout-sim-fast-doesnt-estimate-the-current-the-way-the-service-does)).
This is exactly how the A/B drifted from the real service. Move the pure part
into `current.py` and call it from both:

```python
# scout_lowpower/current.py
def observe(p, plan, mode_ok: bool, armed: bool, ground, heading, load_w):
    """One observation of the current from a window of telemetry, or None.
    mode_ok: the autopilot is in the mode the plan asks for."""
    if plan.action in ("transit", "hold") and mode_ok and armed and heading is not None:
        return from_thrust(p, ground, heading, load_w - E.base_load_w(p, plan.starlink_duty))
    if plan.action in ("pause", "drift") and (mode_ok or not armed):
        return ground
    return None


def cloud_factor(obs) -> float | None:
    """Observed / clear-sky solar over [(t, observed W, clear-sky W), ...]."""
    obs = [o for o in obs if o[2] > 20]
    return sum(o[1] for o in obs) / sum(o[2] for o in obs) if len(obs) >= 4 else None
```

Most of `_observe_current` then becomes about 10 lines in each caller.

### 3.4 Loading params with a fallback, three times

- `lowpower/cli.py:67-80` (`_params`);
- `sim/run.py:358-368` (`load_params`), identical except for the log level;
- `forecast.py:137-139`, which has no fallback.

```diff
 # scout_forecast/energy.py
+def load_params(path) -> Params:
+    """params.json at `path`, else (missing, unreadable or None) the shipped fit.
+    Never raises: under systemd a bad file must not crash-loop the planner."""
+    if path:
+        try:
+            return Params.from_dict(json.loads(Path(path).read_text()))
+        except (OSError, ValueError, TypeError) as e:
+            log.error("can't read energy params %s (%s); using the shipped fit", path, e)
+    return fitted_params()
```

### 3.5 State-directory resolution, twice

`lowpower/cli.py:38-44` and `web/server.py:260-266`: the same
`arg or $SCOUT_STATE_DIR or $STATE_DIRECTORY or "lowpower-state"` chain,
including the `.split(":")[0]`. Web can't import lowpower, but both import
`scout_forecast.env`:

```python
# scout_forecast/env.py
def state_dir(arg: str | None) -> Path:
    """--state-dir, else $SCOUT_STATE_DIR, $STATE_DIRECTORY (systemd; the first
    of a ':' list) or ./lowpower-state."""
    v = arg or os.environ.get("SCOUT_STATE_DIR") or os.environ.get("STATE_DIRECTORY")
    return Path((v or "lowpower-state").split(":")[0])
```

### 3.6 MAVLink command builders, in the service and the sim

The service builds `COMMAND_LONG` and `PARAM_SET` at `service.py:675-692`. The
sim builds them again at `run.py:128-136` and `:138-149`, then again at
`:199-212`, so it has three hand-written `PARAM_SET`s. They belong on the
link:

```python
# mavlink.py, class Connection
def command(self, target: dict, cmd: int, *params: float) -> bool:
    p = [float(v) for v in params] + [0.0] * (7 - len(params))
    return self.send("COMMAND_LONG", command=cmd, confirmation=0,
                     **{f"param{i + 1}": v for i, v in enumerate(p)}, **target)

def param_set(self, target: dict, name: str, value: float) -> bool:
    return self.send("PARAM_SET", param_id=name, param_value=float(value),
                     param_type=MAV_PARAM_TYPE_REAL32, **target)
```

The `_every` rate limiter is also copied (`service.py:914-918`,
`run.py:81-85`). It's 5 lines, so moving it is optional.

### 3.7 Storm spec parsing, twice, done differently

`run.py:58-65` rebuilds `EnvConfig(...)`, while `fast.py:108-112` uses
`replace(...)`. Use one helper in `environment.py`:

```python
def env_config(seed: int, storm: str | None, storm_cloud: float) -> EnvConfig:
    """EnvConfig, with a storm from `storm` = 'START_H:HOURS' if given."""
    if not storm:
        return EnvConfig(seed=seed)
    h0, hours = (float(x) for x in storm.split(":")[:2])
    return EnvConfig(seed=seed, storm_start_h=h0, storm_hours=hours, storm_cloud=storm_cloud)
```

The `--storm` and `--storm-cloud` argparse lines are also duplicated
(`run.py:329-334`, `fast.py:306-309`). Move them into an
`environment.add_storm_args(ap)`.

### 3.8 SITL's `docker run`, twice

`apps/sim/run_sitl.sh:37-58` and `packaging/sitl-container.sh:38-60` hold the
same image, ports, entrypoint, model and the long `--defaults` list, plus the
same rate-label recreate logic. A change to `scout.parm` handling must be made
in both. Have `run_sitl.sh` run the packaged script in the background:

```diff
-if [ "${SIM_FRESH:-0}" = 1 ] && ...
-...
-    >/dev/null
-fi
+[ "${SIM_FRESH:-0}" = 1 ] && "$DOCKER" rm -f scout-sitl >/dev/null 2>&1 || true
+"$here/../../packaging/sitl-container.sh" >"$SIM_WORK/sitl.log" 2>&1 & pids+=($!)
```

This needs `sitl-container.sh` to find `scout.parm` next to itself or through
a variable. Right now it expects `$share/sitl`. Add
`parm="${SCOUT_SITL_PARM_DIR:-$share/sitl}"`, or ship the parm under
`packaging/`.

### 3.9 Smaller duplicates

| Duplicate | Where | Keep |
| --- | --- | --- |
| `LEADS_H = (1, 3, 6, 12, 24)` | `evaluate.py:34`, `server.py:39`, `fast.py:43` | one in `scout_forecast.evaluate`; web and sim already depend on scout-forecast |
| Rover mode names | `data.MODE_NAMES` (`data.py:47`), `mavlink.ROVER_MODES` (`mavlink.py:143`) | one in `scout_forecast.data`, which `mavlink` imports (lowpower already depends on it) |
| Fallback voltage `23.5` | `energy.py:369, 521`, `service.py:131` | `E.NOMINAL_V = 23.5` |
| `STEPS_PER_H = 4` and `/ 4` | `energy.py:39`, `forecast.py:75` | `STEPS_PER_H = 3600 // STEP_S`; use it in `forecast.py:75` |
| Clear-sky solar | `energy.py:373-391` (`solar_at` re-derives `clear_sky_w` inline) | `clear_sky_w(p, t + timedelta(seconds=STEP_S / 2), g["lat"][-1], g["lon"][-1])` |
| The cubic's reference point | `energy.py:469-470` and `487-488`; only one guards `ref_w` against 0 | `_ref(p) -> (ref_v, ref_w)`, used by both |
| Unit vector between points | `fast.py:52-56` (`unit_to`) and `mission.py:147-151` (inside `legs`) | `mission.unit(a, b)`, used by both |
| "Recently restarted" | `service.py:477-480`, `:570-573` | `Autopilot.restarted_recently(now)` |
| "Starlink reduced to minimal duty…" | `planner.py:278-281`, `290-293`, `400-403` | `_note_min_duty(reasons, duty, cfg)`, or add it in `_plan` |
| Floor branch | `planner.py:189-193`: two `return _plan(opt, …)` | choose `opt`, return once |
| Forecast entry dict | `server.py:118-124`, `131-137` | build with a small `entry(r)` |
| Mean over a window | `Telemetry.mean`, `mean_heading`, `ground_velocity` (`service.py:145-164`) all filter `now - t <= window_s` | one `_window(q, window_s, now)` generator |
| `_avg` and `_mean` | `evaluate.py:100, 195` | `_avg` can call `_mean` |
| LSTM grid and folds | `model/lstm/train_lstm.py:69-184` re-implements `build_grid`, gap filling, `blocked_folds` and `no_overlap` | import them from `scout_forecast.energy` / `.evaluate` (it already imports `scout_forecast.data`) and convert to numpy at the edge |
| Unit-test loop | `release.yml:80-83` and `quickstart.sh:56-59` | fine as is; worth a shared `scripts/test.sh` only if it grows |

---

## 4. Style inconsistencies

### 4.1 Import grouping

Most modules use stdlib, then third party/sibling packages, then relative
imports, each group sorted. These don't:

```diff
 # data.py:33-41
 import argparse
 import csv
 import math
-
 import sys
 from collections import Counter
+from collections.abc import Iterable
 from datetime import datetime, timezone
 from pathlib import Path
-from collections.abc import Iterable
```

```diff
 # planner.py:40-43
+import math
 from dataclasses import asdict, dataclass, field, replace
 from datetime import datetime, timedelta
-
-import math
```

```diff
 # forecast.py:24-26 (and evaluate.py:30-32)
-from .env import csv_arg
-
 from . import energy as E
+from .env import csv_arg
```

Also move `planner.py:128`'s local `from .mission import parse_time` to the
existing top-level import from `.mission`. There's no cycle to avoid.

### 4.2 Comments that black wrapped into odd parentheses

A trailing comment on a long default made black wrap the value. Put the
comment above, like every other field in these dataclasses:

```diff
 # planner.py:68-70
-    stem_hours: float = (
-        6.0  # no headway: price motoring into it this long (a tide turns)
-    )
+    # No headway: price motoring into it this long (a tide turns).
+    stem_hours: float = 6.0
 # planner.py:98-100
-    at_final: bool = (
-        False  # the autopilot is on the loiter item after the last waypoint
-    )
+    # The autopilot is on the loiter item after the last waypoint.
+    at_final: bool = False
 # service.py:83-85
-    battery_current_sign: float = (
-        -1.0
-    )  # ArduPilot: + = discharging; we use + = charging
+    # ArduPilot: + = discharging; we use + = charging.
+    battery_current_sign: float = -1.0
```

### 4.3 Hard-to-read expressions

- **Operator precedence:** `fast.py:148-152`, `a and b or c`. Removed by 2.1.
- **Nested ternary:** `cli.py:180-184`, `x if a else y if b else None`:

  ```diff
  -            arrive_by=(
  -                parse_time(a.arrive_by)
  -                if a.arrive_by
  -                else clock.now() + timedelta(hours=a.arrive_in) if a.arrive_in else None
  -            ),
  +            arrive_by=_deadline(a),
  ```

  with `_deadline` written as three `if`/`return` lines.
- **Generic setter loop:** `cli.py:55-63` is a `setattr` loop plus a special
  case for `interval_s`, and needs `--dry-run` with `default=None` to work.
  Four explicit lines are shorter and need no trick:

  ```diff
  -    for k in ("interval_s", "dry_run", "starlink_relay"):
  -        if getattr(a, k, None) is not None:
  -            setattr(st, k, getattr(a, k))
  -    if getattr(a, "interval_s", None) is None:
  -        # 5 min of boat time between decisions; ...
  -        st.interval_s = max(15.0, DECIDE_EVERY_S / clock.rate())
  -    if getattr(a, "no_rearm", False):
  -        st.rearm = False
  +    # 5 min of boat time between decisions; on a fast simulated clock that
  +    # is fewer real seconds (but not under 15 s).
  +    st.interval_s = getattr(a, "interval_s", None) or max(15.0, DECIDE_EVERY_S / clock.rate())
  +    st.dry_run = bool(getattr(a, "dry_run", False))
  +    st.starlink_relay = getattr(a, "starlink_relay", None)
  +    st.rearm = not getattr(a, "no_rearm", False)
  ```

  Then drop `default=None` from `--dry-run` (`cli.py:123`).
- **Lambda with `noqa`:** `run.py:230`, `amps = lambda w, volts: ...  # noqa: E731`.
  A two-line `def` needs no noqa.

### 4.4 Resource handling and checks

- **No `with` blocks:** `fast.py:135-136` opens `truth.jsonl` and
  `history.jsonl` without `with`, so an exception mid-run leaves them
  unflushed. Wrap the loop in `with open(...) as truth_f, open(...) as hist_f:`.
- **`assert` for validation:** `build_debs.py:309` validates a version with
  `assert`, which `python -O` removes. Use
  `if v != version: raise SystemExit(...)`, as the rest of the file does.

### 4.5 Formatting

- **Mixed string formatting:** `server.py:213` uses `'"%s"' % ...` in a
  codebase that uses f-strings everywhere else.
- **Inconsistent load order:** `service.py:293-299` restores state in a
  different order from the one `_save_state` writes it (`stw_max` lands
  between `current` and `current_mean`). Matching the order makes the pair
  easy to check by eye.

### 4.6 Uneven caching

- **`clock`:** `now()` caches the parsed `SCOUT_CLOCK` but `rate()` re-parses
  it on every call, which happens every loop tick (`service.py:495`). Share
  the cache:

  ```diff
  -def now() -> datetime:
  -    global _spec
  -    raw = os.environ.get(ENV)
  -    if not raw:
  -        return datetime.now(timezone.utc)
  -    if _spec is None or _spec[0] != raw:
  -        _spec = (raw, parse(raw))
  -    sim0, real0, rate = _spec[1]
  -    return sim0 + timedelta(seconds=(time.time() - real0) * rate)
  -
  -
  -def rate() -> float:
  -    raw = os.environ.get(ENV)
  -    return parse(raw)[2] if raw else 1.0
  +def _current():
  +    global _spec
  +    raw = os.environ.get(ENV)
  +    if not raw:
  +        return None
  +    if _spec is None or _spec[0] != raw:
  +        _spec = (raw, parse(raw))
  +    return _spec[1]
  +
  +
  +def now() -> datetime:
  +    c = _current()
  +    if c is None:
  +        return datetime.now(timezone.utc)
  +    sim0, real0, r = c
  +    return sim0 + timedelta(seconds=(time.time() - real0) * r)
  +
  +
  +def rate() -> float:
  +    c = _current()
  +    return c[2] if c else 1.0
  ```

- **`env`:** when there is no `.env`, `_dotenv_dir` stays `None`, so every
  `csv_default()` walks the directory tree again (`env.py:64-65`). Record that
  the search was done (`_searched = True`) rather than what it found.

---

## Suggested order

1. Fix **1.1 to 1.4**. These are small diffs with visible effects. 1.2 and 1.4
   change planner and simulation results, so rerun `scout-sim fast` and update
   the REPORT tables afterwards.
2. Remove the dead code in **2.1 and 2.2**. Together they are about 40 lines,
   with no behaviour change.
3. Merge **3.1 to 3.4**. They remove the largest duplicates and stop the fast
   simulator drifting from the service again.
4. Leave the style items for when each file is next edited.

All unit tests should pass unchanged after 2.x and 4.x. No station test sets
`stw_max_ms`, so 1.2 needs a new one: a station test with a strong current
and a learnt top speed below the model's.
