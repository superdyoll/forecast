# Scout energy forecast and low-power mode: report

Status as of 2026-11-27. To install and run it, see the README:
[Quick start](README.md#quick-start) (simulation from a clone) and
[Install on the boat](README.md#install-on-the-boat-debianubuntu-apt) (debs, apt,
systemd).

All telemetry figures come from `scout_telemetry_2025-07.csv` (17 days, 24,674
messages). Simulation figures come from runs against ArduPilot 4.0 SITL and are
labelled as such.

## Summary

- **The battery percentage in Tether is a voltage reading,** exactly `20 × V − 400`.
  It sags under load and sticks at 100 % at 25 V, so it isn't stored energy.
- **The "thruster" channel is the whole boat's load.** With battery and solar it
  closes the energy balance to about 2 W, so energy in and out is measurable.
- **Starlink costs about 22 W,** twice the rest of the idle boat (about 10 W).
  Propulsion is the only bigger load.
- **A physical energy-balance model forecasts well when the plan is known:**
  3.11 points of error over 12 hours, against 7.69 for "stays the same". A Keras
  LSTM did worse than that baseline (7.99) and isn't used.
- **The low-power mode works end to end in SITL:** it uploads the mission, holds
  speed, keeps the reserve, and survives autopilot restarts, link loss, its own
  crashes and operator overrides.
- **Biggest open issue:** ArduPilot holds speed *over ground*, but the energy model
  and the plan are in speed *through the water*. With current, the plan's power
  and arrival time can be badly wrong (see [Open issues](#open-issues)).

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
folds (1,458 windows; `scout-evaluate` run `20261126T194630Z`). Every parameter
was refitted per fold. Error is in battery-% points; lower is better.

| Method | 1 h | 3 h | 6 h | 12 h | All |
| --- | --- | --- | --- | --- | --- |
| **Physical, known plan** | **0.96** | **2.07** | **3.30** | **4.87** | **3.11** |
| Physical, blind | 1.65 | 3.45 | 6.25 | 11.84 | 6.44 |
| Stays the same | 1.84 | 4.66 | 8.18 | 12.40 | 7.69 |
| Same as yesterday | 2.09 | 4.76 | 8.34 | 14.19 | 8.18 |
| LSTM | 2.08 | 4.99 | 7.85 | 12.88 | 7.99 |

- **The known-plan model wins in every fold and at every lead time.** At 1 hour it
  is near the noise floor of a voltage reading (0.2-point steps plus sag).
- **Almost all the skill comes from knowing the load.** So the low-power mode
  forecasts from its own plan.
- **The LSTM barely trained.** Early stopping chose epoch 1–2 in four folds, so it
  learnt little more than "no change". It had about 10 independent days per fold
  and a voltage target. It isn't used.

**Fitted parameters are stable across folds:**

| Parameter | Folds | All data |
| --- | --- | --- |
| Capacity (Wh per 100 points) | 2,100–2,400 | 2,200 |
| Sag (points per A) | 0.6–0.9 | 0.9 |
| Clear-sky solar `k` (W) | 195–240 | 236 |
| Average cloud factor | 0.60–0.69 | 0.63 |

**Not yet measured:**
- **Bias direction.** Error grows about 0.35 points per hour, which suggests a
  steady capacity or solar bias. Signed bias was added to `scout-evaluate` after
  this run, so a fresh run will show the direction.
- **Solar exponent.** `γ` came out at 1.5, the top of the old search grid; the grid
  now goes to 2.5.

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
- **At the last waypoint:** hold (loiter), or drift and motor back.

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
- The simulator crashed when there was no `params.json` (it now uses defaults, as
  the service does).

**Recovery, under systemd** (journals in `docs/evidence/`):
- **Autopilot killed mid-mission:** it saw the heartbeat loss at +5 s and the link
  back at +16 s. It kept the stored mission and re-armed at +20 s. AUTO was accepted
  at +61 s, once ArduPilot's EKF was ready. The service itself never restarted.
- **`kill -9` of the service:** systemd restarted it in 5 s.
- **A hung service (`SIGSTOP`):** the 30 s watchdog restarted it.

**A race the CI runner exposed, fixed:** an autopilot reboot shorter than the 5 s
heartbeat timeout is only visible from the boot clock. If heartbeats arrived first,
the service recorded the rebooted autopilot as "disarmed on purpose" and didn't
re-arm. It now holds the armed state after a reconnect until the boot clock arrives.

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

## Open issues

1. **Speed over ground vs through the water.** ArduPilot's speed target is over
   ground; the model's power and ETA assume speed through the water. In one 20×
   simulation run (deb install, `--arrive-in 4`), a following current of about
   0.45 m/s gave 0.7 m/s over ground for 0.2–0.3 m/s through the water.
   - **Power:** the boat used far less than planned. The battery rose 71 → 82 %
     while the forecast was 7.6 points pessimistic at 1 hour.
   - **Arrival:** SITL's hull tops out at about 0.8 m/s through the water. The
     planner's 1.2 m/s was unreachable, and the boat finished about 4 hours late.
     It reported this itself ("deadline can't be met … arrives 4.2 h late").
   - **Fix:** plan in through-water speed, using a current estimate (SOG minus
     commanded speed, or a current feed). Learn the reachable top speed from
     telemetry.
2. **Defaults when there's no fitted model.** Without `/etc/scout/params.json` the
   service uses built-in defaults: 1,900 Wh, 1.2 points per A and γ = 1. These are
   further from the fitted values (2,200 Wh, 0.9, 1.5) than they need to be. Making
   the defaults the published fitted values would help any install without the
   private data.
3. **State of charge.** Capacity is in Wh per 100 points of a voltage reading.
   Calibrating it needs a coulomb-counted charge–discharge cycle and the battery
   chemistry. If the battery is LiFePO4, voltage barely moves across most of its
   charge.

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
`scripts/quickstart.sh`.
