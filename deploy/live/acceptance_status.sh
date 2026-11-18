#!/bin/sh
set -eu
if [ "$(id -u)" -ne 0 ]; then
    echo 'Use sudo gldrubf-live-status.'
    exit 1
fi
cd /opt/gldrubf-live-acceptance/mx
exec runuser -u gldrubf-live-check -- /usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8 PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m src.gldrubf_bot.live_acceptance status "$@"
