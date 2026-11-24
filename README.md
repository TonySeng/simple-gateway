# Simple Gateway · 统一 AI 能力中转网关

把 **OCR 中转** 与 **LLM 中转** 合并成一个应用：一个进程、一套管理后台、一套 API Key、
一套用量统计。对外仍然是两个各自熟悉的接口风格，对内共用同一套厂商配置、鉴权、配额与日志。

设计目标是**可插拔**：新增一个中转服务（ASR、翻译、向量化、图像生成……）不需要改数据库、
不需要改管理后台，只需要新增一个服务包并注册 `ServiceSpec`。

```
                       ┌──────────────────────────── Simple Gateway ────────────────────────────┐
  客户端 ── Bearer ──▶ │  统一鉴权（服务范围 / 厂商白名单 / 配额 / 限流）                        │
                       │        │                                                                │
                       │        ├── OCR 服务   /v1/ocr/*        ──▶ 腾讯/阿里/百度/讯飞/华为/火山  │
                       │        │                                  PaddleOCR / 通用 HTTP           │
                       │        ├── LLM 服务   /v1/chat/completions                        │
                       │        │              /v1/messages           ──▶ OpenAI 兼容 / Anthropic │
                       │        └── 未来服务    /v1/...               ──▶ 任意上游                 │
                       │                                                                          │
                       │   统一存储：providers · models · api_keys · request_logs                 │
                       │   统一后台：仪表盘 · 服务与提供商 · Key · 用量日志 · 接入说明            │
                       └──────────────────────────────────────────────────────────────────────────┘
```

## 功能特性

**统一层**

- 一套 `providers` 表承载所有服务的上游接入配置：凭证 Fernet 加密入库、settings 明文、
  权重/优先级/默认标记驱动路由与降级。
- 一套 `api_keys`：一个 Key 可同时授权 OCR 与 LLM，也可只授权其一；可分别限定每个服务下
  允许调用的厂商；配额支持 **请求次数 / tokens / 费用** 三种维度，另有每分钟速率限制。
- 一套 `request_logs`：带 `service` 维度，仪表盘、趋势、按模型/厂商/Key 聚合、错误详情一屏看全。
- 统一的「配置即测」：任意服务的任意厂商，用已保存的真实凭证实际打一次上游，回显耗时、
  识别文本或模型列表、以及可读的失败原因。

**OCR 服务**

- 8 类适配器：腾讯云、阿里云、百度智能云、讯飞、华为云、火山引擎、私有化 PaddleOCR、通用 HTTP。
- 统一返回：全文 + 结构化区块（文本/bbox/polygon/置信度）+ 页数 + 字符数 + 耗时。
- 自动降级：默认标记 → 权重大 → 优先级小，依次尝试；指定 `provider` 时失败不降级。

**LLM 服务**

- 入站同时兼容 **OpenAI**（`/v1/chat/completions`）与 **Anthropic**（`/v1/messages`），支持 SSE 流式。
- 出站自动做协议转换：OpenAI 入 → Anthropic 上游、Anthropic 入 → OpenAI 上游均可。
- 模型管理：对外模型名 → 上游真实模型 ID，按 1K tokens 配置输入/输出单价，自动计费。

## 目录结构

```
simple gateway/
├── app/
│   ├── main.py                 应用入口：装配服务路由 + 管理接口 + 静态后台
│   ├── core/                   统一核心层
│   │   ├── config.py           SG_ 前缀环境变量、密钥自动生成与持久化
│   │   ├── db.py               引擎/会话/声明基类
│   │   ├── models.py           Provider / Model / ApiKey / RequestLog
│   │   ├── crypto.py           Fernet 字段级加密
│   │   ├── security.py         Key 生成与哈希、JWT、常量时间口令比较
│   │   ├── deps.py             管理员鉴权、服务级 API Key 鉴权（配额/限流）
│   │   ├── providers.py        凭证读写、厂商选择（权重/优先级）
│   │   ├── usage.py            统一记账
│   │   ├── registry.py         服务注册表（可插拔核心）
│   │   └── schemas.py          管理端契约
│   ├── services/               各中转服务（每个子包 = 一个服务）
│   │   ├── ocr/
│   │   │   ├── service.py      ServiceSpec 注册 + 配置即测
│   │   │   ├── routing.py      厂商注册表 + 降级路由
│   │   │   ├── router.py       /v1/ocr/*
│   │   │   └── providers/      8 个适配器 + 签名/归一化
│   │   └── llm/
│   │       ├── service.py      ServiceSpec 注册 + 模型管理接口
│   │       ├── router.py       /v1/chat/completions、/v1/messages、/v1/models
│   │       ├── proxy.py        转发、流式、计费
│   │       ├── dispatch.py     出站请求构造
│   │       └── translators.py  OpenAI ↔ Anthropic 转换
│   ├── admin/                  管理接口（服务无关，全泛化）
│   └── static/                 原生 JS 单页后台（无 CDN，可离线）
├── tools/migrate_legacy.py     旧 OCR Relay / LLM Gateway 数据迁移
├── tools/migrate_to_pg.py       SQLite → PostgreSQL 迁移（切换数据库时用）
├── tests/smoke.py              端到端冒烟测试（52 项断言）
├── Dockerfile · docker-compose.yml · requirements.txt · .env.example
```

