# Scout energy forecast: what we've learned so far

Status as of 2026-11-26. Covers the telemetry analysis, the cleaning pipeline
(`scout_data.py`), an LSTM experiment (`model/lstm/`) and the physical
energy-balance model that replaces it (`model/physical/`, run with `./forecast`).
All numbers come from `scout_telemetry_2025-07.csv` (17 days, 24,674 messages).

## Summary

- **The battery percentage in Tether is a voltage reading, not a state of charge.**
  It is exactly `20 × V − 400` (20 V reads 0 %, 25 V reads 100 %). It drops the moment
  load increases and sticks at 100 % while the charger holds 25 V.
- **The channel Tether labels "thruster" measures the whole boat's load,** not just
  the thruster. At night it matches battery discharge to within 0.07 A. Battery,
  solar and this load channel account for each other to within about 2 W. That means
  we can measure total energy in and out. Consumers can only be separated by
  comparing operating states.
- **Starlink costs about 22 W, about twice the rest of the idle boat (about 10 W).**
  This is why it runs in sessions.
- **A Keras LSTM doesn't beat a naive forecast.** Averaged over 5 folds, its error
  is 7.99 battery-% points against 7.69 for "battery stays the same". Early stopping
  chose epoch 1–2 in four of the five folds. Don't use it for decisions.
- **Recommendation:** forecast energy in watt-hours with a simple physical model
  (idle load, plus load as a function of speed, minus solar as a function of time of
  day) fitted to this data. Use machine learning, at most, to correct its errors.
- **That physical model is now built and works.** Given the operating plan, its error
  is 3.11 points across the 12-hour horizon (4.87 at 12 h), against 7.69 for "stays the
  same" and 7.99 for the LSTM. It wins in every fold.

## Problems in the data

| Finding | Evidence | How we handle it |
| --- | --- | --- |
| `battery_pct` = 20·V − 400 | Linear fit error 0.02 points over 24,587 rows; jumps about 1.2 points when the thruster starts (0.06 V of sag) | Don't treat it as stored energy (see recommendations) |
| "Thruster" channel = total load | Night-time battery − "thruster" current: +0.07 to −0.03 A in every load band | Treat it as total load; idle draw is the hotel load |
| Starlink is up only in sessions | Only 25 % of 15-minute steps before 07-27 have a Starlink row; Starlink stops entirely at 07-27 02:05Z | Solar and battery current are visible only about 20 % of the time; modelled as missing, not zero |
| Iridium sends fewer fields | No solar, battery current or thruster voltage; speed rounded to 0.2 m/s | Load power uses battery voltage |
| Iridium backlog flushes | 8 messages within 6 s at 07-14 07:39, out of order, speeds up to 34 m/s; the boat really moved about 30 km (probably a tow out to the site) | Flagged `backlog`; left out of time-series work |
| Autopilot restarts | Mode 16 (starting up) with zeroed fields at 07-25 19:38 and 07-27 19:00 | Flagged `reboot`; zeros set to missing |
| Placeholder values | Globalstar `battery_pct` always 0; position 0,0; voltage 0 | Set to missing |
| Units | `temperature_c` is in hundredths of a degree (3120 = 31.2 °C) | Divided by 100 |
| Mode 17 | 3,728 rows; not a standard ArduPilot Rover mode | Labelled `MODE_17`; meaning to be confirmed |
| Message timestamps | `created_at` is when Tether received the message, not when it was measured | Matters only for backlogs; resampled to 15 minutes |

## Where the energy goes (first estimates)

These come from night-time Starlink rows, with solar current under 0.05 A, so the battery
supplies the whole load. Positive battery current means charging. Because these rows
were sent over Starlink, Starlink was on, so each figure includes its draw of about 22 W
(see below).

| State | Load (A) | Load (W) | Speed over ground (m/s) | Rows |
| --- | --- | --- | --- | --- |
| Disarmed, Starlink off (07-14, before deployment) | 0.2 | ≈ 5 | 0 | Iridium rows |
| Armed, little or no thrust | 0.38 | 9 | 0.39 | 70 |
| Low thrust | 1.31 | 29 | 0.56 | 1,301 |
| Medium thrust | 2.91 | 66 | 0.79 | 416 |
| High thrust | 4.62 | 104 | 1.02 | 700 |

