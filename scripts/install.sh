#!/bin/sh
# Thin POSIX entry point; all installation and safety logic lives in Python.
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
if [ -n "${SECOND_BRAIN_PYTHON:-}" ]; then
    python_exe=$SECOND_BRAIN_PYTHON
elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python" ]; then
    python_exe=$VIRTUAL_ENV/bin/python
else
    python_exe=python3
fi
if ! command -v "$python_exe" >/dev/null 2>&1; then
    echo "Python 3.11–3.13 required. Activate a virtual environment or set SECOND_BRAIN_PYTHON." >&2
    exit 1
fi
exec "$python_exe" "$script_dir/install_toolkit.py" "$@"
