#!/bin/bash
# 备份脚本：MySQL + Redis + 配置文件
# 用法：bash backup.sh
set -euo pipefail

DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/backup/resource_web"
mkdir -p "$BACKUP_DIR"

echo "[$(date)] 开始备份..." | tee -a "$BACKUP_DIR/backup.log"

# 从 .env 读取配置
if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

# MySQL 备份（跳过错误表，只备份核心表）
TABLES="resources users search_logs import_queue settings operation_logs search_history favorites"
docker exec resource_web mysqldump -h"${DB_HOST:-172.23.0.2}" -u"${DB_USER:-root}" -p"${DB_PASSWORD}" "${DB_NAME:-pan_resource}" $TABLES 2>/dev/null | gzip > "$BACKUP_DIR/db_${DATE}.sql.gz" || true

# Redis 备份
docker exec resource_web_redis redis-cli -a "${REDIS_PASSWORD}" BGSAVE 2>/dev/null || true
sleep 2
docker cp resource_web_redis:/data/dump.rdb "$BACKUP_DIR/redis_${DATE}.rdb" 2>/dev/null || true

# 配置文件备份
cp -f .env "$BACKUP_DIR/env_${DATE}.bak" 2>/dev/null || true
cp -f docker-compose.yml "$BACKUP_DIR/docker-compose_${DATE}.yml" 2>/dev/null || true

# 清理旧备份（保留最近7天）
find "$BACKUP_DIR" -type f -mtime +7 -delete 2>/dev/null || true

echo "[$(date)] 备份完成，保留最近7天" | tee -a "$BACKUP_DIR/backup.log"
