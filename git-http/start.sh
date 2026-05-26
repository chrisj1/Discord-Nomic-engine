#!/bin/sh
set -e

mkdir -p /run/nginx

# Socket in /tmp (1777, world-writable). The apk package creates
# /run/fcgiwrap owned by fcgiwrap:www-data, which root can't write to
# under cap_drop: ALL (no DAC_OVERRIDE).
fcgiwrap -s unix:/tmp/fcgiwrap.sock &

# Wait for the socket so nginx isn't racing fcgiwrap on the first request.
while [ ! -S /tmp/fcgiwrap.sock ]; do sleep 0.1; done
chmod 666 /tmp/fcgiwrap.sock

exec nginx -g 'daemon off;'
