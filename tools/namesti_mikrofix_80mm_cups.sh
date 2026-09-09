#!/bin/sh

set -eu

APP_DIR=/home/mikrofix/MikroFIX_Service_v30_R9_LINUX_DEV
FILTER_SOURCE="$APP_DIR/tools/mikrofix-pdftoescpos"
PPD_SOURCE="$APP_DIR/tools/MikroFix-TM-T20II.ppd"
FILTER_TARGET=/usr/lib/cups/filter/mikrofix-pdftoescpos
BACKUP_DIR=/home/mikrofix/MikroFixData_V30_SANDBOX/system-backups
DEVICE_URI='usb://EPSON/TM-T20II?serial=544338595145690000'

mkdir -p "$BACKUP_DIR"
if [ -f /etc/cups/ppd/TM-T20II.ppd ]; then
    install -m 0644 /etc/cups/ppd/TM-T20II.ppd "$BACKUP_DIR/TM-T20II.pred-mikrofix-pdf.ppd"
fi
install -o root -g root -m 755 "$FILTER_SOURCE" "$FILTER_TARGET"
/usr/sbin/lpadmin -p TM-T20II -E -v "$DEVICE_URI" -P "$PPD_SOURCE"

printf '\nNastavljen je TM-T20II z MikroFix PDF/ESC-POS filtrom.\n'
printf 'V Ctrl+P izberi TM-T20II in 100%% merilo.\n'
/usr/bin/lpstat -p TM-T20II -l
