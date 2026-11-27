"""``python -m scout_lowpower`` runs the scout-lowpower command."""

import sys

from .service import main

sys.exit(main())
