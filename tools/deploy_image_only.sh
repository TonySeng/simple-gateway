#!/usr/bin/env bash
# =============================================================================
# Simple Gateway —— 只更新镜像（不动数据）
#
# 适用：服务器上已经有在跑的实例，你只想把 gateway 换成迁移包里导出的新镜像。
# 明确不做的事：不 down -v、不删/不备份数据卷、不重建 postgres、不导入 sg.sql。
#
# 用法（在存放 simple-gateway.tar 的目录下执行）：
#   bash deploy_image_only.sh                 # 正常更新
#   bash deploy_image_only.sh --yes           # 免确认（自动化）
#   bash deploy_image_only.sh --dry-run       # 只检查，不动手
#   bash deploy_image_only.sh --rollback      # 回滚到上一次更新前的镜像
#
# 可选参数：
#   --tar <文件>        镜像 tar（默认 simple-gateway.tar）
#   --compose <文件>    compose 文件（默认自动找 docker-compose.server.yml）
#   --health-url <url>  健康检查地址（默认 http://127.0.0.1:8020/health）
#   --force-recreate    即使镜像未变也强制重建容器（配置漂移时用）
#   -h, --help          显示本帮助
# =============================================================================
set -uo pipefail

# Git Bash / MSYS 下别做路径转换（Linux 上无副作用）
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

ASSUME_YES=0
DRY_RUN=0
ROLLBACK=0
FORCE_RECREATE=0
TAR="simple-gateway.tar"
COMPOSE_FILE=""
HEALTH_URL="http://127.0.0.1:8020/health"
GW_CT="simple-gateway"
PG_CT="simple-gateway-postgres"
IMAGE="simple-gateway:latest"
PREV_TAG="simple-gateway:prev"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; DIM='\033[2m'; NC='\033[0m'
say()  { echo -e "${GREEN}$*${NC}"; }
warn() { echo -e "${YELLOW}$*${NC}"; }
die()  { echo -e "${RED}✗ $*${NC}" >&2; exit 1; }
step() { echo; echo -e "${GREEN}==> $*${NC}"; }
dim()  { echo -e "${DIM}$*${NC}"; }

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y)          ASSUME_YES=1 ;;
    --dry-run|-n)      DRY_RUN=1 ;;
    --rollback)        ROLLBACK=1 ;;
    --force-recreate)  FORCE_RECREATE=1 ;;
    --tar)             TAR="${2:?--tar 需要文件路径}"; shift ;;
    --compose)         COMPOSE_FILE="${2:?--compose 需要文件路径}"; shift ;;
    --health-url)      HEALTH_URL="${2:?--health-url 需要地址}"; shift ;;
    -h|--help)         usage ;;
    *)                 die "未知参数：$1（-h 看用法）" ;;
  esac
  shift
done

# ---------------- compose 探测 ----------------
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  die "未找到 docker compose（V2 或 V1）"
fi
# shellcheck disable=SC2086
dc() { $DC "$@"; }

if [ -z "$COMPOSE_FILE" ]; then
  for f in docker-compose.server.yml docker-compose.yml; do
    [ -f "$f" ] && COMPOSE_FILE="$f" && break
  done
fi

img_id() { docker inspect -f '{{.Id}}' "$1" 2>/dev/null || echo ""; }
ct_img() { docker inspect -f '{{.Image}}' "$1" 2>/dev/null || echo ""; }

# ---------------- 回滚分支 ----------------
if [ "$ROLLBACK" = "1" ]; then
  step "回滚到上一次更新前的镜像"
  [ -n "$(img_id "$PREV_TAG")" ] || die "没有找到 $PREV_TAG，无法回滚"
  dim "  $PREV_TAG = $(img_id "$PREV_TAG" | cut -c1-19)…"
  docker tag "$PREV_TAG" "$IMAGE" || die "打标签失败"
  dc -f "$COMPOSE_FILE" up -d --no-deps --force-recreate gateway || die "重建容器失败"
  RB_OK=0
  for i in $(seq 1 30); do
    hst=$(docker inspect -f '{{.State.Health.Status}}' "$GW_CT" 2>/dev/null || echo unknown)
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 "$HEALTH_URL" 2>/dev/null) || true
    code=$(printf '%s' "${code:-}" | cut -c1-3)
    case "$code" in 2[0-9][0-9]) RB_OK=1; break ;; esac
    [ "$hst" = "healthy" ] && { RB_OK=1; break; }
    sleep 2
  done
  if [ "$RB_OK" = "1" ]; then
    say "  已回滚，服务正常（health=$hst / $HEALTH_URL→$code）"
  else
    warn "  已回滚，但服务未恢复（health=$hst / code=$code），请查看：docker logs --tail 50 $GW_CT"
  fi
  exit 0
fi

