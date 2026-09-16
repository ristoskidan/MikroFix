#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "Ta namestitev je namenjena Linuxu."
  exit 1
fi

echo "MikroFIX v30 R9 Linux - priprava okolja"
echo "Ciljna platforma: LMDE 7 / Debian 13 (amd64)"

sudo apt update
sudo apt install -y \
  python3 python3-venv python3-tk python3-dev \
  build-essential pkg-config \
  cups cups-client libcups2 \
  usbmuxd libimobiledevice-utils \
  libsecret-tools gnome-keyring dbus-x11 \
  xdg-utils

python3 - <<'PY'
import platform, struct, sys
if sys.version_info[:2] != (3, 13) or struct.calcsize("P") != 8:
    raise SystemExit(
        f"Potreben je Python 3.13 64-bit; najden {platform.python_version()} "
        f"({'64' if struct.calcsize('P') == 8 else '32'}-bit)."
    )
PY

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install --no-cache-dir -r requirements-linux.txt

if [[ ! -f mikrofix_settings.json ]]; then
  cp -- mikrofix_settings_linux.example.json mikrofix_settings.json
  chmod 600 mikrofix_settings.json
fi

systemctl is-active --quiet cups || sudo systemctl enable --now cups
sudo systemctl enable usbmuxd >/dev/null 2>&1 || true

chmod 700 .
chmod 700 zazeni_mikrofix_linux.sh zazeni_mikrofix_linux_E2E_TEST.sh preveri_mikrofix_linux.sh

echo
echo "Namestitev je končana."
echo "Naslednji korak: ./preveri_mikrofix_linux.sh"
