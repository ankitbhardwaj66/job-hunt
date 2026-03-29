#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# Create venv if it doesn't exist
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
    source "$VENV_DIR/bin/activate"
    pip install -r "$SCRIPT_DIR/requirements.txt"
    playwright install chromium
else
    source "$VENV_DIR/bin/activate"
fi

# Pass all arguments through (e.g. --login, --search)
python "$SCRIPT_DIR/linkedin_prospector.py" "$@"
