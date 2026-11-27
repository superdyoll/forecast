"""``python -m scout_forecast`` runs the forecast command."""

import sys

from .forecast import main

sys.exit(main())
