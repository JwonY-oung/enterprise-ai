#!/bin/bash
# 备份容器入口：按 BACKUP_INTERVAL_SECONDS 周期执行 backup.sh
set -euo pipefail
chmod +x /usr/local/bin/backup.sh
INTERVAL="${BACKUP_INTERVAL_SECONDS:-21600}"
echo "[backup-loop] interval=${INTERVAL}s"
while true; do
  /usr/local/bin/backup.sh || echo "[backup-loop] backup failed; will retry next cycle"
  sleep "$INTERVAL"
done
