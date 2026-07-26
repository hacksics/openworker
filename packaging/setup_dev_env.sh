#!/usr/bin/env bash
# One-time dev bootstrap for a fresh checkout: creates the Python venv every
# from-source flow expects at .venv — the browser dev flow runs its
# openworker-server directly, and the Tauri desktop shell falls back to it when
# no packaged sidecar binary is present (src-tauri/src/lib.rs, resolution step 3).
#
# Usage: bash packaging/setup_dev_env.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/.venv"
MIN_PY="3.10" # keep in sync with requires-python in pyproject.toml

supports_min() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null
}

# Pick the interpreter deliberately instead of trusting a bare `python3`, which is just
# whatever is first on PATH — on macOS that is still the system 3.9. It satisfies
# `python3 -m venv` and only fails later at `pip install` ("requires a different Python"),
# so the run aborts having already created a .venv that looks bootstrapped but has no
# packages in it. Override with PYTHON=/path/to/python3.12.
PY=""
if [ -n "${PYTHON:-}" ]; then
  if ! supports_min "$PYTHON"; then
    echo "error: PYTHON=$PYTHON is older than $MIN_PY." >&2
    exit 1
  fi
  PY="$PYTHON"
else
  # `python3` first so an already-modern default wins; then newest-to-oldest.
  for candidate in python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
    if command -v "$candidate" >/dev/null 2>&1 && supports_min "$candidate"; then
      PY="$candidate"
      break
    fi
  done
fi
if [ -z "$PY" ]; then
  echo "error: no Python >= $MIN_PY on PATH (tried python3, python3.10-3.14)." >&2
  echo "       Install one (macOS: brew install python@3.12; Debian/Ubuntu:" >&2
  echo "       apt install python3.12-venv) or set PYTHON=/path/to/python3." >&2
  exit 1
fi
echo "Using $("$PY" -V) ($(command -v "$PY"))"

# Rebuild in place if a previous run (or an older system Python) left a venv that can't
# install this package — otherwise `venv` reuses it and the install fails the same way
# every time. Untouched when it's already usable, so re-running is cheap.
if [ -x "$VENV/bin/python" ] && ! supports_min "$VENV/bin/python"; then
  echo "Rebuilding $VENV — its Python is older than $MIN_PY."
  "$PY" -m venv --clear "$VENV"
else
  "$PY" -m venv "$VENV"
fi

# The coworker package (server, engine, connectors) + inbound-messaging extras.
# aisuite comes in as a regular dependency (git-pinned in pyproject.toml until
# the next PyPI release).
"$VENV/bin/python" -m pip install --quiet --upgrade pip
"$VENV/bin/python" -m pip install --quiet -e "$ROOT[messaging,dev]"

"$VENV/bin/python" -c 'import aisuite, coworker' # fail loudly if the wiring broke
echo "Ready: $VENV"
echo "  server: $VENV/bin/openworker-server --cwd /path/to/your/project --port 8765"
