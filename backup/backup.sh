#!/bin/bash
# 数据库 + 上传文件备份脚本（在 backup 容器内运行）
# 环境变量：BACKUP_KEEP=保留份数(默认 10)；PGHOST/PGUSER/PGDATABASE 由容器提供。
set -euo pipefail

PGHOST="${PGHOST:-postgres}"
PGUSER="${PGUSER:-knowledge}"
PGDATABASE="${PGDATABASE:-knowledge_base}"
BACKUP_KEEP="${BACKUP_KEEP:-10}"
BACKUP_DIR="/backups"

STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p "$BACKUP_DIR"

echo "[backup] starting ${STAMP}"

# 1. PostgreSQL 压缩备份
DB_FILE="$BACKUP_DIR/knowledge_base-$STAMP.sql.gz"
pg_dump -h "$PGHOST" -U "$PGUSER" -d "$PGDATABASE" --no-owner | gzip > "$DB_FILE"
echo "[backup] database -> $DB_FILE ($(du -h "$DB_FILE" | cut -f1))"

# 2. 上传文件备份（若挂载了上传目录）
if [ -d /app/data/uploads ] && [ -n "$(ls -A /app/data/uploads 2>/dev/null)" ]; then
  UPLOAD_FILE="$BACKUP_DIR/uploads-$STAMP.tar.gz"
  tar -czf "$UPLOAD_FILE" -C /app/data/uploads .
  echo "[backup] uploads   -> $UPLOAD_FILE ($(du -h "$UPLOAD_FILE" | cut -f1))"
fi

# 3. 清理旧备份，保留最近 BACKUP_KEEP 份
ls -1t "$BACKUP_DIR"/knowledge_base-*.sql.gz 2>/dev/null | tail -n +"$((BACKUP_KEEP+1))" | xargs -r rm -f
ls -1t "$BACKUP_DIR"/uploads-*.tar.gz 2>/dev/null | tail -n +"$((BACKUP_KEEP+1))" | xargs -r rm -f

echo "[backup] done ${STAMP}"
