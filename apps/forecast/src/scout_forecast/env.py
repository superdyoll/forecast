"""Settings from the environment or a gitignored ``.env`` file (shell wins).

Shared by every Scout app, so none of them needs the CSV loader just to read
its configuration. There is never a built-in data path.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

CSV_ENV = "SCOUT_TELEMETRY_CSV"
_dotenv_dir: Path | None = None  # directory of the .env that was loaded
_dotenv_keys: set = set()  # variables that came from .env, not the shell
_searched = False  # load_dotenv() has run (found a file or not)


def find_dotenv() -> Path | None:
    """Nearest .env above the current directory, else above the running script."""
    starts = [Path.cwd()]
    if sys.argv and sys.argv[0]:
        starts.append(Path(sys.argv[0]).resolve().parent)
    for start in starts:
        for d in (start, *start.parents):
            if (d / ".env").is_file():
                return d / ".env"
    return None


def load_dotenv(path: str | Path | None = None) -> Path | None:
    """Load KEY=VALUE lines from a .env file into os.environ.

    Variables already set in the environment win. Supports blank lines,
    `# comments`, an optional `export ` prefix and quoted values. Returns
    the file loaded, or None.
    """
    global _dotenv_dir, _searched
    _searched = True
    f = Path(path) if path else find_dotenv()
    if f is None or not f.is_file():
        return None
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key[7:].strip() if key.startswith("export ") else key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value
            _dotenv_keys.add(key)
    _dotenv_dir = f.parent.resolve()
    return f


def csv_default() -> str | None:
    """$SCOUT_TELEMETRY_CSV from the shell or .env, or None. Never a built-in path.

    A relative path from .env is relative to the .env file; one from the
    shell is relative to the current directory.
    """
    if not _searched:
        load_dotenv()
    v = os.environ.get(CSV_ENV)
    if v and CSV_ENV in _dotenv_keys and _dotenv_dir and not Path(v).is_absolute():
        v = str(_dotenv_dir / v)
    return v or None


def state_dir(arg: str | None = None) -> Path:
    """The low-power service's state directory: `arg` (--state-dir), else
    $SCOUT_STATE_DIR, $STATE_DIRECTORY (systemd; the first of a ':' list) or
    ./lowpower-state. Shared by scout-lowpower and scout-web."""
    v = arg or os.environ.get("SCOUT_STATE_DIR") or os.environ.get("STATE_DIRECTORY")
    return Path((v or "lowpower-state").split(":")[0])


def csv_arg(ap: argparse.ArgumentParser) -> None:
    """Add --csv. Defaults to $SCOUT_TELEMETRY_CSV from the environment or a .env.

    There is no built-in path. A relative path in .env is taken relative to
    the .env file; one from the shell is relative to the current directory.
    """
    default = csv_default()
    ap.add_argument(
        "--csv",
        default=default,
        required=not default,
        help=f"raw Tether CSV export (default: ${CSV_ENV}, also read from .env)",
    )
