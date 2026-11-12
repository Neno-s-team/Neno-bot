#!/bin/sh
# One command to run Neno on Linux / macOS: creates venv, installs dependencies, then runs run.py.
set -e
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
if [ ! -x venv/bin/python ]; then
    echo "[1/2] Creating virtual environment..."
    "$PY" -m venv venv
fi
if ! cmp -s requirements.txt venv/.requirements 2>/dev/null; then
    echo "[2/2] Installing dependencies (first run only)..."
    venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt
    cp requirements.txt venv/.requirements
fi
exec venv/bin/python run.py "$@"
