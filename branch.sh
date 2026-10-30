#!/bin/sh
set -eu
cd "$(dirname "$0")"
if command -v uv >/dev/null 2>&1; then
    exec uv run python scripts/edition_history.py "$@"
elif [ -x .venv/bin/python ]; then
    exec .venv/bin/python scripts/edition_history.py "$@"
else
    exec python3 scripts/edition_history.py "$@"
fi
