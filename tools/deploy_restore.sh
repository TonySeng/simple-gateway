#!/usr/bin/env bash
# =============================================================
#  Simple Gateway —— 服务器端「清库 + 恢复」一键脚本
#
#  场景：把本地 migration-export/ 整个目录手动拷到服务器后，
#        一条命令完成：
#          自查 → 备份旧卷 → 清空旧容器/数据卷 → 加载镜像 →
#          起 PostgreSQL → 灌 sg.sql → 放 .secrets.json →
#          起 gateway → 健康检查 + 数据自检
#
#  用法（在 migration-export/ 目录内执行）：
#    bash deploy_restore.sh                 # 交互确认（推荐）
#    bash deploy_restore.sh --yes           # 免确认（重复执行/自动化）
#    bash deploy_restore.sh --yes --no-backup   # 跳过备份旧卷（磁盘紧张）
#
#  ⚠️ 本脚本会【删除】服务器上 simple-gateway 的旧容器与数据卷。
#     默认会在删除前把旧卷打包到 pre-restore-backup/<时间戳>/
# =============================================================
set -euo pipefail

# ---------------- 参数 ----------------
ASSUME_YES=0
DO_BACKUP=1
for a in "$@"; do
  case "$a" in
    -y|--yes)      ASSUME_YES=1 ;;
    --no-backup)   DO_BACKUP=0 ;;
    -h|--help)     sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "未知参数: $a（可用: --yes | --no-backup | --help）" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")"

# 在 Git Bash(MSYS)/Cygwin 下关闭路径自动转换：避免 `/data`、`/tmp` 这类容器内路径
# 被改写成 Windows 本地盘符路径。Linux 服务器上为无副作用的空设置。
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
say()  { echo -e "${GREEN}$*${NC}"; }
warn() { echo -e "${YELLOW}$*${NC}"; }
die()  { echo -e "${RED}✗ $*${NC}" >&2; exit 1; }
step() { echo; echo -e "${GREEN}==> $*${NC}"; }

case "$(uname -s 2>/dev/null || echo unknown)" in
  MINGW*|MSYS*|CYGWIN*)
    warn "提示：当前是 Windows 兼容层（$(uname -s)），本脚本面向 Linux 服务器；"
    warn "      在此运行仅用于演练，正式部署请在服务器上执行。"
    ;;
esac

COMPOSE_FILE="docker-compose.server.yml"
GATEWAY_CT="simple-gateway"
PG_CT="simple-gateway-postgres"
GW_VOL="simple-gateway_gateway-data"
PG_VOL="simple-gateway_postgres-data"
PORT="8020"

on_error() {
  echo
  warn "脚本中断。排查建议："
  warn "  docker ps -a | grep simple-gateway"
  warn "  docker logs --tail 50 $GATEWAY_CT 2>/dev/null"
  warn "  docker logs --tail 50 $PG_CT 2>/dev/null"
}
trap on_error ERR

# ---------------- 0. 环境与文件自检 ----------------
step "[0/8] 环境与文件自检"
command -v docker >/dev/null 2>&1 || die "未安装 docker"

if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  die "未找到 docker compose（V2）或 docker-compose（V1），请先安装"
fi

for f in simple-gateway.tar sg.sql .secrets.json "$COMPOSE_FILE"; do
  [ -s "$f" ] || die "缺少文件或文件为空：$f —— 请在 migration-export/ 目录内执行本脚本"
done

echo "  docker   : $(docker --version)"
echo "  compose  : $DC"
echo "  镜像包   : simple-gateway.tar  $(du -h simple-gateway.tar | cut -f1)"
echo "  数据包   : sg.sql              $(du -h sg.sql | cut -f1)"
echo "  密钥     : .secrets.json       $(wc -c < .secrets.json) 字节"

if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  warn "  注意：端口 $PORT 已被占用，compose 里 8020:8000 的映射会失败，请先释放或改端口"
fi

# ---------------- 1. 危险操作确认 ----------------
step "[1/8] 危险操作确认"
warn "即将【删除】以下内容，并用本包内的 sg.sql 覆盖恢复："
warn "  容器    : $GATEWAY_CT, $PG_CT"
warn "  数据卷  : $PG_VOL（全部 Key/厂商/模型/日志/管理员口令）"
warn "            $GW_VOL（.secrets.json 等运行时数据）"
if [ "$DO_BACKUP" = "1" ]; then
  warn "  已启用备份：删除前会把现有数据卷打包到 pre-restore-backup/"