## 快速开始

### 方式一：Docker（推荐）

```bash
cp .env.example .env         # 至少改掉 SG_ADMIN_PASSWORD
docker compose up -d --build
```

打开 <http://localhost:8020/admin/>，用 `SG_ADMIN_USERNAME / SG_ADMIN_PASSWORD` 登录（默认 `admin / admin123`）。

- 数据持久化：PostgreSQL 数据卷 `postgres-data`（库本身）+ `gateway-data`（运行密钥 `.secrets.json` 与静态资源），重建容器不丢配置与已加密凭证。
- 宿主端口在 `docker-compose.yml` 的 `ports` 中调整（默认 **8020**，避开常见 8000/8010 占用）。
- 默认已用内置 PostgreSQL（`postgres` 服务，数据卷 `postgres-data`，仅 compose 网络内可达）。如需回退 SQLite，把 `SG_DATABASE_URL` 改成 `sqlite:////app/data/gateway.db` 即可；外部独立 PG 则填对应连接串。

### 方式二：本地运行

```bash
pip install -r requirements.txt
python run.py                # 默认 http://0.0.0.0:8000
```

## 管理后台

| 页面 | 内容 |
|------|------|
| 仪表盘 | 总请求/成功率/tokens/费用、按天趋势、分服务概览、最近调用 |
| 服务与提供商 | 按服务分标签页；厂商卡片支持启停、权重/优先级、默认标记、配置即测；LLM 额外有模型与单价管理 |
| API Key | 跨服务授权、服务级厂商白名单、三种配额、速率限制、过期时间；明文仅创建时展示一次 |
| 用量与日志 | 按服务/状态/Key/模型/时间筛选，展开可见错误原因 |
| 接入说明 | 每个服务的接口清单与调用示例、OCR Demo 下载、扩展新服务的步骤 |

厂商表单是**动态渲染**的：字段全部来自服务注册表里的 `ProviderTypeSpec`，所以新增厂商类型
（或新增服务）后，后台自动出现对应输入框，不需要写前端代码。

## 对外接口

Base URL：`http://<host>:<port>`，鉴权：`Authorization: Bearer <网关 Key>`。

### OCR

```bash
# base64 / URL
curl -X POST http://localhost:8020/v1/ocr/recognize \
  -H "Authorization: Bearer $SG_KEY" -H "Content-Type: application/json" \
  -d '{"image_base64":"<BASE64>","provider":"tencent","engine":"idcard"}'

# 文件上传
curl -X POST http://localhost:8020/v1/ocr/recognize/file \
  -H "Authorization: Bearer $SG_KEY" \
  -F "file=@idcard.jpg" -F "provider=baidu"
```

```json
{
  "request_id": "…", "service": "ocr", "provider": "tencent", "provider_name": "腾讯云OCR",
  "text": "识别全文…",
  "blocks": [{"type":"text","text":"…","bbox":[23,78,536,119],"confidence":100}],
  "pages": 1, "char_count": 123, "cost_ms": 412
}
```

`engine` 与 `provider` 说明：

- `provider` 可填厂商 `type`（如 `tencent`）或配置名（如 `腾讯云OCR`）；留空走自动路由。
- 腾讯云 `engine`：`general|general_basic`、`accurate|general_accurate`、`handwriting`、`idcard`、
  `bizlicense`、`bankcard`、`vin`、`plate`、`formula`、`table`、`waybill`。

### LLM

```bash
# OpenAI 兼容（支持 stream:true）
curl http://localhost:8020/v1/chat/completions \
  -H "Authorization: Bearer $SG_KEY" -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"你好"}]}'

# Anthropic 兼容
curl http://localhost:8020/v1/messages \
  -H "Authorization: Bearer $SG_KEY" -H "Content-Type: application/json" \
  -d '{"model":"claude-3-5-sonnet","max_tokens":1024,"messages":[{"role":"user","content":"你好"}]}'

# 可用模型
curl http://localhost:8020/v1/models -H "Authorization: Bearer $SG_KEY"
```

> 客户端只需把 Base URL 指向网关、Key 换成网关 Key，即可在不同上游（OpenAI / Anthropic /
> DeepSeek / 硅基流动 / 私有化 vLLM / Ollama）之间无感切换。

