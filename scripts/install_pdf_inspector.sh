#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv_dir="${project_dir}/.venv"
tag="v0.7.0"

if [[ ! -x "${venv_dir}/bin/python" ]]; then
  echo "Create ${venv_dir} and install requirements.txt first." >&2
  exit 1
fi

build_dir="$(mktemp -d)"
trap 'rm -rf "${build_dir}"' EXIT

"${venv_dir}/bin/pip" install "maturin>=1.7,<2"
git clone --depth 1 --branch "${tag}" https://github.com/firecrawl/pdf-inspector.git "${build_dir}/pdf-inspector"
mkdir -p "${build_dir}/wheels"
(
  cd "${build_dir}/pdf-inspector"
  "${venv_dir}/bin/maturin" build --release --out "${build_dir}/wheels"
)
"${venv_dir}/bin/pip" install --force-reinstall "${build_dir}"/wheels/*.whl
echo "Installed pdf-inspector ${tag} from source."

