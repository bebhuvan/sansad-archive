#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
engine_dir="${project_dir}/.engines/liteparse-2.10.1"

if ! command -v uv >/dev/null 2>&1; then
  echo "Install uv first: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

if [[ ! -d "${engine_dir}/.git" ]]; then
  mkdir -p "$(dirname "${engine_dir}")"
  git clone --depth 1 --branch v2.10.1 https://github.com/run-llama/liteparse.git "${engine_dir}"
fi

cd "${engine_dir}/ocr/paddleocr"
uv sync
echo "PaddleOCR environment is ready."
echo "Start it with: scripts/start_paddleocr.sh"

