# scout-lowpower

The boat service. It forecasts energy with `scout-forecast`, decides how Scout
operates to meet an arrival time or keep a battery reserve, and commands
ArduPilot over MAVLink. Standard library plus `scout-forecast`.

Command: `scout-lowpower run / set-mission / status / plan`.
Tests: `python -m unittest discover -s apps/lowpower/tests`.
