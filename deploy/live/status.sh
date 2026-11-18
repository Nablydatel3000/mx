#!/bin/sh
set -eu
cd /opt/gldrubf-live/mx
exec /usr/bin/python3 -m src.gldrubf_bot.live status "$@"
