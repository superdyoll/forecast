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


def parse_iso(s: str) -> datetime:
    """ISO 8601, 'Z' allowed; naive times are UTC. Always returns UTC."""
    t = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    if t.tzinfo is None:
        return t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc)


def parse(spec: str):
    """(simulated start, real unix time, rate) from 'ISO@UNIXxRATE'.
    Raises ValueError for anything else, or a rate that isn't positive."""
    sim, at, rest = spec.partition("@")
    real, _, rate = rest.partition("x")
    try:
        if not at:
            raise ValueError("no '@'")
        t, r = parse_iso(sim), float(rate or 1.0)
        if not r > 0:
            raise ValueError(f"rate must be positive, not {rate}")
        return t, float(real), r
    except ValueError as e:
        raise ValueError(
            f"bad ${ENV} {spec!r} ({e}); expected e.g. "
            f"2025-07-23T06:00:00+00:00@1764190000.0x20 (scout-sim clock prints one)"
        ) from None


def _current():
    """The parsed $SCOUT_CLOCK (cached while it doesn't change), or None."""
    global _spec
    raw = os.environ.get(ENV)
    if not raw:
        return None
    if _spec is None or _spec[0] != raw:
        _spec = (raw, parse(raw))
    return _spec[1]


def now() -> datetime:
    c = _current()
    if c is None:
        return datetime.now(timezone.utc)
    sim0, real0, r = c
    return sim0 + timedelta(seconds=(time.time() - real0) * r)


def rate() -> float:
    c = _current()
    return c[2] if c else 1.0