- **Load rises steeply with speed.** Going from 0.56 to 1.02 m/s takes about 3.5× the
  power. So running slower is the strongest lever the low-power mode has. Speed over
  ground includes current drift, so the true relationship with speed through the water
  needs current data.
- **Solar:** mean 72 W when producing (median 60 W, peak 285 W at the panels).
- **Losses:** solar − load − battery averages 2 W. That means the measured solar
  figure is close to what actually reaches the battery.
- **Starlink is the biggest consumer after propulsion.** Median load on the 15-minute
  grid, with and without a Starlink session in that step:

  | State | Iridium only | Starlink session | Difference |
  | --- | --- | --- | --- |
  | Disarmed | 10.8 W | 30.5 W | +20 W |
  | Armed, MANUAL | 9.4 W | 32.8 W | +23 W |
  | AUTO | 70.3 W | 93.2 W | +23 W |
  | MODE_17 | 14.5 W | 54.3 W | (thrust differs) |

  At the start of a session the load steps up by a median of 18 W (7 sessions, boat
  disarmed). The physical model fits about 10 W for the hotel load (computer, Iridium,
  sensors) and about 22 W for Starlink.
- **MODE_17 draws light, uneven thrust,** with a median of 15 W against 70 W in AUTO.
  That is consistent with station-keeping. To be confirmed with the Scout team.
- **What we still can't separate:** the computer from the payloads inside the
  ≈10 W hotel load.

## LSTM forecasting experiment

**Setup.** The model takes the last 24 hours on a 15-minute grid and predicts the change
in `battery_pct` over each of the next 12 hours. Inputs:

- battery %, battery voltage, load power
- speed over ground
- solar power, plus a flag for whether solar was reported
- armed and under-way flags
- time of day

It is one LSTM layer of 32 units, with dropout, a small weight penalty, Huber loss and
early stopping, giving 1,458 windows in total.

**Validation.** 5-fold cross-validation over contiguous ~3-day blocks of time. Training
windows that overlap a test window are removed, and inputs are scaled using each fold's
training data only. The model is compared against two baselines:

- **Stays the same:** no change from the last reading.
- **Same as yesterday:** repeat the change seen 24 hours earlier.

**Mean error over folds (battery-% points; lower is better):**

| Method | 1 h | 3 h | 6 h | 12 h | All |
| --- | --- | --- | --- | --- | --- |
| LSTM | 2.08 | 4.99 | **7.85** | 12.88 | 7.99 |
| Stays the same | **1.84** | **4.66** | 8.18 | **12.40** | **7.69** |
| Same as yesterday | 2.09 | 4.76 | 8.34 | 14.19 | 8.18 |

**By fold (error over all lead times):**

| Fold | Test window | Conditions | LSTM | Stays the same | Same as yesterday |
| --- | --- | --- | --- | --- | --- |
| 0 | 07-15 → 07-18 | under way 99 % | **8.89** | 8.99 | 11.49 |
| 1 | 07-18 → 07-21 | under way 79 % | 10.61 | 11.34 | **8.65** |
| 2 | 07-21 → 07-24 | under way 57 % | 7.41 | **6.83** | 10.84 |
| 3 | 07-24 → 07-27 | under way 19 % | 5.44 | 5.54 | **4.87** |
| 4 | 07-27 → 07-30 | drifting, Iridium only | 7.61 | 5.74 | **5.06** |

**Why it fails:**

1. **The target is voltage.** About 1.8 points of error at 1 hour is load switching on
   and off (voltage sag) and voltage quantisation, not energy. No model can predict that
   from past data alone.
2. **It barely trains.** The best epoch was 1, 1, 14, 2 and 6 across the folds, and 2
   for the final model. A network stopped that early outputs nearly "no change", so it
   tracks the "stays the same" baseline.
3. **Little data.** About 7,000 weights against roughly 10 independent days of training
   per fold.
4. **Solar is missing most of the time.** It is visible in about 20 % of training steps
   and 2 % of fold 4's test window.
5. **The last fold is a regime the model never trained on:** drifting with no solar
   readings. That is where the LSTM is worst.

**What we'd trust it for:** nothing yet. Errors at 12 hours (about 12–13 points) match
the typical 12-hour swing (8–18 points). "Same as yesterday" winning three folds shows
there is a usable day/night signal for a better model to pick up.

## Recommendations

1. **Forecast energy, not voltage.** Model battery power (solar − load − about 2 W) and
   add it up into watt-hours. Convert to SoC using the battery's real capacity. Where
   voltage has to be used, remove the sag using an internal resistance estimated from
   load steps.
