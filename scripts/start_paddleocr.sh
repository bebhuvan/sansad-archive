#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
server_dir="${project_dir}/.engines/liteparse-2.10.1/ocr/paddleocr"

if [[ ! -d "${server_dir}" ]]; then
  echo "Run scripts/setup_paddleocr.sh first." >&2
  exit 1
fi

cd "${server_dir}"
exec uv run server.py