#### Claude Code / Anthropic SDK 接入（务必注意 base_url）

Anthropic SDK 会**自己在 base_url 后拼 `/v1/messages`**，所以 base_url 只能是网关根地址，
**不能带 `/v1`**（这是 OpenAI SDK 的习惯，两者不同）：

```jsonc
// ~/.claude/settings.json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://<网关IP>:8020",   // ← 不要写成 .../v1
    "ANTHROPIC_AUTH_TOKEN": "sg_sk_xxx",
    "ANTHROPIC_MODEL": "claude-opus-5"
  }
}
```

写成 `.../v1` 时实际请求会变成 `/v1/v1/messages` 而 404，Claude Code 会把这种 404
**误报成「There's an issue with the selected model (xxx). It may not exist or you may not
have access to it.」** —— 看到这句话先查 base_url，别急着换模型。

网关已做兼容：重复的 `/v1/v1/` 会被自动折叠；同时实现了 Claude Code 会用到的
`POST /v1/messages/count_tokens`。即便如此，仍建议按上面的规范填 base_url。

## 新增一个中转服务（可插拔）

以「语音识别」为例，三步：

1. **建包** `app/services/asr/`，写 `router.py`（对外接口）和 `service.py`。
2. **注册服务**：

```python
from app.core.registry import ServiceSpec, ProviderTypeSpec, register_service

register_service(ServiceSpec(
    key="asr",                      # 唯一 key，会成为日志/Key 授权里的 service
    label="语音识别",
    description="统一对接云端与私有化 ASR",
    order=30,                       # 后台标签排序
    usage_units="秒",
    endpoints=[{"method": "POST", "path": "/v1/asr/transcribe", "summary": "语音转写"}],
    provider_types=[ProviderTypeSpec(
        type="generic", label="通用 HTTP ASR",
        fields=[{"name": "api_key", "label": "API Key", "secret": True, "required": False}],
        setting_fields=[{"name": "url", "label": "服务地址", "placeholder": "http://…"}],
    )],
    router=router,
    test_provider=test_provider,    # async (db, provider, file_bytes, mime, params) -> dict
))
```

3. 启动时 `app.core.registry.load_services()` 会自动发现并装配。

完成即可获得：后台自动出现「语音识别」标签页与动态凭证表单、API Key 可勾选该服务、
调用日志与仪表盘自动带 `asr` 维度、`/v1/asr/transcribe` 自动挂载 —— **零数据库迁移、零前端改动**。

## 切换到 PostgreSQL（SQLite → PostgreSQL）

`docker-compose.yml` 默认已内置 `postgres` 服务（数据卷 `postgres-data`），网关通过
`SG_DATABASE_URL` 连接；**`postgres` 仅在 compose 网络内可达，未暴露宿主机端口**。

若要把已有的 SQLite 数据迁进来（脚本见 `tools/migrate_to_pg.py`）：

```bash
# 在能同时访问 SQLite 文件与 PG 的容器内执行
python tools/migrate_to_pg.py \
  --sqlite /app/data/gateway.db \
  --pg "postgresql+psycopg://sg:sgpass@postgres:5432/sgateway"
# 目标已有数据时清空重导加 --drop
```

要点：

- 所有表主键为短随机串（`new_id()`），脚本按 `providers→models→api_keys→request_logs→meta`
  整行拷贝、保留原 id，**无自增序列冲突**。
- **运行密钥 `.secrets.json` 固定在数据目录、与数据库类型无关**——切换数据库时必须保证这一点，
  否则进程会自生成新 Fernet 密钥，导致既有加密凭证无法解密（见 `app/core/config.py`
  的 `_default_secrets_file`）。`docker-compose.yml` 已显式设置 `SG_SECRETS_FILE=/app/data/.secrets.json`。
- 回退 SQLite 只需把 `SG_DATABASE_URL` 改回 `sqlite:////app/data/gateway.db`，两套库互不影响。

## 数据模型

| 表 | 说明 |
|----|------|
| `providers` | 所有服务的上游配置：`service` + `type` + 名称唯一；凭证加密、settings 明文、weight/priority/is_default |
| `models` | LLM 模型：对外名称、上游 ID、输入/输出单价（每 1K tokens）、上下文长度 |
| `api_keys` | 统一 Key：`allowed_services`、`allowed_providers`（按服务）、三种配额与用量、速率限制、`safety_policy_id`（审查规则绑定） |
| `request_logs` | 统一日志：`service`、厂商、模型、tokens、费用、字符数、耗时、错误、IP，以及 `safety_result/safety_policy/safety_detail` |
| `safety_libraries` | 敏感词库：名称、说明、多行词条（支持 `re:` 正则）、启用状态 |
| `safety_policies` | 审查规则：引用词库、输入/输出方向、匹配方式、命中阈值、默认回复、异常策略 |
| `safety_reviews` | 独立审查记录：命中词、词库、方向、动作、原文摘录、request_id |
| `app_settings` | 后台可改的运行时配置 |
| `meta` | 键值元信息 |