else
  warn "  ⚠️ 已禁用备份（--no-backup）：数据删除后不可恢复"
fi

if [ "$ASSUME_YES" != "1" ]; then
  printf "确认继续请完整输入 YES（其它任意输入取消）: "
  ans=""
  read -r ans || true
  [ "$ans" = "YES" ] || { echo "已取消，未做任何改动。"; exit 0; }
fi

# ---------------- 2. 备份现有数据卷 ----------------
if [ "$DO_BACKUP" = "1" ]; then
  step "[2/8] 备份现有数据卷"
  BK="pre-restore-backup/$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$BK"
  for pair in "$PG_VOL:postgres-data" "$GW_VOL:gateway-data"; do
    vol="${pair%%:*}"; name="${pair##*:}"
    if docker volume inspect "$vol" >/dev/null 2>&1; then
      docker run --rm -v "$vol":/data -v "$PWD/$BK":/backup alpine \
        sh -c "tar czf /backup/$name.tar.gz -C /data . >/dev/null 2>&1 || true"
      if [ -s "$BK/$name.tar.gz" ]; then
        say "  已备份 $vol -> $BK/$name.tar.gz（$(du -h "$BK/$name.tar.gz" | cut -f1)）"
      else
        warn "  备份 $vol 失败或为空卷，继续"
      fi
    else
      say "  卷 $vol 不存在，无需备份"
    fi
  done
else
  step "[2/8] 跳过备份（--no-backup）"
fi

# ---------------- 3. 清理旧容器与数据卷 ----------------
step "[3/8] 清理旧容器与数据卷"
$DC -f "$COMPOSE_FILE" down -v --remove-orphans >/dev/null 2>&1 || true
docker rm -f "$GATEWAY_CT" "$PG_CT" >/dev/null 2>&1 || true
docker volume rm -f "$PG_VOL" "$GW_VOL" >/dev/null 2>&1 || true

for v in "$PG_VOL" "$GW_VOL"; do
  if docker volume inspect "$v" >/dev/null 2>&1; then
    die "数据卷 $v 仍存在（可能有其它容器占用），请手动处理后重试：docker volume rm -f $v"
  fi
done
say "  旧容器与数据卷已清空"

# ---------------- 4. 加载镜像 ----------------
step "[4/8] 加载应用镜像"
docker load -i simple-gateway.tar | sed 's/^/  /'
docker image inspect simple-gateway:latest >/dev/null 2>&1 || die "镜像加载后仍找不到 simple-gateway:latest"
say "  镜像就绪（构建时间 $(docker image inspect simple-gateway:latest -f '{{.Created}}')）"

# ---------------- 5. 启动 PostgreSQL ----------------
step "[5/8] 启动 PostgreSQL 并等待就绪"
$DC -f "$COMPOSE_FILE" up -d postgres >/dev/null
pg_ok=0
for _ in $(seq 1 40); do
  if docker exec "$PG_CT" pg_isready -U sg -d sgateway >/dev/null 2>&1; then pg_ok=1; break; fi
  sleep 2
done
[ "$pg_ok" = "1" ] || die "PostgreSQL 80 秒内未就绪，查看：docker logs --tail 50 $PG_CT"
say "  PostgreSQL 就绪"

# ---------------- 6. 恢复数据 ----------------
step "[6/8] 恢复数据（sg.sql）"
# 用 stdin 直接喂给 psql：不在容器内落临时文件，也不需要宿主机临时文件
PG_OUT=""
if ! PG_OUT="$(docker exec -i "$PG_CT" psql -U sg -d sgateway -v ON_ERROR_STOP=1 -q -o /dev/null < sg.sql 2>&1)"; then
  warn "  psql 输出末尾 20 行："
  printf '%s\n' "$PG_OUT" | tail -20 | sed 's/^/    /'
  die "数据恢复失败。常见原因：目标库非空（旧表残留）——请确认第 3 步已清空数据卷后重跑"
fi
if [ -n "$PG_OUT" ]; then
  printf '%s\n' "$PG_OUT" | tail -5 | sed 's/^/  psql: /'
