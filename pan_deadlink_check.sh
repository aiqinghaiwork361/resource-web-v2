#!/bin/bash
# 网盘失效链接检测脚本
# 原理：请求网盘链接页面，根据返回内容判断是否失效/违规/取消分享
set -uo pipefail

RESOURCE_WEB_DIR="/vol2/1000/docker/resource_web"
if [ -f "$RESOURCE_WEB_DIR/.env" ]; then
    set -a
    source "$RESOURCE_WEB_DIR/.env"
    set +a
fi

DB_HOST="${DB_HOST:-172.23.0.2}"
DB_PORT="${DB_PORT:-3306}"
DB_USER="${DB_USER:-root}"
DB_PASS="${DB_PASSWORD:-}"
DB_NAME="${DB_NAME:-pan_resource}"

if [ -z "$DB_PASS" ]; then
    echo "ERROR: DB_PASSWORD not set"
    exit 1
fi

CHECK_LIMIT="${CHECK_LIMIT:-20}"
TIMEOUT="${TIMEOUT:-15}"
TMP_FILE="/tmp/deadlink_check_$$.html"

check_url() {
    local url="$1"
    local http_code body

    # 只检测 HTTP 网盘链接
    case "$url" in
        http://*|https://*)
            ;;
        *)
            echo "unknown"
            return
            ;;
    esac

    http_code=$(curl -s -L -o "$TMP_FILE" -w "%{http_code}" \
        -H "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36" \
        --max-time "$TIMEOUT" "$url" 2>/dev/null) || http_code="000"

    if [ "$http_code" = "000" ]; then
        echo "dead"
        return
    fi

    body=$(cat "$TMP_FILE" 2>/dev/null | tr -d '\n' | sed 's/  */ /g' | head -c 3000)

    # 百度网盘
    if echo "$url" | grep -qi "pan.baidu.com\|yun.baidu.com"; then
        if echo "$body" | grep -qi "链接已失效\|分享已取消\|分享不存在\|页面不存在\|错误码\|违规\|非法"; then
            echo "dead"
        else
            echo "alive"
        fi
        return
    fi

    # 阿里/夸克
    if echo "$url" | grep -qi "quark.cn\|aliyundrive.com\|alipan.com\|pan.aliyun.com"; then
        if echo "$body" | grep -qi "分享已过期\|文件不存在\|链接失效\|分享取消\|违规\|非法分享\|不存在\|已过期"; then
            echo "dead"
        else
            echo "alive"
        fi
        return
    fi

    # 天翼
    if echo "$url" | grep -qi "cloud.189.cn"; then
        if echo "$body" | grep -qi "已失效\|已取消\|不存在\|过期\|违规\|非法\|错误\|分享已关闭"; then
            echo "dead"
        else
            echo "alive"
        fi
        return
    fi

    # 迅雷
    if echo "$url" | grep -qi "pan.xunlei.com"; then
        if echo "$body" | grep -qi "已失效\|已取消\|不存在\|过期\|违规\|非法\|错误\|分享已关闭\|该分享不存在"; then
            echo "dead"
        else
            echo "alive"
        fi
        return
    fi

    # 通用
    if echo "$body" | grep -qi "404\|not found\|已失效\|已取消\|不存在\|过期\|违规\|非法\|分享已关闭\|错误"; then
        echo "dead"
    elif [ "$http_code" -ge 200 ] && [ "$http_code" -lt 400 ]; then
        echo "alive"
    else
        echo "unknown"
    fi
}

echo "$(date): 开始失效链接检测 (limit=$CHECK_LIMIT)"

# 只检测 HTTP 网盘链接，且未检测或非 alive 的
mysql -h"$DB_HOST" -P"$DB_PORT" -u"$DB_USER" -p"$DB_PASS" "$DB_NAME" -N -e "
SELECT id, url 
FROM resources 
WHERE url LIKE 'http%'
  AND (last_checked IS NULL OR link_status NOT IN ('alive', 'unknown'))
ORDER BY last_checked ASC 
LIMIT $CHECK_LIMIT
" 2>/dev/null | while IFS=$'\t' read -r rid url; do
    if [ -z "$url" ]; then continue; fi
    
    status=$(check_url "$url")
    
    mysql -h"$DB_HOST" -P"$DB_PORT" -u"$DB_USER" -p"$DB_PASS" "$DB_NAME" -e "
        UPDATE resources SET last_checked=NOW(), link_status='$status' WHERE id=$rid
    " 2>/dev/null
    
    echo "  [$(date +%H:%M:%S)] id=$rid status=$status url=${url:0:60}"
done

rm -f "$TMP_FILE"
echo "$(date): 检测完成"
