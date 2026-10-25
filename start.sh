#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PY="${FORKSYNC_PYTHON:-}"
if [[ -z "$PY" ]]; then
  if [[ -x ".venv/bin/python" ]]; then PY=".venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then PY="python3"
  elif command -v python >/dev/null 2>&1; then PY="python"
  else
    echo "[x] 找不到 python3，请安装 Python 3.9+ 或设置 FORKSYNC_PYTHON"
    exit 1
  fi
fi

echo "[*] 解释器: $PY"
exec "$PY" app.py --port "${PORT:-8770}" "$@"
