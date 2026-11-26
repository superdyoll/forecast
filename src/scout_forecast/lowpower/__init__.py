"""Low-power mission mode: forecast energy, decide how Scout operates, command
ArduPilot over MAVLink. Standard library only.

  planner  - pure decision logic (testable without an autopilot)
  mission  - operator mission file and geometry
  mavlink  - minimal MAVLink v2 codec and self-reconnecting links
  service  - the boat-side service and the `scout-lowpower` command
"""
