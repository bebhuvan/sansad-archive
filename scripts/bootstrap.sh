#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv_dir="${project_dir}/.venv"

python3 -m venv "${venv_dir}"
"${venv_dir}/bin/pip" install --upgrade pip
"${venv_dir}/bin/pip" install -r "${project_dir}/requirements.txt"
"${venv_dir}/bin/sansad-pipeline" init
"${venv_dir}/bin/python" -m unittest discover -s "${project_dir}/tests" -v

echo "Ready. Run ${venv_dir}/bin/sansad-pipeline doctor"

