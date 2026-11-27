# scout-web

A page comparing the forecast with what happened. It reads the state files
`scout-lowpower` writes (`decision.json`, `history.jsonl`) and, if a telemetry
CSV is configured, forecasts past periods with `scout-forecast`. Standard
library server, no external assets, binds to 127.0.0.1 by default.

Command: `scout-web`. Tests: `python -m unittest discover -s apps/web/tests`.
