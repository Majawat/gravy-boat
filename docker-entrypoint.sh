#!/bin/sh
# Bring the database schema up to date, then serve. Runs once per container start,
# before the gunicorn workers exist, so there is no race between workers.
set -e
flask --app "app:create_app()" db upgrade
exec gunicorn -w 2 -b 0.0.0.0:8794 --access-logfile - "app:create_app()"
