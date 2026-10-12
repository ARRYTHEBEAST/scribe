#!/bin/bash
# Double-click to start Scribe (first run sets everything up).
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/python scribe.py "$@"
