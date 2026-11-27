"""Low-power mission mode: forecast energy, decide how Scout operates, command
ArduPilot over MAVLink. Standard library plus the scout-forecast energy model.

  planner  - pure decision logic (testable without an autopilot)
  mission  - operator mission file and geometry
  mavlink  - minimal MAVLink v2 codec and self-reconnecting links
  service  - the boat-side service: telemetry, decisions, commands, recovery
  cli      - the `scout-lowpower` command (run, set-mission, status, plan)
"""

__version__ = "0.1.0"
