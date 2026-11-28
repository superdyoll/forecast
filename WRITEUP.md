# Scout low-power mode: write-up

Detail, tables and how each figure was made: [REPORT.md](REPORT.md). To run
it: [README.md](README.md).

**Where the energy goes** (17-day telemetry, medians on a 15-minute grid):
idle "hotel" load **9.9 W** (not under way, no Starlink session); Starlink
**+22 W** (the same states with and without a session); propulsion in AUTO
**+60 W** at cruise, flattening at **~73 W** (~0.85 m/s through the water);
MODE_17 **+4.5 W**; losses **~2 W** (solar − load − battery). Solar averages
**72 W** when producing, peaking at **285 W**. Propulsion grows with the cube
of speed, so speed is the main lever, then Starlink.

**What surprised us in the data, and what we did:**
- `battery_pct` is exactly 20 × V − 400: a voltage, not stored energy. It sags
  under load and sticks at 100 % at 25 V. The model forecasts the reading,
  including sag, with capacity fitted in Wh per 100 points.
- The "thruster" channel is the whole boat's load (at night it matches the
  battery discharge within 0.07 A). So the hotel load is measurable.
- Most solar and battery-current readings arrive only during Starlink sessions,
  and Starlink stops for good on 07-27. Gaps are kept as gaps, not zeros.
- Iridium flushes queued rows out of order, and two autopilot restarts zero
  every field. Both are flagged. The restarts became real cases for the
  failure tests.

**How good the forecast is:** on blocked, purged time folds (fitted without
the test window), with the plan known: **1.0 / 2.2 / 3.5 / 5.0 points** at
1 / 3 / 6 / 12 h, against 1.8 / 4.7 / 8.2 / 12.4 for "stays the same". Nearly
all of the skill comes from knowing the load, so the service forecasts from its
own plan. Trust it for 6–12 h under a given plan and for comparing plans. Don't
trust it below ~28 % (no data there), as a state-of-charge gauge, or for
weather: it assumes recent cloud persists. A Keras LSTM did worse than "stays
the same" and isn't used.

**What the mode decides:** every 5 minutes, and at once on events, it forecasts
each option and picks one. The options are each cruise speed (0.3–1.2 m/s over
ground, each leg priced with the estimated current), Starlink normal or
minimal, and hold, drift or pause. Feasible means never below reserve + 5
points. With a deadline it takes the feasible speed that arrives in time with
the most battery to spare: **the reserve outranks the deadline**. If nothing is
feasible it pauses with the motors off to recharge. On station it holds if
affordable, else drifts with the motors off until it is. Every decision and its
reasons go to journald and the web page.

**Where each part runs:**

| Part | Runs on | Why |
| --- | --- | --- |
| `scout-lowpower`, the service | the boat | It has to work with no shore link. It reads a local mission file, keeps state on disk and talks MAVLink to ArduPilot. |
| `scout-web`, the page | served on the boat, read in a browser on the boat's network | The boat's own data and decisions. Bound to localhost by default, so it needs an SSH tunnel or a LAN setting. |
| `scout-sim`, SITL | a shore or dev machine | Simulation only, never on the boat. |
| Fitting and evaluation | ashore | Done with the private CSV; the fit ships as `params.json`. |

- **Backend:** Python 3.9 standard library only, so the boat needs no pip,
  no network installs and nothing that could break on an upgrade. That
  includes the MAVLink codec, checked byte for byte against pymavlink, and
  `http.server`.
- **Frontend:** one static HTML file with no external assets or build step,
  so it works with no internet.
- **systemd:** `Type=notify` with a 30 s watchdog and `Restart=always`.
- **Link down or autopilot dropped:** the shore link isn't needed. If the
  autopilot link drops, the service reconnects with backoff. When the
  autopilot returns it resyncs, checks the stored mission and re-arms
  (journals in `docs/evidence/`).

**What we chose not to build:**
- weather or ocean forecasts. Positions and dates are shifted, so the public
  data can't be matched to this deployment; cloud and current are inferred
  from the boat's own readings instead.
- payload scheduling beyond Starlink duty.
- a fleet or shore dashboard.
- authentication on the page; it's bound to localhost instead.
- state of charge by coulomb counting.
- a custom SITL hull. The ArduPilot 4.0 hull can't hold Scout's speeds, so energy
  and decisions are judged in `scout-sim fast`, and SITL for MAVLink and recovery.

**Measure next on a real boat:**
- current for each consumer;
- speed through the water;
- solar on a fixed schedule, not only in Starlink sessions;
- a coulomb-counted charge–discharge cycle, plus the battery chemistry;
- commanded against achieved speed.
