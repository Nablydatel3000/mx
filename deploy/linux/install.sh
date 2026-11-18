#!/bin/bash
# Run explicitly on the user's server. This script is never executed by Codex.
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
    echo 'Use sudo bash deploy/linux/install.sh on your own Linux server.'
    exit 1
fi
command -v systemctl >/dev/null
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required; Ubuntu 24.04 recommended"'
if systemctl is-active --quiet gldrubf-paper.service; then
    echo 'Stop gldrubf-paper.service before changing software. Keep its database.'
    exit 1
fi
SOURCE_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
if ! id gldrubf >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /var/lib/gldrubf --shell /usr/sbin/nologin gldrubf
fi
install -d -m 0755 /opt/gldrubf/mx
# Software only: never copy .env, tests, logs or databases from the checkout.
(cd "$SOURCE_ROOT" && tar --exclude='__pycache__' --exclude='*.pyc' --exclude='.env' --exclude='.env.*' -cf - src) | (cd /opt/gldrubf/mx && tar -xf -)
chown -R root:root /opt/gldrubf/mx
find /opt/gldrubf/mx -type d -exec chmod 0755 {} +
find /opt/gldrubf/mx -type f -exec chmod 0644 {} +
install -d -m 0700 -o gldrubf -g gldrubf /var/lib/gldrubf /var/log/gldrubf
cd /opt/gldrubf/mx
python3 -m src.gldrubf_bot.vps configure
chgrp gldrubf /etc/gldrubf /etc/gldrubf/paper.json
for UNIT_FILE in gldrubf-paper.service gldrubf-health.service gldrubf-health.timer gldrubf-backup.service gldrubf-backup.timer; do
    install -m 0644 "$SOURCE_ROOT/deploy/linux/$UNIT_FILE" "/etc/systemd/system/$UNIT_FILE"
done
systemctl daemon-reload
echo 'Files installed. Services are NOT enabled or started.'
echo 'If resuming Windows PAPER, restore its stopped SQLite BEFORE first start and use identical costs.'
echo 'Then: sudo systemctl enable --now gldrubf-paper.service gldrubf-health.timer gldrubf-backup.timer'
