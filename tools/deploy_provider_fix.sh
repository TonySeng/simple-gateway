#!/usr/bin/env bash
# 服务器端「只换代码」部署：以当前生产镜像为基础层，重新 COPY 新代码。
# 通过 ssh 'bash -s' < 本文件 在服务器执行；不做 down -v、不动数据卷、不重建 postgres。
set -euo pipefail

WORK="$HOME/private-app/build-20261113"
EXPORT="$HOME/private-app/migration-export"
GW=simple-gateway
PG=simple-gateway-postgres

echo "==> [1/8] 解压代码包"
cd "$WORK"
rm -rf app run.py tools
tar xzf sg-app-20261113.tgz
echo "    app.js bytes: $(wc -c < app/static/app.js)"

echo "==> [2/8] 备份数据库"
BK="$EXPORT/pre-providerfix-$(date +%Y%m%d-%H%M%S).sql"
docker exec "$PG" pg_dump -U sg -d sgateway > "$BK"
echo "    $BK ($(du -h "$BK" | cut -f1))"

echo "==> [3/8] 记录并备份当前镜像"
OLD=$(docker inspect -f '{{.Id}}' "$GW:latest")
PG_ID_BEFORE=$(docker inspect -f '{{.Id}}' "$PG")
PG_START_BEFORE=$(docker inspect -f '{{.State.StartedAt}}' "$PG")
docker tag "$GW:latest" "$GW:prev"
docker tag "$GW:latest" "$GW:base-providerfix"
echo "    old=$OLD  prev 与 base-providerfix 已打标签（可回滚）"

echo "==> [4/8] 生成 Dockerfile（复用生产镜像的依赖层）"
cat > Dockerfile <<'DOCKEREOF'
FROM simple-gateway:base-providerfix
WORKDIR /app
COPY app ./app
COPY run.py ./run.py
COPY tools ./tools
DOCKEREOF

echo "==> [5/8] 构建新镜像"
docker build -t "$GW:latest" . 2>&1 | tail -12
NEW=$(docker inspect -f '{{.Id}}' "$GW:latest")
echo "    new=$NEW"
if [ "$NEW" = "$OLD" ]; then echo "!! 镜像 ID 未变化，构建可能命中了旧缓存"; exit 1; fi

echo "==> [6/8] 校验镜像内确实是新代码（缓存坑铁律）"
A=$(docker run --rm "$GW:latest" grep -c 'settingText' /app/app/static/app.js || true)
B=$(docker run --rm "$GW:latest" grep -c 'duplicate_provider' /app/app/admin/providers.py || true)
C=$(docker run --rm "$GW:latest" grep -c '20261113a' /app/app/static/index.html || true)
D=$(docker run --rm "$GW:latest" grep -c '"json": True' /app/app/services/ocr/routing.py || true)
echo "    settingText=$A duplicate=$B 版本=$C json标记=$D"
[ "${A:-0}" -ge 1 ] && [ "${B:-0}" -ge 1 ] && [ "${C:-0}" -ge 1 ] && [ "${D:-0}" -ge 1 ] || {
  echo "!! 镜像内代码校验失败，终止部署"; exit 1;
}

echo "==> [7/8] 只重建 gateway 容器"
cd "$EXPORT"
if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi
echo "    compose = $DC"
$DC -f docker-compose.server.yml up -d --no-deps --force-recreate gateway

HEALTH=000
for i in $(seq 1 30); do
  HEALTH=$(curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:8020/health || true)
  case "$HEALTH" in 2??) break ;; esac
  sleep 2
done
echo "    /health = $HEALTH"
case "$HEALTH" in 2??) ;; *) docker logs --tail 30 "$GW"; echo "!! 健康检查未通过，可用 $GW:prev 回滚"; exit 1 ;; esac

echo "==> [8/8] 线上核验"
echo "    容器镜像: $(docker inspect -f '{{.Image}}' "$GW")"
PG_ID_AFTER=$(docker inspect -f '{{.Id}}' "$PG")
PG_START_AFTER=$(docker inspect -f '{{.State.StartedAt}}' "$PG")
if [ "$PG_ID_AFTER" = "$PG_ID_BEFORE" ] && [ "$PG_START_AFTER" = "$PG_START_BEFORE" ]; then
  echo "    ✓ postgres 未重建，数据卷未动"
else
  echo "    !! postgres 状态变化，请核查"
fi
echo "    HTTP 静态资源 settingText 命中: $(curl -s http://127.0.0.1:8020/admin/app.js | grep -c settingText || true)"
echo "    HTTP index 版本: $(curl -s http://127.0.0.1:8020/admin/ | grep -o 'app.js?v=[0-9a-z]*' || true)"
echo "    业务数据: 厂商 $(docker exec "$PG" psql -U sg -d sgateway -tAc 'select count(*) from providers;' | tr -d ' \r') / 模型 $(docker exec "$PG" psql -U sg -d sgateway -tAc 'select count(*) from models;' | tr -d ' \r') / Key $(docker exec "$PG" psql -U sg -d sgateway -tAc 'select count(*) from api_keys;' | tr -d ' \r')"
echo
echo "完成：只换了镜像与代码，数据一行未动。回滚：docker tag simple-gateway:prev simple-gateway:latest 后重建容器。"
