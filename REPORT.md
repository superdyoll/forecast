# Scout energy forecast and low-power mode: report

Status as of 2026-11-27. To install and run it, see the README:
[Quick start](README.md#quick-start) (simulation from a clone) and
[Install on the boat](README.md#install-on-the-boat-debianubuntu-apt) (debs, apt,
systemd).

All telemetry figures come from `scout_telemetry_2025-07.csv` (17 days, 24,674
messages). Simulation figures come from runs against ArduPilot 4.0 SITL and are
labelled as such. Days-long scenarios (storms) use `scout-sim fast`: the same
environment, power truth and planner without SITL.

## Summary

- **The battery percentage in Tether is a voltage reading,** exactly `20 × V − 400`.
  It sags under load and sticks at 100 % at 25 V, so it isn't stored energy.
- **The "thruster" channel is the whole boat's load.** With battery and solar it
  closes the energy balance to about 2 W, so energy in and out is measurable.
- **Starlink costs about 22 W,** twice the rest of the idle boat (about 10 W).
  Propulsion is the only bigger load.
- **A physical energy-balance model forecasts well when the plan is known:**
  3.23 points of error over 12 hours, against 7.69 for "stays the same". A Keras
  LSTM did worse than that baseline (7.99) and isn't used.
- **The low-power mode works end to end in SITL:** it uploads the mission, holds
  speed, keeps the reserve, and survives autopilot restarts, link loss, its own
  crashes and operator overrides.
- **Current is now in the model.** ArduPilot holds speed *over ground*; power
  depends on speed *through the water*. The service estimates the current and the
  hull's real top speed from measured propulsion power. In one SITL run, the
  1-hour forecast went from 7.6 points pessimistic to within 1 point. In another,
  ArduPilot went faster than commanded and the forecast was optimistic: that's
  the top open issue (see
  [Current](#current-speed-over-ground-vs-through-the-water)).
- **A 3-day storm used to run the battery flat.** Four fixes keep a boat that
  starts at 70 % above its reserve; weaker starts still dip below it, which only
  load-shedding or a weather forecast can fix (see [Storms](#storms)).

## What the telemetry says

| Finding | Evidence | Handling |
| --- | --- | --- |
| `battery_pct` = 20·V − 400 | Linear fit error 0.02 points over 24,587 rows | Treated as a voltage; model includes sag |
| "Thruster" channel = total load | Night: battery − "thruster" current within 0.07 A | Used as total load |
| Starlink up only in sessions, stops 07-27 02:05Z | 25 % of 15-min steps before then have a Starlink row | Solar and battery current missing most of the time; modelled as missing |
| Iridium has fewer fields and flushes backlogs | No solar or battery current; 8 rows in 6 s, out of order | Backlog rows flagged and left out |
| Autopilot restarts | Mode 16 with zeroed fields at 07-25 19:38 and 07-27 19:00 | Flagged; zeros set to missing |
| Timestamps are receipt times | `created_at` is when Tether received it | Resampled to 15 minutes |
| Mode 17 | 3,728 rows; not a stock Rover mode | Labelled `MODE_17`, meaning unconfirmed |

**Energy budget** (medians on the 15-minute grid, model fitted on all data):

| Component | Value | Source |
| --- | --- | --- |
| Hotel load | 9.9 W | Not under way, no Starlink session (742 steps) |
| Starlink | +22 W | Same states, with vs without a session |
| AUTO propulsion | +60 W, flattening at about 70 W above 0.7 m/s | AUTO minus hotel, in 0.2 m/s bins (263 steps) |
| MODE_17 propulsion | +4.5 W | Almost no thrust; consistent with station-keeping |
| Solar | mean 72 W when producing, peak 285 W | Solar channel V×A |
| Losses | 2 W | Solar − load − battery |

Propulsion is the strongest lever: going from 0.56 to 1.02 m/s over ground takes
about 3.5× the power.

## Forecast models

The **physical model** (`apps/forecast/.../energy.py`, standard library only)
simulates the battery on a 15-minute grid:
- **Load:** the planned load, or the last 6 hours' mean for a blind forecast.
- **Solar:** clear-sky `k · sin(sun elevation)^γ`, times a cloud factor learnt from
  recent readings.
- **Battery:** a rested level moved by net energy ÷ capacity, plus sag proportional
  to current.

The physical model and the LSTM were checked on the same 5 blocked, purged time
folds (1,458 windows; physical model: `scout-evaluate` run `20261127T233027Z`;
LSTM: run `20261126T192216Z`). Every parameter was refitted per fold. Error is in
battery-% points; lower is better.

| Method | 1 h | 3 h | 6 h | 12 h | All |
| --- | --- | --- | --- | --- | --- |
| **Physical, known plan** | **0.98** | **2.17** | **3.46** | **5.01** | **3.23** |
| Physical, blind | 1.64 | 3.53 | 6.42 | 11.99 | 6.56 |
| Stays the same | 1.84 | 4.66 | 8.18 | 12.40 | 7.69 |
| Same as yesterday | 2.09 | 4.76 | 8.34 | 14.19 | 8.18 |
| LSTM | 2.08 | 4.99 | 7.85 | 12.88 | 7.99 |

- **The known-plan model wins overall, and in every fold from 6 hours on.** In
  the last fold (07-27 to 07-30, mostly after Starlink stopped) the blind model
  is slightly better at 1 and 3 hours (0.69 against 0.71, 1.59 against 1.64). At
  1 hour it is near the noise floor of a voltage reading (0.2-point steps plus
  sag).
- **Almost all the skill comes from knowing the load.** So the low-power mode
  forecasts from its own plan.
- **The LSTM barely trained.** Early stopping chose epoch 1–2 in four folds, so it
  learnt little more than "no change". It had about 10 independent days per fold
  and a voltage target. It isn't used.

**Fitted parameters are stable across folds:**

| Parameter | Folds | All data |
| --- | --- | --- |
| Capacity (Wh per 100 points) | 2,100–2,400 | 2,300 |
| Sag (points per A) | 0.7–0.9 | 0.8 |
| Clear-sky solar `k` (W) | 241–365 | 361 |
| Solar exponent `γ` | 1.75–2.5 | 2.5 |
| Clock offset (h) | −0.75–0 | −0.75 |
| Average cloud factor | 0.55–0.68 | 0.62 |

**What the rerun changed.** The earlier run (`20261126T194630Z`: 3.11 overall,
`γ` 1.5, `k` 236) had `γ` at the top of the old search grid. With the grid
reaching 2.5 the fit chooses a sharper solar curve (`γ` 2.5, `k` 361): clear-sky
output at the July noon sun at 60 N (about 50° up) rises from 158 to 185 W, and at
20° falls from 47 to 25 W. It scores 0.02–0.16 points worse per lead time, within
the spread between folds. `γ`
again sits at the top of the grid, and it is chosen by how well it correlates
with measured solar, not by forecast error: worth testing a wider grid, or
choosing `γ` by forecast error. The shipped `fitted_params.json` is this run's
all-data fit.

**Bias.** The known-plan forecast is pessimistic by 1.2 points on average over
all lead times (blind: 1.7): the boat did a little better than forecast.

**Trust it for:** 6–12 hour forecasts under a given plan (about ±3–5 points), and
comparing plans. It has only been checked between about 28 % and 100 %, so keep a
margin near the reserve.

## The low-power mode

`scout-lowpower` runs on the boat and talks MAVLink to ArduPilot. Every 5 minutes
of boat time (sooner on events) it forecasts the battery for each option and picks
one. The options are each cruise speed from 0.3 to 1.2 m/s, with normal or minimal
Starlink duty, plus hold, drift or pause.
- **Feasible** means the forecast never drops below the reserve plus 5 points.
- **With a deadline:** the feasible speed that arrives in time with the most
  battery to spare. **The reserve outranks the deadline.**
- **Nothing feasible:** pause with motors off and let the sun recharge.
- **At the last waypoint or on station:** hold (loiter, stemming the current) if
  affordable, else drift with the motors off until holding is affordable again.
  Motoring back costs at least as much as holding (power grows with the cube of
  speed), so a boat that can't afford to hold can't afford to return either.
- **Speeds are over ground.** Each leg is priced with the estimated current: the
  water speed it takes, and the ground speed the hull can actually reach.

The decision logic (`planner.py`) is pure and unit-tested; MAVLink I/O and recovery
live in `service.py`. Every decision, its reasons and its forecast go to journald
and to `decision.json` / `history.jsonl`. `scout-web` scores those forecasts
against the readings that followed.

## What testing found

**Bugs the simulation exposed, all fixed:**
- Mission upload was rejected, because ArduPilot 4.0 needs frame 3, not 6.
- A failed upload was never retried.
- A heartbeat that was simply late was taken for an operator override.
- The planner held a speed forever.
- The learnt load factor stuck at 1.0, because it was timed in real seconds, not
  simulated.
- An arrival flipped back to transit.
- A stale waypoint index declared a new mission arrived.
- The simulator crashed when there was no `params.json` (it now uses the shipped
  fit, as the service does).

**Recovery, under systemd** (journals in `docs/evidence/`):
- **Autopilot killed mid-mission:** it saw the heartbeat loss at +5 s and the link
  back at +16 s. It kept the stored mission and re-armed at +20 s. AUTO was accepted
  at +61 s, once ArduPilot's EKF was ready. The service itself never restarted.
- **`kill -9` of the service:** systemd restarted it in 5 s.
- **A hung service (`SIGSTOP`):** the 30 s watchdog restarted it.

**Races in quick autopilot reboots, fixed.** A reboot shorter than the 5 s
heartbeat timeout shows only in the boot clock. If heartbeats arrived first, the
service took the rebooted autopilot's state as the operator's: first "disarmed on
purpose" (no re-arm; found by CI), then "switched to MANUAL" (an override; found
while adding the storm tests). After a reconnect it now neither records the armed
state nor judges overrides nor enforces its plan until the boot clock arrives.

**Bugs the storm and current work exposed, fixed:**
- **Safety:** below the battery floor the planner said "propulsion stopped" but
  still motored back to station (the drift option's return flag). Now drift is
  motors off, always.
- Motoring back against a gale was priced at 15 % of the time; it was 100 %.
- The current estimate averaged 10–15 minutes, but Starlink comes in 15-minute
  sessions: a window could be all-on or all-off (±15 W, more than low-speed
  propulsion). It now averages a whole Starlink cycle (1 hour).
- A current faster than the boat meant pausing, i.e. drifting downstream (10 km
  in one run). It now motors into it if that's affordable for half a tide (6 h).
- Today's peak tidal current was assumed to last the whole forecast; it now
  relaxes to its daily mean over 3 hours.
- `quickstart.sh` reused a SITL container whose boat had already finished the
  route; it now starts SITL fresh each run.

## Packaging

One deb per app, installed with apt. The `scout` metapackage gives the boat
`scout-forecast`, `scout-lowpower` and `scout-web`; `scout-sim` adds the simulation
and needs Docker.

Every merge into `main` runs `.github/workflows/release.yml`, in order:
1. Bump all versions together.
2. Run the unit tests.
3. Build the wheels and debs.
4. Smoke-test the debs under systemd: install, upgrade from the previous release,
   drive a fake autopilot, `kill -9`, purge.
5. Only then publish a GitHub release.

## Current: speed over ground vs through the water

ArduPilot holds the commanded speed over ground. Power depends on speed through
the water. The difference is the current (plus leeway), which Scout has no sensor
for. The load channel measures propulsion power, so the service (`current.py`):
- reads speed through the water off the propulsion curve and, with the heading
  and ground velocity, estimates the current (motors off: the drift is the
  current);
- learns the hull's real top speed: making clearly less over ground than
  commanded means it's at its limit;
- plans each leg with that current, relaxing to its daily mean over 3 hours.

The fitted model's top speed through the water is 0.85 m/s, read off the point
where AUTO power stops rising (about 73 W). It now ships with the package
(`fitted_params.json`) and as `/etc/scout/params.json` in the deb, so a clone or
an install without the private data uses the fit, not generic defaults.

**SITL, 20 ×, demo transit (6.7 km, 4 h deadline), 1-hour forecast error** (these
runs used the earlier fit, `20261126T194630Z`):

| Run | Current estimate error | 1 h error / bias | Stays the same |
| --- | --- | --- | --- |
| Before (deb install, generic defaults) | none | 7.96 / −7.62 | 2.18 |
| Current estimated, 10-min window | 0.27 m/s | 7.04 / −7.04 | 3.17 |
| + top speed learnt, 1-hour window | 0.12 m/s | 1.12 / −0.75 | 1.90 |
| Final, at 5 × (SITL keeps pace; heading averaged) | 0.23 m/s | 3.53 / +3.51 | 2.69 |

The 3-hour error in the third run was 0.64 points against 6.59 for "stays the
same". **Caveat:** on Apple silicon SITL is emulated and runs at about 5.5 × at
most, so at 20 × the boat lags the clock. Speeds are right but progress looks
slow, so arrival times in those runs aren't meaningful. `scout-sim` now warns
about this, and `quickstart.sh` uses 5 × on arm64. At 5 × the boat arrived at
09:20 against the 10:00 deadline. But its forecast turned optimistic, by
+3.5 points at 1 h and +7.2 at 3 h. From 08:00, on the legs across the
current, ArduPilot made 0.7–0.8 m/s over ground for a 0.3–0.4 m/s command, at
0.93 m/s through the water and 100–155 W. The planner had priced the command
at a few watts. See [Open issues](#open-issues).

**Fast simulation, the same planner with and without the current estimate**
(`scout-sim fast`, 0.75–2 days; error at 3 hours; rerun with the refitted model,
the simulator's truth kept ~5–10 % off it as before):

| Scenario | Without | With | Stays the same |
| --- | --- | --- | --- |
| Transit, seeds 1/2/3 (all on time) | 1.33 / 2.87 / 1.19 | 1.42 / 3.00 / 1.08 | 5.03 / 3.15 / 3.66 |
| Transit, slow hull (0.3 m/s), error / bias | 2.02 / −1.87 | 1.71 / −0.89 | 5.2 |
| Station-keeping, 2 days, error / bias | 1.19 / +0.43 | 1.16 / +0.23 | 4.4 |

On a hull as fast as the model believes, the estimate makes little difference
over a few hours (slightly worse on seeds 1 and 2, better on seed 3). It pays
where the hull is slower than the model: there it halves the bias, as in SITL,
where the large gains come from exactly that.

## Storms

`scout-sim fast --mission station --storm 24:72`: five days on station with an
Atlantic low from day 2 to day 4. Solar falls to 8 % of clear sky. Wind rises to
20 m/s, which adds about 0.6 m/s of leeway drift on top of the current. A calm
five days from 70 % never goes below 67.3 %.

| Planner | Start 70 %: low / hours below reserve | Start 55 % | Start 40 % | 12 h bias |
| --- | --- | --- | --- | --- |
| Before | 0 % / 74 h | 0 % / 81 h | – | +10.6 |
| Floor bug, drift and Starlink fixed | 27.8 % / 4.5 h | 21.0 % / 27 h | 16.8 % / 36 h | +3.0 |
| + 48 h look-ahead, bad weather persists 96 h, current relaxes | 31.2 % / 0 h | 23.4 % / 14 h | 13.9 % / 42 h | +4.1 |
| + code review (current estimated every boat minute, as on the boat; station priced at the learnt top speed) | 30.5 % / 0 h | 23.3 % / 14 h | 14.0 % / 41 h | +4.1 |
| + refitted model (`20261127T233027Z`) | **30.7 % / 0 h** | 23.8 % / 14 h | 12.4 % / 47 h | +3.5 |

The reserve is 30 %. What went wrong first:
1. **The battery floor didn't stop the motors.** This was the safety bug above.
2. **It kept motoring back against a drift it couldn't beat.**
3. **The simulator ignored the planner's Starlink advice.** It now follows it, as
   a relay would.
4. **The planner expected the sun back within 12 hours,** so it held station into
   the gale on day 1.

What helped most was the longer look-ahead. With 24 hours it couldn't see that
holding into a gale on day 1 leaves nothing for day 3. A weaker start still dips
below the reserve. With the motors off, the hotel load and minimal Starlink
(about 12 W) take about 36 points over 3 sunless days, more than a 40–55 %
battery has above the reserve.

**Suggested model changes for storms,** most valuable first:
1. **Use a weather forecast.** A cloud and wind forecast (e.g. MET Norway or ECMWF
   over Starlink, cached for Iridium-only periods) would replace the persistence
   guess. The boat could then charge to full before the storm, skip holding into
   it, and cut Starlink early.
2. **Check the reserve against a bad-weather case,** e.g. the 10th-percentile
   solar for the season. Keep the expected case for ETAs. The persistence rule is
   a crude version of this.
3. **Add a survival load plan.** Starlink fully off (Iridium only) and payloads
   and compute asleep: about 5 W was seen disarmed on 07-14, against about 12 W
   now. Over 3 days that's about 20 points, the gap between 55 % and surviving.
4. **Let the station radius grow with the weather.** Drift with the motors off in
   a gale and return after it: propulsion is cubic, so returning in calm water is
   far cheaper.
5. **Model leeway separately from the current.** In a gale leeway dominates and
   follows the wind, not the tide; a wind forecast predicts it.
6. **Check the battery model below 28 %.** The storm runs went to 11–14 %, outside
   the range the capacity and sag were fitted on. Voltage falls faster near empty.

## Open issues

1. **The autopilot doesn't always hold the commanded speed.** In the 5 × SITL run
   above, ArduPilot accepted `DO_CHANGE_SPEED` / `WP_SPEED` of 0.3–0.4 m/s but made
   0.7–0.8 m/s over ground, at 100–155 W. That's either SITL's `sailboat-motor`
   tuning or something about these legs across the current. The planner assumes
   ground speed = command.
   - **Next:** check the speed controller and `CRUISE_SPEED` / `CRUISE_THROTTLE`
     in SITL. Then learn achieved against commanded ground speed (and power)
     from telemetry, as the top speed already is, and price with what the boat
     actually does.
2. **State of charge.** Capacity is in Wh per 100 points of a voltage reading.
   Calibrating it needs a coulomb-counted charge–discharge cycle and the battery
   chemistry. If the battery is LiFePO4, voltage barely moves across most of its
   charge.
3. **The current estimate needs the load channel and heading.** On Iridium-only
   telemetry ashore it can't be checked, though the boat measures both onboard. It
   also lumps leeway in with the current.
4. **Arrival latches near the end only.** A boat that drifts beyond the drift
   radius after arriving reads as "not arrived" and plans a transit back. That's
   correct, but its 12-hour forecast in a storm was optimistic by about 6 points.

**Questions for the Scout team:**
- What are the battery chemistry and capacity?
- Is the "thruster" channel a shunt on the main bus, and is solar measured before or
  after the charge controller?
- What is mode 17?
- Are Starlink sessions deliberate?

**Worth measuring on a real boat:**
- Current for each consumer.
- Speed through the water.
- Solar on a fixed schedule, not only during Starlink sessions.

## Reproducing

The telemetry CSV is private and never in the repository. With it set in `.env`
(`SCOUT_TELEMETRY_CSV`), `scout-evaluate` reruns the cross-validation and `forecast`
runs single forecasts. The LSTM experiment is `python3 -m model.lstm.train_lstm`;
its run is `20261126T192216Z`. The simulation needs no private data:
`scripts/quickstart.sh` (SITL), and `scout-sim fast` for the scenarios above
(`--seed`, `--hull-top-stw`, `--storm`, `--no-current-estimate`).
