#!/bin/sh
# Started as root: make the data folder belong to PUID:PGID, then run as that user.
# Started as another user (compose `user:`): just check the data folder is writable.
set -e
DATA="${GRAVYBOAT_DATA:-/data}"

if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA"
  if [ "$(stat -c %u:%g "$DATA")" != "$PUID:$PGID" ]; then
    chown -R "$PUID:$PGID" "$DATA"
  fi
  exec gosu "$PUID:$PGID" "$0" "$@"
fi

if [ ! -w "$DATA" ]; then
  echo "Gravy Boat can't write to $DATA (running as uid $(id -u))." >&2
  echo "Fix the folder's owner on the host, or drop 'user:' and set PUID/PGID instead." >&2
  exit 1
fi

# Bring the database schema up to date, then serve. Runs once per container start,
# before the gunicorn workers exist, so there is no race between workers.
flask --app "app:create_app()" db upgrade
exec gunicorn -w 2 -b 0.0.0.0:8794 --no-control-socket --access-logfile - "app:create_app()"
