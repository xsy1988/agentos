#!/usr/bin/env bash
# 数据库每日备份：pg_dump（docker 容器内执行）→ gzip → 按保留天数轮转。
# 用法：scripts/backup_db.sh [保留份数，默认 14]
# crontab 示例（每天 04:30）：30 4 * * * /Users/hg/工作/项目demo/Agent平台/scripts/backup_db.sh >> /Users/hg/工作/项目demo/Agent平台/logs/backup.log 2>&1
set -euo pipefail

KEEP="${1:-14}"

# 软链解析（与 bin/agentos 同款）：保证从任意路径/软链调用时 ROOT 正确
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [ "${SOURCE#/*}" = "$SOURCE" ] && SOURCE="$DIR/$SOURCE"
done
ROOT="$(cd -P "$(dirname "$SOURCE")/.." && pwd)"

BACKUP_DIR="$ROOT/backups"
mkdir -p "$BACKUP_DIR"

# 容器名/库/用户与 docker-compose.yml 保持一致（容器名固定 agent-platform-db）
DB_CONTAINER="agent-platform-db"
DB_USER="agent"
DB_NAME="agent_platform"

STAMP="$(date +%Y%m%d_%H%M%S)"
OUT="$BACKUP_DIR/${DB_NAME}_${STAMP}.sql.gz"

# 数据目录（解析产物 backend/data/parse）一并打包：文档正文不在库里，恢复时缺它则
# 知识库检索失效
DATA_ARCHIVE="$BACKUP_DIR/data_${STAMP}.tar.gz"

echo "[$(date '+%F %T')] backup start"
docker exec "$DB_CONTAINER" pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$OUT"
if [ -d "$ROOT/backend/data" ]; then
  tar -czf "$DATA_ARCHIVE" -C "$ROOT/backend" data
fi
echo "[$(date '+%F %T')] wrote $OUT ($(du -h "$OUT" | cut -f1))"

# 轮转：按文件名时间戳排序，超出保留份数删除最旧
ls -1t "$BACKUP_DIR"/${DB_NAME}_*.sql.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r f; do
  rm -f "$f"
  echo "[$(date '+%F %T')] rotated out $f"
done
ls -1t "$BACKUP_DIR"/data_*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r f; do
  rm -f "$f"
done
echo "[$(date '+%F %T')] backup done"