2. **Build a physical energy-balance model as the main forecast:**
   - idle load (5–9 W),
   - plus load as a function of speed, fitted from the table above,
   - minus solar as a function of sun elevation (latitude and time) scaled by a cloud
     factor from weather data or recent solar readings.

   Each part is fitted from the Starlink sessions and can be explained in one line. It
   still works when only Iridium is up. It can also answer "what if we slow down?",
   which is what the low-power mode needs to decide.
3. **Use the LSTM, if at all, only to correct the physical model's errors,** with planned
   speed and mode as known future inputs. Keep reporting both baselines next to it.
4. **Validate on time blocks with overlap removed,** as now, and report error at 1, 6,
   12 and 24 hours.
5. **Correct the labels downstream.** Treat the "thruster" channel as total load and
   `battery_pct` as a voltage reading in every tool and display that uses them.

## Physical energy-balance model

`model/physical/energy.py` uses the standard library only, so it can run on the boat.
It simulates the battery on a 15-minute grid:

- **Load:** the recorded load-channel value when the operating plan is known (the
  low-power mode sets its own plan). For a blind forecast, the mean load of the last
  6 hours. The load channel is reported over Iridium too, so it is known throughout the
  deployment.
- **Solar:** a clear-sky curve, `k · sin(sun elevation)^γ`, from the boat's position and
  a fitted clock offset, multiplied by a cloud factor. The factor is the observed /
  clear-sky ratio over the last 24 hours, relaxing to the training average over about
  12 hours. Solar fits sun elevation best with the clock offset between −1 h and 0 h,
  so the shifted times still line up with the shifted positions.
- **Battery:** a "rested" level that moves by net energy divided by capacity, plus an
  immediate sag (or rise) proportional to battery current. Capacity and sag are fitted
  by minimising forecast error on the training data. A rough first estimate from
  night-time drain is about 1.9 kWh per 100 points of `battery_pct`, with about
  1.2 points of sag per amp.

### Results (17 days, 5 folds, 12-hour horizon)

The model is checked on exactly the same blocked, purged folds and 1,458 windows as the
LSTM (`python3 -m model.physical.evaluate`, run `20261126T194630Z`). Every parameter
is refitted on each fold's training data.

**Mean error over folds (battery-% points; lower is better):**

| Method | 1 h | 3 h | 6 h | 12 h | All |
| --- | --- | --- | --- | --- | --- |
| **Physical, known load plan** | **0.96** | **2.07** | **3.30** | **4.87** | **3.11** |
| Physical, blind (hold last 6 h load) | 1.65 | 3.45 | 6.25 | 11.84 | 6.44 |
| Stays the same | 1.84 | 4.66 | 8.18 | 12.40 | 7.69 |
| Same as yesterday | 2.09 | 4.76 | 8.34 | 14.19 | 8.18 |
| LSTM | 2.08 | 4.99 | 7.85 | 12.88 | 7.99 |

**By fold (error over all lead times):**

| Fold | Test window | Conditions | Known plan | Blind | Stays the same |
| --- | --- | --- | --- | --- | --- |
| 0 | 07-15 → 07-18 | under way 99 % | **3.25** | 6.03 | 8.99 |
| 1 | 07-18 → 07-21 | under way 79 % | **4.49** | 10.37 | 11.34 |
| 2 | 07-21 → 07-24 | under way 57 % | **3.56** | 7.80 | 6.83 |
| 3 | 07-24 → 07-27 | under way 19 % | **2.62** | 5.96 | 5.54 |
| 4 | 07-27 → 07-30 | drifting, Iridium only | **1.62** | 2.01 | 5.74 |

**What the results show:**

- **With the operating plan known, the model cuts error by 60 %.** Overall error is 3.11
  against 7.69 for "stays the same" and 7.99 for the LSTM. At 12 hours it is 4.87
  against 12.40. It wins in every fold and at every lead time. At 1 hour (0.96 points)
  it is close to the noise floor of the voltage-based reading: 0.2-point steps plus
  sag.
- **Error grows roughly in line with lead time,** about 0.35 points per hour. That
  pattern suggests a steady energy bias (in capacity or solar) rather than random
  noise. `evaluate.py` now reports a signed bias for each method, so the next run will
  show the direction.
