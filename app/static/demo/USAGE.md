# OCR 中转服务 · 使用说明文档

> 统一对接多家云厂商 OCR 与私有化部署 OCR，对外提供**统一接口格式、统一鉴权、统一配额与用量统计**。

---

## 1. 核心能力

| 能力 | 说明 |
| --- | --- |
| 多厂商适配 | 腾讯云、阿里云、百度智能云、讯飞、华为云、火山引擎、**PaddleOCR 私有化**、**通用 HTTP 兼容** |
| 统一接口 | 无论上游是谁，对外只有 `/v1/ocr/recognize` 与 `/v1/ocr/recognize/file` 两个识别端点 |
| 鉴权 | 每个调用方一个 API Key（`ocr_sk_` 前缀），可单独设置配额、速率、可用厂商 |
| 路由降级 | 自动路由按「默认标记 → 降级权重 → 优先级」挑选上游，失败自动切下一个 |
| 用量统计 | 每次调用的耗时、字符数、成功/失败、命中厂商全部落库 |
| 凭证安全 | 上游厂商密钥用 Fernet 加密入库，API Key 只存哈希，明文仅创建时展示一次 |
| 配置即测 | 管理后台可对**单个提供商**发起真实调用测试，验证配置是否可用 |

---

## 2. 部署与访问地址

本项目以 Docker Compose 方式运行（自带 PostgreSQL）：

```bash
cd ocr-relay
# 首次运行前请编辑 .env，至少确认 CRYPTO_KEY / SECRET_KEY 已固定
docker compose up -d --build
```

| 入口 | 地址 |
| --- | --- |
| 管理后台 | `http://<服务地址>/admin/` |
| Swagger 交互文档 | `http://<服务地址>/docs` |
| 对外识别接口 | `http://<服务地址>/v1/ocr/recognize` |
| 健康检查 | `http://<服务地址>/health` |

> 本地默认端口为 **8010**（宿主机 8000 已被占用），可在 `docker-compose.yml` 的 `ports` 中调整。

---

## 3. 三步跑通

### 第 1 步：配置提供商

管理后台 → **提供商配置** → 新建，选择类型并填写凭证与接入设置。

- 凭证框中标有 <span style="color:#f87171">*</span> 的是**必填项**（腾讯云 `SecretId`/`SecretKey`、百度 `API Key`/`Secret Key` 等）；
- **新建时若必填凭证没填会被直接拦截**，避免存下一条永远调不通的配置；
- 已建的配置若缺凭证，列表里会显示红色标签「缺 secret_id/secret_key」，点进编辑页顶部也有醒目提示；
- 凭证出于安全**不回显**，编辑时留空即表示保留原值；只填写其中一项也**不会覆盖**其它项（后端为合并语义）。

以百度智能云为例：

| 位置 | 字段 | 值 |
| --- | --- | --- |
| 凭证 | API Key | 你的 API Key |
| 凭证 | Secret Key | 你的 Secret Key |
| 凭证 | AppID | 你的 AppID（可选，仅记录） |
| 接入设置 | OCR 接口地址 | `https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic` |

### 第 2 步：点击「测试」验证配置

在提供商列表点击 **测试**（或编辑弹窗里的 **测试连接**）：

- 可选上传一张自己的图片；不上传则使用内置测试图；
- 服务会用**已保存的真实凭证**实际调用一次上游 OCR；
- 返回调用成功/失败、耗时、识别到的文字。

> 失败时返回上游原始错误信息，并给出排查建议（密钥错误 / 接口地址不匹配 / 无接口权限 / 余额不足等）。

### 第 3 步：创建 API Key 并对外使用

管理后台 → **API Key 管理** → 新建，设置：

- **总配额**：该 Key 累计可调用次数，0 表示不限；
- **速率限制**：每分钟最多调用次数，0 表示不限；
- **允许调用的提供商**：勾选 `*` 表示全部，或只允许部分厂商；
- **过期时间**：可选。

明文 Key 仅在创建/重置时展示一次，形如 `ocr_sk_xxxxxxxxxxxxxxxx`。

---

## 4. 对外接口

鉴权统一使用请求头：

```
Authorization: Bearer <你的 API Key>
```

### 4.1 `POST /v1/ocr/recognize` —— JSON 识别

请求体字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `image_base64` | string | 二选一 | base64 图片（可带 `data:image/png;base64,` 前缀） |
| `image_url` | string | 二选一 | 图片 URL，由中转服务代为下载 |
| `provider` | string | 否 | 指定上游（类型或配置名）；留空走自动路由 |
| `engine` | string | 否 | 识别类型，如 `general` / `accurate_basic` / `idcard` / `handwriting` |
| `options` | object | 否 | 透传给上游的额外参数 |
| `with_raw` | bool | 否 | 是否返回上游原始响应（排障用） |

```bash
curl -X POST http://127.0.0.1:8010/v1/ocr/recognize \
  -H "Authorization: Bearer ocr_sk_xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"image_base64":"<BASE64>"}'
```

### 4.2 `POST /v1/ocr/recognize/file` —— 文件上传识别

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `file` | file | 是 | 图片文件（jpg / png / bmp / tiff / pdf） |
| `provider` | string | 否 | 同上 |
| `engine` | string | 否 | 同上 |
| `options` | string | 否 | **JSON 字符串**，如 `"{\"id_card_side\":\"front\"}"` |

```bash
curl -X POST http://127.0.0.1:8010/v1/ocr/recognize/file \
  -H "Authorization: Bearer ocr_sk_xxxxxxxx" \
  -F "file=@./test.png"
```

