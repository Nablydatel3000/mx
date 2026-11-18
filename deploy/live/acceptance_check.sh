#!/bin/sh
# Protected stdin carries the existing local credential; never an argv or temp file.
set -eu
if [ "$(id -u)" -ne 0 ]; then
    echo 'Use sudo gldrubf-live-check.'
    exit 1
fi
if [ -L /etc/gldrubf-live-acceptance/trading-token ] || [ ! -f /etc/gldrubf-live-acceptance/trading-token ]; then
    echo 'A regular private acceptance credential is required.'
    exit 1
fi
if [ "$(stat -c %u /etc/gldrubf-live-acceptance/trading-token)" != 0 ] || [ "$(stat -c %a /etc/gldrubf-live-acceptance/trading-token)" != 600 ]; then
    echo 'Acceptance credential must be root-owned 0600.'
    exit 1
fi
cd /opt/gldrubf-live-acceptance/mx
ACTION=${1:-check}
if [ "$#" -gt 1 ]; then echo 'Only one action argument is allowed.'; exit 1; fi
case "$ACTION" in
    check|accounts) ;;
    *) echo 'Only check or accounts is allowed.'; exit 1 ;;
esac
exec runuser -u gldrubf-live-check -- /usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8 PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m src.gldrubf_bot.live_acceptance "$ACTION" --credential-stdin < /etc/gldrubf-live-acceptance/trading-token