# ---------------- 1. 自检 ----------------
step "[1/7] 自检"
command -v docker >/dev/null 2>&1 || die "未安装 docker"
docker info >/dev/null 2>&1 || die "docker 未运行或无权限（试试 sudo）"
[ -f "$TAR" ] || die "找不到镜像文件：$TAR（请在本目录放置 simple-gateway.tar，或用 --tar 指定）"
[ -f "$COMPOSE_FILE" ] || die "找不到 compose 文件（用 --compose 指定）"
say "  docker OK · compose = $DC · 镜像包 = $TAR · compose 文件 = $COMPOSE_FILE"

# ---------------- 2. 记录当前状态（用于对比与回滚）----------------
step "[2/7] 记录当前状态"
OLD_IMG=$(img_id "$IMAGE")
RUN_IMG=$(ct_img "$GW_CT")
PG_ID_BEFORE=$(docker inspect -f '{{.Id}}' "$PG_CT" 2>/dev/null || echo "absent")
PG_START_BEFORE=$(docker inspect -f '{{.State.StartedAt}}' "$PG_CT" 2>/dev/null || echo "absent")
VOLS_BEFORE=$(docker volume ls --format '{{.Name}}' | grep -E 'simple-gateway_(postgres-data|gateway-data)$' | sort | tr '\n' ' ')
dim "  当前镜像        : ${OLD_IMG:0:19}… (${OLD_IMG:-无})"
dim "  容器实际运行镜像: ${RUN_IMG:0:19}…"
dim "  postgres 容器   : ${PG_ID_BEFORE:0:19}… 启动于 $PG_START_BEFORE"
dim "  数据卷          : ${VOLS_BEFORE:-无}"

[ "$PG_ID_BEFORE" = "absent" ] && warn "  ⚠ 未发现 $PG_CT —— 若这台机器上本就没有数据库，请确认这是你想更新的实例"

# ---------------- 3. 备份当前镜像（供回滚）----------------
step "[3/7] 备份当前镜像为 $PREV_TAG"
if [ -n "$OLD_IMG" ]; then
  if [ "$DRY_RUN" = "1" ]; then
    dim "  [dry-run] docker tag $IMAGE $PREV_TAG"
  else
    docker tag "$IMAGE" "$PREV_TAG" && say "  已备份：$PREV_TAG（回滚用：bash $0 --rollback）"
  fi
else
  warn "  当前没有 $IMAGE，跳过备份（首次安装请用 deploy_restore.sh）"
fi

# ---------------- 4. 加载新镜像 ----------------
step "[4/7] 加载新镜像（$TAR）"
if [ "$DRY_RUN" = "1" ]; then
  dim "  [dry-run] docker load -i $TAR"
else
  LOAD_OUT=$(docker load -i "$TAR" 2>&1) || { echo "$LOAD_OUT" | tail -5; die "docker load 失败"; }
  echo "$LOAD_OUT" | sed 's/^/  /'
fi
NEW_IMG=$(img_id "$IMAGE")
[ -n "$NEW_IMG" ] || die "加载后仍找不到 $IMAGE"
if [ "$DRY_RUN" = "1" ]; then
  dim "  [dry-run] 跳过镜像差异判断"
elif [ "$NEW_IMG" = "$OLD_IMG" ]; then
  warn "  镜像 ID 未变化（包里的就是当前镜像）"
else
  say "  新镜像：${NEW_IMG:0:19}…"
fi

# ---------------- 5. 只重建 gateway ----------------
step "[5/7] 重建 gateway 容器（--no-deps，绝不动 postgres）"
RECREATE_ARGS="up -d --no-deps"
[ "$FORCE_RECREATE" = "1" ] && RECREATE_ARGS="$RECREATE_ARGS --force-recreate"
if [ "$DRY_RUN" = "1" ]; then
  dim "  [dry-run] $DC -f $COMPOSE_FILE $RECREATE_ARGS gateway"
else
  # shellcheck disable=SC2086
  if ! dc -f "$COMPOSE_FILE" $RECREATE_ARGS gateway; then
    warn "  重建失败，尝试回滚…"
    if [ -n "$(img_id "$PREV_TAG")" ]; then
      docker tag "$PREV_TAG" "$IMAGE"
      dc -f "$COMPOSE_FILE" up -d --no-deps --force-recreate gateway >/dev/null 2>&1 || true
      die "已回滚到旧镜像，请查看：docker logs --tail 50 $GW_CT"
    fi
    die "重建失败且无备份可回滚"
  fi
fi

# ---------------- 6. 等健康 ----------------
step "[6/7] 等待 /health 通过"
if [ "$DRY_RUN" = "1" ]; then
  dim "  [dry-run] 轮询 $HEALTH_URL"
  HEALTH_OK=1
