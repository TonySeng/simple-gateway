#!/usr/bin/env bash
# =============================================================
#  simple-gateway 服务器端恢复脚本（Linux，需 Docker + compose v2）
#  在迁移包目录内执行： bash migrate_restore.sh
# =============================================================
set -euo pipefail

cd "$(dirname "$0")"

echo "==> [1/6] 加载应用镜像"
docker load -i simple-gateway.tar
docker tag simple-gateway:latest simple-gateway:latest 2>/dev/null || true

echo "==> [2/6] 启动 PostgreSQL（仅 postgres 服务，先建空库）"
docker compose -f docker-compose.server.yml up -d postgres
echo "    等待 postgres 健康..."
for i in $(seq 1 30); do
  if docker exec simple-gateway-postgres pg_isready -U sg -d sgateway >/dev/null 2>&1; then
    echo "    postgres 就绪"; break
  fi
  sleep 2
done

echo "==> [3/6] 恢复数据到 sgateway"
docker cp sg.sql simple-gateway-postgres:/tmp/sg.sql
docker exec -i simple-gateway-postgres psql -U sg -d sgateway -v ON_ERROR_STOP=1 -f /tmp/sg.sql
docker exec simple-gateway-postgres rm -f /tmp/sg.sql 2>/dev/null || true
echo "    数据恢复完成"

echo "==> [4/6] 写入密钥 .secrets.json 到 gateway-data 卷（gateway 启动前必须就位）"
MSYS_NO_PATHCONV=1 docker run --rm \
  -v simple-gateway_gateway-data:/data \
  -v "$PWD":/src \
  alpine cp /src/.secrets.json /data/.secrets.json
echo "    密钥已写入"

echo "==> [5/6] 启动 gateway"
docker compose -f docker-compose.server.yml up -d gateway
echo "    等待 gateway 健康..."
for i in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8020/health >/dev/null 2>&1; then
    echo "    gateway 就绪"; break
  fi
  sleep 3
done

echo "==> [6/6] 验证"
curl -fsS http://127.0.0.1:8020/health && echo "  <- /health OK"
echo
echo "✅ 迁移完成。后台地址： http://<服务器IP>:8020/admin/"
echo "🔑 管理员口令随数据一起迁移：请用你【本地当前】的管理员口令登录（不是 admin123）。"
echo "   SG_ADMIN_PASSWORD 只在空库首次初始化时播种一次，不会覆盖已迁移的口令；"
echo "   启动日志里若有 'SG_ADMIN_PASSWORD 仍为默认 admin123'，那只是环境变量默认值提示，可忽略。"
echo "⚠️ 建议固定 SG_JWT_SECRET / SG_FERNET_KEY，并确保 .secrets.json 与数据库同步备份（缺一则厂商凭证解不开）。"