### 4.3 `GET /v1/ocr/types` —— 查看支持的提供商类型

返回当前服务已适配的厂商类型及其所需配置项，用于动态渲染调用方界面。

---

## 5. 响应结构

无论上游是哪家，返回结构完全一致：

```json
{
  "request_id": "0f3a1c2e-....",
  "provider": "baidu",
  "engine": null,
  "text": "识别出的全文",
  "blocks": [
    {
      "type": "text",
      "text": "单行文字",
      "bbox": [12, 30, 260, 62],
      "polygon": null,
      "confidence": 0.98,
      "line_id": 0,
      "index": 0
    }
  ],
  "pages": 1,
  "char_count": 42,
  "cost_ms": 386,
  "raw": null
}
```

| 字段 | 说明 |
| --- | --- |
| `request_id` | 本次请求唯一 ID，便于排查 |
| `provider` | 实际命中的上游类型 |
| `engine` | 实际使用的识别引擎 |
| `text` | 拼接后的完整文本 |
| `blocks` | 结构化结果，`bbox` 为 `[x1,y1,x2,y2]` 像素坐标 |
| `pages` | 页数（PDF 等多页场景） |
| `char_count` | 识别字符数，用量统计依据 |
| `cost_ms` | 本次调用耗时（毫秒） |
| `raw` | 上游原始响应，仅当 `with_raw=true` 时返回 |

---

## 6. 错误码

| HTTP | 场景 | 处理建议 |
| --- | --- | --- |
| 400 | 图片解析失败 / 未提供图片 | 检查 `image_base64` 是否为合法 base64 |
| 401 | 缺少 Key 或 Key 无效 | 检查 `Authorization: Bearer <key>` |
| 403 | Key 已停用或已过期 | 在管理后台启用或重建 Key |
| 429 | 超出总配额或速率限制 | 提额或等待限流窗口 |
| 502 | 无可用上游，或所有上游调用失败 | 在后台用「测试」逐个验证提供商配置 |
| 504 | 上游超时 | 调大超时或更换厂商 |

---

## 7. 路由与降级规则

1. 请求带 `provider` → **只调用该提供商**，失败直接返回 502，**不降级**；
2. 请求不带 `provider` → 按 **默认标记 → 降级权重(大者优先) → 优先级(小者优先)** 依次尝试；
3. 某个上游失败 → 自动切下一个，全部失败才返回 502；
4. API Key 的「允许调用的提供商」在路由前过滤，被禁用的厂商不会被调用。

---

## 8. 调用 Demo 下载

管理后台 → **使用说明** 页面可直接下载：

| 文件 | 说明 |
| --- | --- |
| `demo_python.py` | Python（requests）示例，覆盖文件上传 / base64 / URL 三种方式 |
| `demo_curl.sh` | curl 示例，含响应格式化输出 |
| `demo_node.js` | Node.js 18+ 示例（内置 fetch，无需第三方依赖） |
| `USAGE.md` | 本文档 |

运行示例前设置两个环境变量：

```bash
export OCR_BASE_URL=http://127.0.0.1:8010
export OCR_API_KEY=ocr_sk_xxxxxxxx
python demo_python.py ./test.png
```

---

## 9. 常见问题

**Q：管理后台看不到刚建的提供商？**
A：列表按「默认 → 权重 → 优先级」排序；确认没有填错名称。若刚保存过请刷新页面。

**Q：测试提示「腾讯云 OCR 未配置凭证：缺少 secret_id、secret_key」怎么办？**
A：这条配置的密钥**没有保存成功**（多半是当时凭证框没填、或切换了类型导致已填内容被清空）。
到「提供商配置 → 编辑 → 凭证」，把带 `*` 的必填项补齐并保存，列表上的红色「缺 …」标签消失后再点测试。
若提示的是密钥无效 / 无接口权限 / 余额不足，则是云厂商侧问题，见下一条。

**Q：调用返回 502 怎么办？**
A：多数是上游配置问题。到「提供商配置」逐个点「测试」，错误信息会直接显示（如密钥无效、接口无权限、地址不通）。

**Q：提示"没有可用且被当前 API Key 允许的 OCR 提供商"？**
A：该 Key 的「允许调用的提供商」没有勾选任何已启用的厂商，或所有上游都已停用。

**Q：如何让某个厂商只处理特定票种（如身份证）？**
A：给每个厂商单独建一条配置，接口调用时带 `provider=配置名` + `engine=idcard` 即可精确路由。

**Q：配额是按什么扣减？**
A：只有**实际进入处理流程**的请求才计数；因配额/限流被直接拒绝的请求（429）不会扣减。

**Q：服务重启后已配置的厂商凭证还在吗？**
A：在。凭证加密入库，只要 `.env` 里的 `CRYPTO_KEY` 不变，重启后仍可正常解密。**修改 `CRYPTO_KEY` 会导致旧密文无法解密。**

---

## 10. 运维速查

```bash
docker compose ps            # 查看容器状态
docker compose logs -f app   # 实时日志
docker compose restart app   # 重启应用
docker compose down          # 停止（数据卷保留）
docker compose up -d --build # 重新构建并启动
```

- 数据库持久化在 Docker 卷 `pgdata`，`down` 不会丢数据；
- 生产环境务必修改 `SECRET_KEY`、`CRYPTO_KEY`、`ADMIN_PASSWORD`；
- 管理后台建议置于反向代理 + HTTPS 之后；
- 速率限制为**进程内**实现，多实例部署需改为 Redis（见 `app/deps.py` 注释）。
