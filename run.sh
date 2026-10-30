#!/bin/sh

cd "$(dirname "$0")" || exit 1
if command -v uv >/dev/null 2>&1; then
    run_python() { uv run python -u "$@"; }
elif [ -x .venv/bin/python ]; then
    run_python() { .venv/bin/python -u "$@"; }
else
    run_python() { python3 -u "$@"; }
fi

language=${1:-eng}
if [ "$language" = por ]; then
    date=${2:-all}
    status=0
    run_python execute.py --lang por download --date "$date" || status=$?
    run_python execute.py --lang por sanitize --date "$date" || status=$?
    run_python execute.py --lang por missing || status=$?
    exit "$status"
elif [ "$language" != eng ]; then
    echo "Usage: ./run.sh [eng|por] [YYYY-MM|all]" >&2
    exit 2
fi

# Run the script twice just to make sure we didn't miss any downloads
run_python execute.py download --date all
run_python execute.py download --date all
run_python execute.py missing --fill
run_python execute.py sanitize --date all
