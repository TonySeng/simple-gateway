# Simple Gateway 迁移指南（本地 → 应用服务器）

> 适用：已合并的 OCR + LLM 中转网关，数据已落在 PostgreSQL。
> 交付方式：**镜像 tar 导出**（服务器无需源码，只要镜像 + compose + 密钥 + 数据）。

---

## 一、要迁移的 3 样东西

| 内容 | 本地来源 | 落点 |
|---|---|---|
| 应用 | 镜像 `simple-gateway:latest` | 服务器 Docker 镜像 |
| 数据 | PostgreSQL `sgateway` 库 | 服务器 PostgreSQL |
| 密钥 | 卷 `simple-gateway_gateway-data` 内的 `/app/data/.secrets.json`（Fernet 密钥） | 服务器 gateway-data 卷同名路径 |

⚠️ **头号坑**：`.secrets.json` 必须和数据一起迁。只迁库不迁密钥 → 所有厂商凭证解密失败、provider 全部失效。

> 卷里那个 `gateway.db` 是切 PG 前的旧 SQLite，已废弃，**不用管**；`_peek.py` 是调试残留，也不需迁移。

---

## 二、前提条件（服务器端）

- 已装 **Docker + docker compose v2**（`docker compose version` 能看到 v2）。
- 架构一致：本地镜像在 **amd64**（Windows Docker Desktop 构建）。
  - 若服务器是 **arm64**（如部分云主机 / 树莓派 / 鲲鹏），`docker load` 能成功但 `docker run` 会报 `exec format error`。
  - 解决办法见文末「架构不一致时的回退方案」。
- 端口 `8020` 在服务器上未被占用（被占用就改 `docker-compose.server.yml` 的 `ports` 映射）。

---

## 三、本地导出（在你本机执行一次）

```bash
cd "D:/SoftwareData/Workbuddy/simple gateway"
bash tools/migrate_export.sh
```

脚本会生成 `migration-export/` 目录，含：
- `simple-gateway.tar` —— 应用镜像
- `sg.sql` —— PostgreSQL 数据 dump（plain SQL，含建表 + 数据）
- `.secrets.json` —— Fernet 密钥
- `docker-compose.server.yml` —— 服务端 compose（用已加载镜像，不 build）
- `migrate_restore.sh` —— 服务器端恢复脚本（不删旧数据，适合全新服务器）
- `deploy_restore.sh` —— 服务器端「清库 + 恢复」一键脚本（含备份/清空/自检，适合已有旧部署）
- `deploy_image_only.sh` —— 服务器端「只换镜像、不动数据」脚本（日常升级用）
- `MIGRATION.md` —— 本文档

> 嫌目录多可再打包：`tar czf simple-gateway-migration.tar.gz -C migration-export .`

---

## 四、传到服务器

```bash
scp -r migration-export/ <user>@<服务器IP>:/path/to/deploy/
# 例： scp -r migration-export/ root@1.2.3.4:/opt/simple-gateway/
```

---

## 五、服务器端恢复（SSH 登录服务器后执行）

```bash
ssh <user>@<服务器IP>
cd /path/to/deploy/migration-export
```

按服务器现状二选一：

### 方案 A（推荐）：一键「清库 + 恢复」`deploy_restore.sh`

服务器上**已有旧版本容器/数据**、或不确定是否干净时用这个。它会先备份旧卷，再清空、重建、恢复：

```bash
bash deploy_restore.sh                  # 交互确认，需手动输入 YES
bash deploy_restore.sh --yes            # 免确认（重复执行 / 自动化）
bash deploy_restore.sh --yes --no-backup  # 跳过备份旧卷（磁盘紧张时）
```

脚本依次：
1. 环境与文件自检（docker / compose V2 或 V1 / 四个必备文件 / 端口 8020 占用检查）
2. **危险确认**（需输入 `YES`；`--yes` 可跳过）
3. **备份现有数据卷**到 `pre-restore-backup/<时间戳>/`（`--no-backup` 可跳过）
4. 清理旧容器与数据卷（`compose down -v` + 兜底 `docker rm/volume rm`，并复查卷确已删除）
5. `docker load` 加载镜像
6. 起 PostgreSQL，等 `pg_isready`
7. `psql` 把 `sg.sql` 灌进 `sgateway`（`ON_ERROR_STOP=1`，失败即停并打印 psql 报错）
8. 把 `.secrets.json` 写进 `gateway-data` 卷（**gateway 启动前必须就位**）
9. 起 gateway，等 `/health` 200
10. **数据自检**：厂商数 / 模型数 / Key 数 / 日志数 / 讯飞CC 类型与模型数 / `api_keys.channels` 列 / 运行态 `provider_types` 是否含 `xfyun-cc`

### 方案 B：全新服务器（无旧容器）用 `migrate_restore.sh`

```bash
bash migrate_restore.sh
```

