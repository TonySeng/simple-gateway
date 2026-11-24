#!/usr/bin/env bash
# =============================================================
#  simple-gateway 本地导出脚本
#  在 Windows(Git Bash) / macOS / Linux 均可运行。
#  作用：把「应用镜像 + PostgreSQL 数据 + 密钥 + 部署文件」
#        打包到 migration-export/ 目录，供你拷贝到服务器。
# =============================================================
set -euo pipefail

EXPORT_DIR="migration-export"
mkdir -p "$EXPORT_DIR"

echo "==> [1/4] 导出应用镜像 simple-gateway:latest"
docker save simple-gateway:latest -o "$EXPORT_DIR/simple-gateway.tar"
echo "    镜像大小: $(du -h "$EXPORT_DIR/simple-gateway.tar" | cut -f1)"

echo "==> [2/4] 导出 PostgreSQL 数据 (sgateway)"
# 在 postgres 容器内执行 pg_dump，免装本机 psql 客户端
docker exec simple-gateway-postgres pg_dump -U sg -d sgateway -F p -O -x > "$EXPORT_DIR/sg.sql"
echo "    dump 大小: $(du -h "$EXPORT_DIR/sg.sql" | cut -f1)"

echo "==> [3/4] 拷贝密钥 .secrets.json（Fernet 密钥，必须随数据一起迁移，否则厂商凭证解不开）"
MSYS_NO_PATHCONV=1 docker run --rm \
  -v simple-gateway_gateway-data:/data \
  -v "$PWD/$EXPORT_DIR":/out \
  alpine cp /data/.secrets.json /out/.secrets.json
echo "    .secrets.json 大小: $(wc -c < "$EXPORT_DIR/.secrets.json") 字节"

echo "==> [4/4] 复制服务器端部署文件"
cp docker-compose.server.yml "$EXPORT_DIR/" 2>/dev/null || true
cp .env.server.example       "$EXPORT_DIR/" 2>/dev/null || true
cp tools/migrate_restore.sh  "$EXPORT_DIR/" 2>/dev/null || true
cp tools/deploy_restore.sh     "$EXPORT_DIR/" 2>/dev/null || true
cp tools/deploy_image_only.sh  "$EXPORT_DIR/" 2>/dev/null || true
cp MIGRATION.md              "$EXPORT_DIR/" 2>/dev/null || true

echo
echo "✅ 导出完成。migration-export/ 内容："
ls -la "$EXPORT_DIR"
echo
echo "下一步（在你本机执行）："
echo "  scp -r migration-export/ <user>@<服务器IP>:/path/to/deploy/"
echo "然后 SSH 登录服务器，进入该目录按场景二选一："
echo "  bash deploy_image_only.sh   # 只换镜像，不动数据（日常升级，推荐）"
echo "  bash deploy_restore.sh      # 清库后恢复数据（首次部署 / 需要重置数据）"
