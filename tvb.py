#!/usr/bin/env python
"""Zero-install launcher.

The package lives under ``src/`` so that the test suite imports the same way a
published install would.  That layout means ``python -m tvb_agent.cli`` only
works once the project is installed, which is a poor first five minutes for
anyone who just cloned the repo.  This launcher puts ``src`` on the path and
hands straight over to the CLI, so the project runs immediately:

    python tvb.py doctor
    python tvb.py run --target 15 --json leads.json --csv leads.csv -v
    python tvb.py audit leads.json

``pip install -e .`` still works and gives you the ``tvb-agent`` command; this
is simply the path that needs no install step at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tvb_agent.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
