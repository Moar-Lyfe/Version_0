#!/usr/bin/env bash
# Start the Executive Dashboard on macOS or Linux.
#
#   ./run.sh              start on http://localhost:8501
#   ./run.sh --port 9000  start on another port
#
# Creates .venv and installs dependencies on first run; afterwards it just
# launches. Nothing is installed system-wide.
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR=".venv"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python 3.10+ is required but '$PYTHON_BIN' was not found." >&2
  echo "Install it from https://www.python.org/downloads/ and try again." >&2
  exit 1
fi

if [ ! -d "$VENV_DIR" ]; then
  echo "First run: creating virtual environment in $VENV_DIR ..."
  "$PYTHON_BIN" -m venv "$VENV_DIR"
  "$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null
  "$VENV_DIR/bin/python" -m pip install -r requirements.txt
fi

if [ ! -f "config/config.yaml" ]; then
  echo "No config/config.yaml yet -- copying the example."
  cp config/config.example.yaml config/config.yaml
  echo "Edit config/config.yaml to point 'data.sources' at your workbooks."
fi

if [ ! -d "data/sample" ] || [ -z "$(ls -A data/sample 2>/dev/null)" ]; then
  echo "Generating sample workbooks so the dashboard has something to show ..."
  "$VENV_DIR/bin/python" tools/generate_sample_data.py || true
fi

echo "Starting the dashboard. Press Ctrl+C to stop."
exec "$VENV_DIR/bin/python" -m streamlit run app/main.py "$@"
