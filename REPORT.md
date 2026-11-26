# Scout energy forecast: what we've learned so far

Status as of 2026-11-26. Covers the telemetry analysis, the cleaning pipeline
(`scout_data.py`) and a first forecasting experiment (`model/train_lstm.py`).
All numbers come from `scout_telemetry_2025-07.csv` (17 days, 24,674 messages).

## Summary

- **The battery percentage in Tether is a voltage reading, not a state of charge.**
  It is exactly `20 × V − 400` (20 V reads 0 %, 25 V reads 100 %). It drops the moment
  load increases and sticks at 100 % while the charger holds 25 V.
- **The channel Tether labels "thruster" measures the whole boat's load,** not just
  the thruster. At night it matches battery discharge to within 0.07 A. Battery,
  solar and this load channel account for each other to within about 2 W. That means
  we can measure total energy in and out, but we can't split the load between
  consumers directly.
- **A Keras LSTM doesn't beat a naive forecast.** Averaged over 5 folds, its error
  is 7.99 battery-% points against 7.69 for "battery stays the same". Early stopping
  chose epoch 1–2 in four of the five folds. Don't use it for decisions.
- **Recommendation:** forecast energy in watt-hours with a simple physical model
  (idle load, plus load as a function of speed, minus solar as a function of time of
  day) fitted to this data. Use machine learning, at most, to correct its errors.

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
supplies the whole load. Positive battery current means charging.

| State | Load (A) | Load (W) | Speed over ground (m/s) | Rows |
| --- | --- | --- | --- | --- |
| Disarmed, idle | 0.2 | ≈ 5 | 0 | many (Iridium) |
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
- **What we can't separate yet:** the computer, radios (Starlink duty cycle) and
  payloads. They are all inside the idle 5–9 W and the load channel. Measuring them is
  on the list below.

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

## Questions for the Scout team

- What is the battery chemistry and capacity in Wh? If it is LiFePO4, its voltage barely
  changes across most of its charge, and voltage-based SoC is close to meaningless.
- Is the "thruster" channel a shunt on the main load bus? Is solar measured before or
  after the charge controller?
- What is mode 17?
- Is Starlink deliberately run in sessions to save power? What does it draw when on?

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
python3 scout_data.py scout_telemetry_2025-07.csv --summary
pip install -r model/requirements.txt
python3 model/train_lstm.py scout_telemetry_2025-07.csv   # writes model/runs/<timestamp>/results.json
```

The run reported here is `model/runs/20261126T192216Z` (default settings, seed 0).
