#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if [[ ! -x .venv/bin/python ]]; then
  echo "Okolje ni nameščeno. Najprej zaženi: bash namesti_mikrofix_linux.sh"
  exit 1
fi

unset MIKROFIX_ALLOW_FURS_PRODUCTION
export MIKROFIX_CONFIG="${PWD}/mikrofix_settings.json"
exec .venv/bin/python mikrofix.py