- **The blind forecast is only modestly better than "stays the same"** (6.44 vs 7.69),
  and is worse in folds 2 and 3 when the boat changes what it is doing. Almost all of
  the skill comes from knowing the load. **The low-power mode should therefore forecast
  from its own planned schedule.** The blind figure is roughly what to expect when the
  plan is unknown.
- **The best fold is the hardest one for the LSTM.** Fold 4 (drifting, Iridium only,
  no solar readings) scores 1.62. The model uses the clock and sun position, and falls
  back to the average cloud factor when solar isn't reported.
- **Fold 1 is the weakest** (4.49; 7.29 at 12 hours). The boat was under way 79 % of the
  time, with large energy swings, so any error in solar or capacity is magnified.

**The fitted parameters are stable across folds,** which suggests the physics holds
rather than being overfitted:

| Parameter | Range over folds | All data |
| --- | --- | --- |
| Capacity (Wh per 100 points of `battery_pct`) | 2,100–2,400 | 2,200 |
| Sag (points per A) | 0.6–0.9 | 0.9 |
| Clear-sky solar scale `k` (W) | 195–240 | 236 |
| Clock offset (h) | −0.75 to 0 | −0.5 |
| Average cloud factor | 0.60–0.69 | 0.63 |
| Losses (W) | 1.8–2.2 | 2.0 |

The solar exponent `γ` came out at 1.5 in every fold, which was the top of the search
grid. The grid now runs up to 2.5; check the next run. On the one-week test file the
wider grid changed the error by only +0.05 points.

**Load components fitted on all data** (medians on the 15-minute grid, Starlink off
unless stated):

| Component | Value | How it's measured |
| --- | --- | --- |
| Hotel load | 9.9 W | Not under way, no Starlink session (742 steps) |
| Starlink | +22.0 W | Same states, with vs without a session |
| AUTO propulsion | +60 W | AUTO minus hotel (263 steps) |
| MODE_17 propulsion | +4.5 W | MODE_17 minus hotel (307 steps): almost no thrust |
| AUTO propulsion vs speed over ground | ≈ 9 W below 0.4 m/s, 48 W at 0.5, 67–73 W at 0.7–1.3 | 0.2 m/s bins |

The propulsion curve flattens at about 70 W. That suggests a throttle limit, with any
speed above about 0.7 m/s coming from current rather than thrust. The low-power mode's
"what if we slow down" estimate needs speed through the water, not over ground.

**What we'd trust it for:**

- Forecasting the battery 6–12 hours ahead under a given plan (about ±3–5 points), and
  comparing plans with each other.
- Not for guaranteeing a hard reserve near empty without a safety margin. The model
  is linear in voltage and has only been checked between about 28 % and 100 %.
- Not for blind forecasts beyond a few hours.

## Questions for the Scout team

- What is the battery chemistry and capacity in Wh? If it is LiFePO4, its voltage barely
  changes across most of its charge, and voltage-based SoC is close to meaningless.
- Is the "thruster" channel a shunt on the main load bus? Is solar measured before or
  after the charge controller?
- What is mode 17?
- Is Starlink deliberately run in sessions to save power? The data says about 22 W
  when on. Does that match its specification?

## What we'd want measured on a real boat

- Per-consumer current: computer, Starlink, Iridium and payloads, separate from the
  thruster.
- A full charge–discharge cycle with coulomb counting, to get usable capacity and
  calibrate SoC.
- Speed through the water, or a current estimate, to separate thruster power from
  drift.
- Solar on a fixed schedule, not only during Starlink sessions, plus a cloud or
  irradiance reference.

## Reproducing

```sh
# From the repo root. The CSV is never built in: pass --csv or set the variable.
cp .env.example .env   # then set SCOUT_TELEMETRY_CSV (a shell export overrides it)
python3 scout_data.py --summary
./forecast --from 2025-07-23T00:00Z --hours 24          # physical model, stdlib only
python3 -m model.physical.evaluate                       # k-fold; writes ./runs/physical/<timestamp>/
pip install -r model/lstm/requirements.txt
python3 -m model.lstm.train_lstm                         # writes ./runs/lstm/<timestamp>/
python3 -m model.physical.evaluate --lstm-results runs/lstm/<timestamp>/results.json
```

The runs reported here are LSTM `20261126T192216Z` and physical `20261126T194630Z`
(default settings). They were written under `model/*/runs/` by earlier versions of the
scripts.
