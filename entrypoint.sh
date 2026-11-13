#!/bin/sh
set -eu
umask 077
: "${PRINTER_HOST:?Set PRINTER_HOST}"
: "${PRINTER_DRIVER:?Set PRINTER_DRIVER to a CUPS driver identifier}"
: "${CUPS_QUEUE:=printer}"
: "${PRINTER_PORT:=9100}"
case "$CUPS_QUEUE" in *[!A-Za-z0-9_-]*|'') echo 'Invalid CUPS_QUEUE' >&2; exit 1;; esac
case "$PRINTER_PORT" in *[!0-9]*|'') echo 'Invalid PRINTER_PORT' >&2; exit 1;; esac
mkdir -p /data/app /data/cups/config /data/cups/spool /data/cups/state /data/cups/cache
if [ ! -f /data/cups/config/cupsd.conf ]; then cp -a /etc/cups/. /data/cups/config/; fi
# Move existing application files into the app-owned directory, preserving data.
for item in pages prints documents settings.json page-order.json; do
  if [ -e "/data/$item" ] && [ ! -e "/data/app/$item" ]; then mv "/data/$item" "/data/app/$item"; fi
done
chmod 0755 /data /data/cups
chown -R appuser:lp /data/app
chmod 0700 /data/app
chown -R root:lp /data/cups
chmod 0710 /data/cups/spool
chmod 0770 /data/cups/cache
chmod 0755 /data/cups/state
sed -i '/^[[:space:]]*\(ServerRoot\|RequestRoot\|StateDir\|CacheDir\|TempDir\|AccessLog\|ErrorLog\|PageLog\)[[:space:]]/d' /data/cups/config/cups-files.conf
cat >> /data/cups/config/cups-files.conf <<'CONFIG'
ServerRoot /data/cups/config
RequestRoot /data/cups/spool
StateDir /data/cups/state
CacheDir /data/cups/cache
TempDir /data/cups/cache
AccessLog none
ErrorLog stderr
PageLog none
CONFIG
sed -i 's|^Listen /run/cups/cups.sock|Listen /data/cups/state/cups.sock|' /data/cups/config/cupsd.conf
export CUPS_SERVER=/data/cups/state/cups.sock
export DATA_DIR=/data/app
export TMPDIR=/data/cups/cache
/usr/sbin/cupsd -c /data/cups/config/cupsd.conf -s /data/cups/config/cups-files.conf
n=0
until lpstat -r >/dev/null 2>&1; do
  n=$((n+1)); if [ "$n" -ge 10 ]; then echo 'Print scheduler did not start' >&2; exit 1; fi
  sleep 1
done
if lpstat -p "$CUPS_QUEUE" >/dev/null 2>&1; then
  lpadmin -p "$CUPS_QUEUE" -v "socket://$PRINTER_HOST:$PRINTER_PORT"
  if [ -f /data/cups/driver ] && [ "$(cat /data/cups/driver)" != "$PRINTER_DRIVER" ]; then
    lpadmin -p "$CUPS_QUEUE" -m "$PRINTER_DRIVER"
  fi
else
  lpadmin -p "$CUPS_QUEUE" -E -v "socket://$PRINTER_HOST:$PRINTER_PORT" -m "$PRINTER_DRIVER"
fi
printf '%s\n' "$PRINTER_DRIVER" > /data/cups/driver
python /app/cups_control.py &
exec gunicorn --bind 0.0.0.0:8080 --user appuser --group lp --workers 1 --threads 4 --worker-tmp-dir /data/cups/cache --timeout 210 app:app
