"""Operator mission: what to do, and the constraints the service must respect.

Stored as JSON on the boat, so the service works without the shore link:

    {"kind": "transit", "waypoints": [[60.1, 2.2], [60.3, 2.4]],
     "arrive_by": "2025-07-25T12:00Z", "min_reserve_pct": 30}

    {"kind": "station", "station": [60.3, 2.4], "radius_m": 50,
     "drift_radius_m": 500, "hold_until": "2025-07-28T00:00Z",
     "min_reserve_pct": 30}

`arrive_by` / `hold_until` are optional. Reserve is in Tether's battery_pct
(20 x V - 400), the number operators see.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

EARTH_R = 6371000.0


@dataclass
class Mission:
    kind: str  # "transit" or "station"
    waypoints: list = field(default_factory=list)  # [[lat, lon], ...]
    station: list | None = None  # [lat, lon]
    radius_m: float = 50.0  # loiter radius when holding station
    drift_radius_m: float = 500.0  # how far it may drift before returning
    arrive_by: datetime | None = None
    hold_until: datetime | None = None
    min_reserve_pct: float = 30.0
    arrival_radius_m: float = 30.0

    def __post_init__(self):
        if self.kind not in ("transit", "station"):
            raise ValueError("kind must be 'transit' or 'station'")
        if self.kind == "transit" and not self.waypoints:
            raise ValueError("a transit mission needs at least one waypoint")
        if self.kind == "station" and not self.station:
            raise ValueError("a station mission needs 'station': [lat, lon]")
        for lat, lon in self.waypoints + ([self.station] if self.station else []):
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError(f"bad position {lat}, {lon}")
        if not 0 <= self.min_reserve_pct < 100:
            raise ValueError("min_reserve_pct must be 0-100")
        if self.drift_radius_m < self.radius_m:
            raise ValueError("drift_radius_m must be >= radius_m")

    @property
    def goal(self) -> tuple[float, float]:
        return (
            tuple(self.station) if self.kind == "station" else tuple(self.waypoints[-1])
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("arrive_by", "hold_until"):
            d[k] = d[k].isoformat() if d[k] else None
        return d

    @property
    def id(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:12]

    @classmethod
    def from_dict(cls, d: dict) -> Mission:
        d = dict(d)
        for k in ("arrive_by", "hold_until"):
            if d.get(k):
                d[k] = parse_time(d[k])
        d["waypoints"] = [list(map(float, w)) for w in d.get("waypoints", [])]
        if d.get("station"):
            d["station"] = list(map(float, d["station"]))
        known = cls.__dataclass_fields__
        unknown = set(d) - set(known)
        if unknown:
            raise ValueError(f"unknown mission fields: {sorted(unknown)}")
        return cls(**d)


def parse_time(s: str) -> datetime:
    t = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    return (
        t.replace(tzinfo=timezone.utc)
        if t.tzinfo is None
        else t.astimezone(timezone.utc)
    )


def load(path: str | Path) -> Mission:
    return Mission.from_dict(json.loads(Path(path).read_text()))


def save(m: Mission, path: str | Path) -> None:
    write_atomic(path, json.dumps(m.to_dict(), indent=2))


def write_atomic(path: str | Path, text: str) -> None:
    """Write via a temp file + rename, so a crash never leaves half a file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# ----------------------------------------------------------------- geometry
def distance_m(a, b) -> float:
    """Great-circle distance between (lat, lon) points."""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (
        math.sin((la2 - la1) / 2) ** 2
        + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    )
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(h)))


def remaining_m(m: Mission, pos, next_wp: int) -> float:
    """Distance left: to waypoint `next_wp` (0-based), then along the rest."""
    if m.kind == "station":
        return distance_m(pos, m.station)
    i = min(max(next_wp, 0), len(m.waypoints) - 1)
    d = distance_m(pos, m.waypoints[i])
    for a, b in zip(m.waypoints[i:], m.waypoints[i + 1 :]):
        d += distance_m(a, b)
    return d


def legs(m: Mission, pos, next_wp: int) -> list:
    """The route still to go as [(length m, (unit north, unit east)), ...]:
    from `pos` to waypoint `next_wp`, then along the rest. Directions use a
    local flat-earth approximation, fine over a leg of a few km."""
    pts = [pos] + list(m.waypoints[min(max(next_wp, 0), len(m.waypoints) - 1) :])
    out = []
    for a, b in zip(pts, pts[1:]):
        n = math.radians(b[0] - a[0]) * EARTH_R
        e = math.radians(b[1] - a[1]) * EARTH_R * math.cos(math.radians(a[0]))
        norm = math.hypot(n, e)
        if norm > 0.5:
            out.append((distance_m(a, b), (n / norm, e / norm)))
    return out
