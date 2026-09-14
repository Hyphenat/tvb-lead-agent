#!/usr/bin/env bash
# Local verification: lint then the full test suite.
set -u
cd "$(dirname "$0")/.."
echo "=== ruff ==="
python3 -m ruff check src app tests 2>&1 | tail -5
echo "=== pytest ==="
python3 -m pytest -q 2>&1 | tail -20
