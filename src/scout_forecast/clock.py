"""The time the service plans with. Real UTC unless $SCOUT_CLOCK says otherwise.

Simulations set SCOUT_CLOCK so the service and the simulator agree on a
simulated date (e.g. July sun at 60 N in November) and rate:

    SCOUT_CLOCK="2025-07-23T06:00:00+00:00@1764190000.0x1"
                 simulated start          real unix   rate

means: at real unix time 1764190000.0 it was 2025-07-23 06:00Z, and simulated
time runs at 1x. `scout-sim clock --start ...` prints a value to export.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

ENV = "SCOUT_CLOCK"
_spec = None


def make(start: datetime, rate: float = 1.0, real: float | None = None) -> str:
    real = time.time() if real is None else real
    return f"{start.astimezone(timezone.utc).isoformat()}@{real:.3f}x{rate:g}"


def parse(spec: str):
    sim, _, rest = spec.partition("@")
    real, _, rate = rest.partition("x")
    t = datetime.fromisoformat(sim.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t, float(real), float(rate or 1.0)


def now() -> datetime:
    global _spec
    raw = os.environ.get(ENV)
    if not raw:
        return datetime.now(timezone.utc)
    if _spec is None or _spec[0] != raw:
        _spec = (raw, parse(raw))
    sim0, real0, rate = _spec[1]
    return sim0 + timedelta(seconds=(time.time() - real0) * rate)


def rate() -> float:
    raw = os.environ.get(ENV)
    return parse(raw)[2] if raw else 1.0