else
  HEALTH_OK=0
  for i in $(seq 1 30); do
    # 注意：容器重启瞬间连接可能先回 200 再中断，curl 会以非 0 退出但已打印状态码，
    # 所以这里不用 `|| echo 000`（会拼成 "200000"），只截取前 3 位判 2xx。
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 "$HEALTH_URL" 2>/dev/null) || true
    code=$(printf '%s' "${code:-}" | cut -c1-3)
    case "$code" in
      2[0-9][0-9]) HEALTH_OK=1; break ;;
    esac
    hst=$(docker inspect -f '{{.State.Health.Status}}' "$GW_CT" 2>/dev/null || echo unknown)
    [ "$hst" = "healthy" ] && { HEALTH_OK=1; code="200(health)"; break; }
    sleep 2
  done
  if [ "$HEALTH_OK" = "1" ]; then
    say "  $HEALTH_URL → $code"
  else
    warn "  ✗ 健康检查未通过（最后状态码 ${code:-无}，容器健康状态 ${hst:-unknown}）"
    dim "  最近日志："
    docker logs --tail 25 "$GW_CT" 2>&1 | sed 's/^/    /'
    if [ -n "$(img_id "$PREV_TAG")" ]; then
      warn "  正在回滚到 $PREV_TAG …"
      docker tag "$PREV_TAG" "$IMAGE"
      dc -f "$COMPOSE_FILE" up -d --no-deps --force-recreate gateway >/dev/null 2>&1 || true
      die "已回滚。请把上面日志发我排查"
    fi
    die "更新失败且无备份可回滚"
  fi
fi

# ---------------- 7. 核验：镜像已换 + 数据未动 ----------------
step "[7/7] 核验"
if [ "$DRY_RUN" = "1" ]; then
  dim "  [dry-run] 跳过运行态核验"
  echo
  say "dry-run 结束，未做任何改动。"
  exit 0
fi

NOW_IMG=$(ct_img "$GW_CT")
if [ "$NOW_IMG" = "$NEW_IMG" ]; then
  say "  ✓ 容器已在跑新镜像：${NOW_IMG:0:19}…"
else
  warn "  ⚠ 容器镜像与预期不一致：$NOW_IMG vs $NEW_IMG"
fi

PG_ID_AFTER=$(docker inspect -f '{{.Id}}' "$PG_CT" 2>/dev/null || echo "absent")
PG_START_AFTER=$(docker inspect -f '{{.State.StartedAt}}' "$PG_CT" 2>/dev/null || echo "absent")
VOLS_AFTER=$(docker volume ls --format '{{.Name}}' | grep -E 'simple-gateway_(postgres-data|gateway-data)$' | sort | tr '\n' ' ')

if [ "$PG_ID_AFTER" = "$PG_ID_BEFORE" ] && [ "$PG_START_AFTER" = "$PG_START_BEFORE" ]; then
  say "  ✓ postgres 未被重建（容器 ID 与启动时间均未变）"
else
  warn "  ⚠ postgres 状态发生变化，请核查！"
fi
if [ "$VOLS_AFTER" = "$VOLS_BEFORE" ]; then
  say "  ✓ 数据卷未变：${VOLS_AFTER}"
else
  warn "  ⚠ 数据卷列表变化：前 [${VOLS_BEFORE}] 后 [${VOLS_AFTER}]"
fi

# 代码级标记（老镜像没有这些标记属正常，仅作提示）
mark_main=$(docker exec "$GW_CT" sh -c 'grep -c collapse_duplicate_v1 /app/app/main.py 2>/dev/null || echo 0' 2>/dev/null | tr -d ' \r')
mark_ct=$(docker exec "$GW_CT" sh -c 'grep -c count_tokens /app/app/services/llm/router.py 2>/dev/null || echo 0' 2>/dev/null | tr -d ' \r')
dim "  代码标记：/v1 折叠=${mark_main:-0} · count_tokens=${mark_ct:-0}（0 表示该镜像不含这项能力）"

# 业务数据仍在（只读查询，不写）
PGN=$(docker exec "$PG_CT" psql -U sg -d sgateway -tAc "select count(*) from providers;" 2>/dev/null | tr -d ' \r')
MGN=$(docker exec "$PG_CT" psql -U sg -d sgateway -tAc "select count(*) from models;" 2>/dev/null | tr -d ' \r')
KGN=$(docker exec "$PG_CT" psql -U sg -d sgateway -tAc "select count(*) from api_keys;" 2>/dev/null | tr -d ' \r')
[ -n "$PGN" ] && say "  ✓ 数据仍在：厂商 ${PGN} / 模型 ${MGN} / Key ${KGN}"

echo
say "完成。只换了镜像，数据一行未动。"
dim "  回滚：bash $(basename "$0") --rollback"
dim "  日志：docker logs -f $GW_CT"