不删任何东西，只做「加载镜像 → 起 PG → 灌数据 → 放密钥 → 起 gateway → `/health`」。若目标库非空会因建表冲突失败，此时改用方案 A。

> ⚠️ 两个脚本都在 `migration-export/` 目录内执行（脚本会 `cd` 到自身所在目录）。
> 若在 Windows 编辑后拷到 Linux 出现 `$'\r': command not found`，执行 `sed -i 's/\r$//' deploy_restore.sh` 修正。

---

## 五点五、只更新镜像（不动数据，日常升级用）

服务器**已经在跑**、你只想把应用换成新镜像（例如本次的 `/v1` 折叠兼容 + `count_tokens`），
用这个脚本。它**不会** `down -v`、不备份/不删除数据卷、不重建 postgres、也不导入 `sg.sql`。

```bash
cd /path/to/deploy/migration-export
bash deploy_image_only.sh                 # 交互式更新（会提示将做哪些事）
bash deploy_image_only.sh --yes           # 免确认
bash deploy_image_only.sh --dry-run       # 只自检，不动手
bash deploy_image_only.sh --rollback      # 出问题回滚到上一次的镜像
```

流程与保障：

| 步骤 | 做什么 | 数据影响 |
|---|---|---|
| 1 | 自检 docker / compose / tar / compose.yml | 无 |
| 2 | 记录当前镜像 ID、postgres 容器 ID 与启动时间、数据卷列表 | 无 |
| 3 | 给当前镜像打 `simple-gateway:prev` 标签（回滚用） | 无 |
| 4 | `docker load -i simple-gateway.tar` | 无 |
| 5 | `docker compose -f docker-compose.server.yml up -d --no-deps gateway` | **不碰 postgres** |
| 6 | 轮询 `/health`，不通过则**自动回滚**并打印日志 | 无 |
| 7 | 核验：运行镜像 = 新镜像、postgres 容器 ID/启动时间未变、数据卷未变、厂商/模型/Key 行数仍在 | 只读查询 |

> 只重建 gateway 容器，`postgres-data` 与 `gateway-data`（含 `.secrets.json`）两个卷原样保留。
> 升级期间后台会有约 5–15 秒不可用（容器重启）。
> 注意 `--no-deps` 是刻意的：去掉它 compose 可能连带判断 postgres 是否需要重建。

---

## 六、验证

```bash
# 服务器本机
curl -fsS http://127.0.0.1:8020/health

# 浏览器访问后台
# http://<服务器IP>:8020/admin/
```

建议再调一次真实接口（如某厂商 OCR / SiliconFlow 模型列表），确认密钥与数据都通了。

本版本建议额外核对两项：
- 后台 →「模型与厂商」→ 讯飞CC →「测试」，应显示 `连接成功，上游返回 22 个模型`（base_url 必须是 `https://one.iflytek.com/api/llm/console/chat/v1`，带 `/v1`）；
- 编辑任意 API Key，「指定渠道」区域应**按服务各有一个下拉**（如 LLM / OCR 可分别钉到不同上游，互不影响）。

---

## 七、生产加固（务必做）

管理员口令**随数据一起迁移**（哈希存在 `meta` 表），服务器上请用你**本地当前**的管理员口令登录 —— 不是 `admin123`。
`SG_ADMIN_PASSWORD` 只在**空库首次初始化**时播种一次，不会覆盖已迁移的口令；日志里若出现 `SG_ADMIN_PASSWORD 仍为默认 admin123`，那只是环境变量默认值的提示，可忽略。

仍需固定：
- `SG_JWT_SECRET`：设为强随机值（固定后重启不会让已登录会话失效）
- `SG_FERNET_KEY`：一般不用改，保持 `.secrets.json` 即可；**该文件必须与数据库一起备份**，缺一则已加密的厂商凭证解不开

---

## 八、架构不一致时的回退方案（arm64 服务器）

镜像 tar 跨架构跑不了时，改用「源码 build」方式：

```bash
# 把整个项目目录（含 app/、Dockerfile、docker-compose.yml）传到服务器
scp -r "D:/SoftwareData/Workbuddy/simple gateway" <user>@<服务器IP>:/opt/simple-gateway-src
ssh <user>@<服务器IP>
cd /opt/simple-gateway-src
# 用自带 compose（含 build:）构建并启动
docker compose up -d --build
# 数据仍按上文「五」的 2~4 步恢复（postgres 起 → 灌 sg.sql → 放 .secrets.json）
```

源码 build 不依赖镜像架构，但要服务器能拉依赖（已配置清华镜像源加速）。

---

## 九、回滚 / 清理

- 本机已保留回滚备份：`backups/gateway.pre-pg.db` 与 `backups/gateway.pre-pg.secrets.json`（切 PG 前的 SQLite 状态）。
- 迁移成功后，本机 `simple-gateway`、`simple-gateway-postgres` 容器与两个卷可保留作热备，或 `docker compose down -v` 清理（**会删本机数据卷，确认服务器已验证通过后再做**）。
