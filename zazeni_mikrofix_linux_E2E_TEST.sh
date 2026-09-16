#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if [[ ! -x .venv/bin/python ]]; then
  echo "Okolje ni nameščeno. Najprej zaženi: bash namesti_mikrofix_linux.sh"
  exit 1
fi

unset MIKROFIX_ALLOW_FURS_PRODUCTION
export MIKROFIX_CONFIG="${PWD}/mikrofix_settings_e2e_test.json"
export MIKROFIX_E2E_TEST=YES
export MIKROFIX_SKIP_FILE_PROTECTION=1

.venv/bin/python pripravi_v30_e2e_test.py
exec .venv/bin/python mikrofix.py