## 从旧项目迁移

旧 OCR Relay（PostgreSQL/SQLite）与 LLM Gateway（SQLite）的数据可以一条命令并入：

```bash
# 旧 OCR：需要旧 .env 里的 CRYPTO_KEY 才能解密凭证
# 旧 LLM：密钥在数据卷 /app/data/.secrets.json 的 fernet_key（也可直接指向该文件）
python -m tools.migrate_legacy \
  --ocr-url "postgresql+psycopg://ocr:ocr123@127.0.0.1:5432/ocr_relay" \
  --ocr-key "$OLD_CRYPTO_KEY" \
  --llm-sqlite "../LLM Tools/llm-gateway/data/gateway.db" \
  --llm-secrets "../LLM Tools/llm-gateway/data/.secrets.json"

# 需要历史调用日志时加 --with-logs
```

迁移行为：

- 厂商、模型、Key 全部映射到新表；**Key 的 hash 原样保留**，客户端不需要换密钥。
- 凭证用旧密钥解密后用新密钥重新加密，因此迁移后可直接使用。
- 已存在的同名厂商 / 同 hash Key 自动跳过，可安全重复执行。
- 旧的 OCR Key 会映射为 `allowed_services=["ocr"]` 并按原 `allowed_providers` 限定厂商；
  旧的 LLM Key 映射为 `allowed_services=["llm"]`，`tokens/price` 配额映射到 `quota_tokens/quota_cost`。

## 安全与运维

- **凭证加密**：所有厂商密钥 Fernet 加密入库，接口只回显掩码（`****abcd`），转发瞬间内存解密。
- **Key 只存哈希**：对外 Key 仅保存 SHA-256，明文只在创建/重置时展示一次。
- **密钥持久化**：`SG_JWT_SECRET` / `SG_FERNET_KEY` 留空时自动生成并写入数据目录 `.secrets.json`（0600），
  容器重建不失效；**该文件与数据库必须一起备份**，丢失后已加密凭证无法解密。
- **安全审查**：后台「安全审查」页维护多套敏感词库与审查规则；每个 API Key 可单独绑定规则
  （继承默认 / 关闭审查 / 指定规则）。默认按关键词子串匹配，支持 `re:` 正则、全半角/零宽/大小写归一化、
  命中阈值与输入/输出方向。输入命中直接返回默认内容且不调用上游；输出命中替换为默认内容；
  流式输出会整段缓冲后再判定，保证命中内容不会已经发给客户端。命中详情写入独立 `safety_reviews` 表，
  请求日志的 `safety_result` 直接标注 `passed / blocked_input / blocked_output / error`。
- **内置安全模板**：首次启动自动安装 7 套词库 + 4 条规则（`app/core/safety_bootstrap.py`）：
  - `内置·日常安全基线`（默认，输入+输出）：注入越狱、违法违规、恶意代码、凭证泄露，低误伤，所有未绑定 Key 自动继承；
  - `内置·个人隐私防护`（按需绑定）：手机号、身份证、银行卡、邮箱、IPv4；
  - `内置·数据防泄漏`（按需绑定）：涉密标记与内部文档术语，招标/评标场景请勿绑定；
  - `内置·输出合规复核`（按需绑定，仅输出）：模型自称绕过限制等回复。
  - 内置项与自建项一样可直接编辑、停用、删除；启动时只补齐缺失项，**不覆盖用户修改**。
  后台「安全审查」页顶部可手动「补齐缺失」或「恢复出厂」（只覆盖内置项，自建数据不受影响）。
- **非 root 容器** + `/health` 健康检查 + `restart: unless-stopped`。
- **限流**为进程内实现，多 worker / 多实例部署请替换为 Redis（改 `app/core/deps.py` 中的 `_rate_limited` 即可）。
- 公网暴露前：改掉默认管理员口令、固定 JWT/Fernet 密钥、置于 Nginx/Caddy 后启用 HTTPS、
  生产已默认使用内置 PostgreSQL（数据卷 `postgres-data`）；如需独立 PG 实例，把 `SG_DATABASE_URL` 指向外部库即可。

## 测试

```bash
python -m tests.smoke
python -m tests.test_safety_review
python -m tests.test_safety_builtins
```

52 项断言覆盖：启动建表、登录与鉴权、统一厂商 CRUD 与凭证合并语义、模型管理、
跨服务 Key 授权、服务范围拒绝（403）、次数配额（429）、OpenAI/Anthropic 双向转发、
SSE 流式计费、OCR 自动路由与失败降级、配置即测、日志与统计、Key 重置。
