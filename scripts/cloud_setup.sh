#!/usr/bin/env bash
# Idempotent environment setup for the weekly run (cloud VM: Ubuntu; also works in local Git Bash).
# Ensures .venv runs Python 3.11 with the pinned requirements and the package installed.
# Last stdout line on success: PYTHON=<venv interpreter>. Everything else goes to stderr. Never reads .env.
set -euo pipefail
cd "$(dirname "$0")/.."

venv_python() {
  if [ -x .venv/bin/python ]; then echo .venv/bin/python
  elif [ -x .venv/Scripts/python.exe ]; then echo .venv/Scripts/python.exe
  fi
}
is_311() { "$1" -c "import sys; assert sys.version_info[:2] == (3, 11)" 2>/dev/null; }
up_to_date() {   # package importable and no pinned requirement missing or at another version
  "$1" -c "import tradingbot" 2>/dev/null &&
    ! "$1" -m pip install --dry-run --no-deps -q -r requirements.txt 2>/dev/null | grep -q "Would install"
}

PY="$(venv_python || true)"
if [ -n "$PY" ] && is_311 "$PY" && up_to_date "$PY"; then
  echo "PYTHON=$PY"
  exit 0
fi

if [ -z "$PY" ] || ! is_311 "$PY"; then
  rm -rf .venv
  if command -v uv >/dev/null 2>&1; then
    uv python install 3.11 >&2
    uv venv --seed --python 3.11 .venv >&2
  elif command -v py >/dev/null 2>&1; then
    py -3.11 -m venv .venv >&2
  else
    python3.11 -m venv .venv >&2
  fi
  PY="$(venv_python)"
fi

"$PY" -m pip install -q -r requirements.txt >&2
"$PY" -m pip install -q -e . --no-deps >&2
is_311 "$PY" && "$PY" -c "import tradingbot"
echo "PYTHON=$PY"
