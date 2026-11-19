#!/bin/bash
# Non-trading transient unit only. No service enable/start, arm, resume or cancel.
set -euo pipefail
if [ "$#" -ne 1 ]; then echo '{"category":"one_maintenance_command_required"}'; exit 78; fi
case "$1" in
    credential-check|diagnose|sync|preflight) ;;
    *) echo '{"category":"nontrading_commands_only"}'; exit 78 ;;
esac
if [ "$(id -u)" -ne 0 ]; then echo '{"category":"root_required_for_credential_delivery_only"}'; exit 78; fi
if systemctl is-active --quiet gldrubf-live.service; then
    echo '{"category":"trading_service_active_no_maintenance_started"}'; exit 78
fi
python3 - <<'CREDENTIAL_METADATA_CHECK'
import os,pwd,stat
from pathlib import Path
try:
    assert pwd.getpwnam('gldrubf-live').pw_uid!=0
    folder=Path('/etc/gldrubf-live');secret=folder/'trading-token'
    d=folder.lstat();s=secret.lstat()
    assert stat.S_ISDIR(d.st_mode) and d.st_uid==0 and not d.st_mode&0o022
    assert stat.S_ISREG(s.st_mode) and s.st_uid==0 and s.st_nlink==1 and stat.S_IMODE(s.st_mode)==0o600
except Exception:
    print('{"category":"master_credential_or_directory_policy_failed"}')
    raise SystemExit(78)
CREDENTIAL_METADATA_CHECK
if ! runuser -u gldrubf-live -- test -r /etc/gldrubf-live/config.json || \
   ! runuser -u gldrubf-live -- test -r /opt/gldrubf-live/mx/src/gldrubf_bot/live_credential_entrypoint.py; then
    echo '{"category":"service_config_or_code_access_denied"}'; exit 78
fi
exec systemd-run --quiet --wait --pipe --collect --service-type=exec \
    --unit=gldrubf-live-maintenance \
    --property=User=gldrubf-live --property=Group=gldrubf-live \
    --property=WorkingDirectory=/opt/gldrubf-live/mx \
    --property=LoadCredential=trading-token:/etc/gldrubf-live/trading-token \
    --property=RuntimeDirectory=gldrubf-live-maintenance \
    --property=RuntimeDirectoryMode=0700 --property=UMask=0077 \
    --property=NoNewPrivileges=yes --property=ProtectSystem=strict \
    --property=ProtectHome=yes --property=PrivateTmp=yes \
    --property=ReadWritePaths=/var/lib/gldrubf-live \
    --setenv=PYTHONDONTWRITEBYTECODE=1 \
    /usr/bin/python3 -m src.gldrubf_bot.live_credential_entrypoint "$1"
