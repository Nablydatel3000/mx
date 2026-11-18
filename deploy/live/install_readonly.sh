#!/bin/bash
# Preparation only. Installs no systemd unit, permission or LIVE ledger. Never run by Codex.
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
    echo 'Run with sudo on your own VPS.'
    exit 1
fi
command -v runuser >/dev/null
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required"'
if command -v systemctl >/dev/null && systemctl is-active --quiet gldrubf-live.service; then
    echo 'Existing trading LIVE service is active. No installation or automatic stop performed.'
    exit 1
fi
SOURCE_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
for ACCEPTANCE_TARGET in /opt/gldrubf-live-acceptance/mx /etc/gldrubf-live-acceptance/config.json /var/lib/gldrubf-live-acceptance /usr/local/bin/gldrubf-live-check /usr/local/bin/gldrubf-live-status; do
    if [ -e "$ACCEPTANCE_TARGET" ] || [ -L "$ACCEPTANCE_TARGET" ]; then
        echo 'Existing acceptance files/commands found. Refusing overwrite; preserve reports and configuration.'
        exit 1
    fi
done
# Ask for configuration before creating any permanent installation files.
# This private temporary directory contains account/configuration only, NEVER a token.
ACCEPTANCE_STAGING=$(mktemp -d /tmp/gldrubf-acceptance-install.XXXXXX)
trap 'rm -rf -- "$ACCEPTANCE_STAGING"' EXIT
(cd "$SOURCE_ROOT" && python3 -m src.gldrubf_bot.live_acceptance configure --config "$ACCEPTANCE_STAGING/config.json")
if ! id gldrubf-live-check >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/gldrubf-live-acceptance --shell /usr/sbin/nologin gldrubf-live-check
fi
install -d -m 0755 /opt/gldrubf-live-acceptance/mx
(cd "$SOURCE_ROOT" && tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.env' --exclude='.env.*' -cf - src) | (cd /opt/gldrubf-live-acceptance/mx && tar -xf -)
chown -R root:root /opt/gldrubf-live-acceptance/mx
find /opt/gldrubf-live-acceptance/mx -type d -exec chmod 0755 {} +
find /opt/gldrubf-live-acceptance/mx -type f -exec chmod 0644 {} +
install -d -m 0750 -o root -g gldrubf-live-check /etc/gldrubf-live-acceptance
install -d -m 0700 -o gldrubf-live-check -g gldrubf-live-check /var/lib/gldrubf-live-acceptance
install -m 0640 -o root -g gldrubf-live-check "$ACCEPTANCE_STAGING/config.json" /etc/gldrubf-live-acceptance/config.json
install -m 0755 "$SOURCE_ROOT/deploy/live/acceptance_check.sh" /usr/local/bin/gldrubf-live-check
install -m 0755 "$SOURCE_ROOT/deploy/live/acceptance_status.sh" /usr/local/bin/gldrubf-live-status
echo 'Read-only acceptance files installed. No systemd changes, LIVE DB, permission or API requests.'
echo 'Next: install the credential locally, then sudo gldrubf-live-check.'