fi
say "  数据恢复完成"

# ---------------- 7. 写入 Fernet 密钥 ----------------
step "[7/8] 写入 Fernet 密钥到 $GW_VOL"
# 先用 compose「创建但不启动」gateway，让数据卷带上 compose 标签（否则后续 up 会有告警）
$DC -f "$COMPOSE_FILE" up -d --no-start gateway >/dev/null 2>&1 || true
# 用 stdin 写入，不挂载宿主机目录，避免路径转换差异。
# ⚠️ 必须以 appuser(999:999) 身份落盘：容器内进程非 root 运行，
#    若文件属主为 root（alpine 默认），应用读不到密钥 → 自生成临时密钥
#    → 所有已加密的厂商凭证永久解不开。这里 chown 是必须的，不是优化。
docker run --rm -i -v "$GW_VOL":/data alpine \
  sh -c 'cat > /data/.secrets.json && chmod 600 /data/.secrets.json && chown 999:999 /data/.secrets.json' < .secrets.json
docker run --rm -v "$GW_VOL":/data alpine sh -c 'test -s /data/.secrets.json' \
  || die "密钥写入失败：$GW_VOL 内未找到 .secrets.json"
# 属主自检：非 999 直接终止，避免启动后静默丢凭证
OWNER=$(docker run --rm -v "$GW_VOL":/data alpine stat -c '%u:%g' /data/.secrets.json 2>/dev/null)
[ "$OWNER" = "999:999" ] || die "密钥文件属主异常（$OWNER，应为 999:999）：容器以 appuser 运行将无法读取，请执行 docker run --rm -v $GW_VOL:/data alpine chown 999:999 /data/.secrets.json"
say "  密钥已就位，属主 $OWNER（缺此文件或属主错误则已加密的厂商凭证无法解密）"

# ---------------- 8. 启动 gateway + 自检 ----------------
step "[8/8] 启动 gateway 并自检"
$DC -f "$COMPOSE_FILE" up -d gateway >/dev/null
gw_ok=0
for _ in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then gw_ok=1; break; fi
  sleep 3
done
if [ "$gw_ok" != "1" ]; then
  warn "  gateway 未在 120 秒内通过健康检查，末尾日志："
  docker logs --tail 40 "$GATEWAY_CT" 2>&1 | sed 's/^/    /'
  die "gateway 启动失败"
fi
say "  /health 通过：$(curl -fsS "http://127.0.0.1:$PORT/health")"

echo
echo "  —— 数据自检 ——"
q() { docker exec -i "$PG_CT" psql -U sg -d sgateway -tAc "$1" 2>/dev/null | tr -d ' \r'; }
echo "    厂商数        : $(q 'select count(*) from providers;')"
echo "    模型数        : $(q 'select count(*) from models;')"
echo "    API Key 数    : $(q 'select count(*) from api_keys;')"
echo "    请求日志数    : $(q 'select count(*) from request_logs;')"
echo "    讯飞CC 类型   : $(q "select type from providers where name = '讯飞CC';")"
echo "    讯飞CC 模型数 : $(q "select count(*) from models m join providers p on m.provider_id = p.id where p.name = '讯飞CC';")"
echo "    api_keys.channels 列 : $(q "select count(*) from information_schema.columns where table_name='api_keys' and column_name='channels';") (1=已就绪)"

TYPES="$(curl -fsS "http://127.0.0.1:$PORT/api/admin/config" 2>/dev/null || true)"
if echo "$TYPES" | grep -q '"xfyun-cc"'; then
  say "    运行态 provider_types 已含 xfyun-cc ✔"
else
  warn "    运行态未检测到 xfyun-cc（若网关尚未就绪可稍后刷新后台确认）"
fi

echo
say "✅ 清库 + 恢复完成"
echo "   后台地址： http://<服务器IP>:$PORT/admin/"
echo "   🔑 管理员口令随数据迁移：请用【本地当前】的口令登录（不是 admin123）"
echo "   SG_ADMIN_PASSWORD 仅在空库首次初始化时播种一次，不会覆盖已迁移的口令。"
echo "   ⚠️ 建议固定 SG_JWT_SECRET；.secrets.json 必须与数据库一起备份。"
