#!/bin/sh
set -e

# Stale socket survives container restart (lives in writable layer, not
# wiped between starts), so fcgiwrap would fail with "Address in use".
rm -f /tmp/fcgiwrap.sock

# Socket in /tmp (1777). The apk package's /run/fcgiwrap is owned by
# fcgiwrap:www-data — the nginx user we run as can't write there.
fcgiwrap -s unix:/tmp/fcgiwrap.sock &

# Wait for the socket so nginx isn't racing fcgiwrap on the first request.
while [ ! -S /tmp/fcgiwrap.sock ]; do sleep 0.1; done

exec nginx -g 'daemon off;'
