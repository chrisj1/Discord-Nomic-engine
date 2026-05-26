#!/bin/sh
set -e

mkdir -p /run/nginx /run/fcgiwrap

fcgiwrap -s unix:/run/fcgiwrap/fcgiwrap.sock &

# Wait for the socket so nginx isn't racing fcgiwrap on the first request.
while [ ! -S /run/fcgiwrap/fcgiwrap.sock ]; do sleep 0.1; done
chmod 666 /run/fcgiwrap/fcgiwrap.sock

exec nginx -g 'daemon off;'
