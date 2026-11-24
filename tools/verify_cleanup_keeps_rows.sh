#!/usr/bin/env bash
# 线上核验：清理只清空详情，日志记录一条不少
set -uo pipefail
BASE=http://127.0.0.1:8020
ADMIN_USER=admin
ADMIN_PASS='Iflytek@2026#'

jqv() { python3 -c "import sys,json;d=json.load(sys.stdin);print(d$1)"; }

TOKEN=$(curl -s -X POST "$BASE/api/admin/login" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}" | jqv "['access_token']")
[ -n "$TOKEN" ] || { echo "登录失败"; exit 1; }
H="Authorization: Bearer $TOKEN"

rows_in_db() { docker exec simple-gateway-postgres psql -U sg -d sgateway -t -A -c 'select count(*) from request_logs;'; }

echo "== 清理前 =="
curl -s "$BASE/api/admin/logs/retention" -H "$H"; echo
ROWS_BEFORE=$(rows_in_db)
echo "库里行数: $ROWS_BEFORE"

echo
echo "== 临时把保留期改成 0 天（让全部详情都超期）=="
curl -s -X PUT "$BASE/api/admin/logs/retention?days=0" -H "$H"; echo

echo
echo "== 触发清理 =="
curl -s -X POST "$BASE/api/admin/logs/retention/cleanup" -H "$H"; echo

ROWS_AFTER=$(rows_in_db)
echo "库里行数: $ROWS_AFTER"
if [ "$ROWS_BEFORE" = "$ROWS_AFTER" ]; then
  echo "✓ 记录数未变（$ROWS_BEFORE -> $ROWS_AFTER）：只清了详情"
else
  echo "✗ 记录数发生变化（$ROWS_BEFORE -> $ROWS_AFTER）"
fi

echo
echo "== 抽查一条：统计字段是否完好 =="
docker exec simple-gateway-postgres psql -U sg -d sgateway -c \
  "select id, service, status, latency_ms, tokens_in, tokens_out, cost,
          coalesce(request_body,'')='' as req_empty,
          coalesce(response_body,'')='' as resp_empty,
          coalesce(upstream_body,'')='' as up_empty
     from request_logs order by created_at desc limit 3;"

echo
echo "== 恢复保留期 7 天 =="
curl -s -X PUT "$BASE/api/admin/logs/retention?days=7" -H "$H"; echo
