#!/usr/bin/env bash
# setup.sh — Dev environment bootstrap for klaxxon
#
# Creates and populates the project virtualenv, installs aetos MCP tools,
# and configures pre-commit hooks. Run once after cloning the repo.

set -euo pipefail

VENV_DIR=".venv"
PYTHON="${VENV_DIR}/bin/python"
PIP="${VENV_DIR}/bin/pip"

echo "==> Creating virtualenv (${VENV_DIR})..."
if [ ! -d "${VENV_DIR}" ]; then
  python -m venv "${VENV_DIR}"
else
  echo "    (already exists, skipping)"
fi

echo "==> Installing app dependencies..."
"${PIP}" install -r requirements.txt

echo "==> Installing aetos (MCP extra)..."
"${PIP}" install "aetos[mcp] @ git+https://gitlab.aelab.io/iat-pub/aetos.git"

echo "==> Installing pre-commit hooks..."
"${VENV_DIR}/bin/pre-commit" install --install-hooks

echo ""
echo "✓ Setup complete!"
echo "  Python : $(${PYTHON} --version)"
echo "  aetos  : $(${PYTHON} -c 'import aetos; print(aetos.__version__)')"
echo ""
echo "Activate your venv with:  source ${VENV_DIR}/bin/activate"
