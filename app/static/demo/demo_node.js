/**
 * OCR 中转服务 · Node.js 调用示例（Node 18+ 内置 fetch，无需第三方依赖）
 *
 * 用法：
 *   export OCR_BASE_URL=http://127.0.0.1:8010
 *   export OCR_API_KEY=ocr_sk_xxxxxxxxxxxxxxxx
 *   node demo_node.js ./test.png
 */
const fs = require("fs");
const path = require("path");

const BASE_URL = (process.env.OCR_BASE_URL || "http://127.0.0.1:8010").replace(/\/$/, "");
const API_KEY = (process.env.OCR_API_KEY || "").trim();

async function handle(res) {
  const text = await res.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch (e) {
    data = { raw: text };
  }
  if (!res.ok) {
    throw new Error(`调用失败 HTTP ${res.status}: ${data.detail || text}`);
  }
  return data;
}

/** 方式一：文件上传 */
async function recognizeFile(filePath, provider, engine) {
  const buf = fs.readFileSync(filePath);
  const form = new FormData();
  form.append("file", new Blob([buf]), path.basename(filePath));
  if (provider) form.append("provider", provider);
  if (engine) form.append("engine", engine);
  const res = await fetch(`${BASE_URL}/v1/ocr/recognize/file`, {
    method: "POST",
    headers: { Authorization: `Bearer ${API_KEY}` },
    body: form,
  });
  return handle(res);
}

/** 方式二：base64 JSON */
async function recognizeBase64(filePath, provider, engine) {
  const b64 = fs.readFileSync(filePath).toString("base64");
  const payload = { image_base64: b64 };
  if (provider) payload.provider = provider;
  if (engine) payload.engine = engine;
  const res = await fetch(`${BASE_URL}/v1/ocr/recognize`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  return handle(res);
}

/** 方式三：图片 URL */
async function recognizeUrl(url, provider, engine) {
  const payload = { image_url: url };
  if (provider) payload.provider = provider;
  if (engine) payload.engine = engine;
  const res = await fetch(`${BASE_URL}/v1/ocr/recognize`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  return handle(res);
}

function show(tag, res) {
  console.log(`\n===== ${tag} =====`);
  console.log(
    `provider=${res.provider} engine=${res.engine} 耗时=${res.cost_ms}ms ` +
      `字符数=${res.char_count} 请求ID=${res.request_id}`
  );
  console.log("---- 识别文本（前 800 字）----");
  console.log((res.text || "(空)").slice(0, 800));
  if (res.blocks && res.blocks.length) {
    console.log("---- 区块样例（前 3 条）----");
    res.blocks.slice(0, 3).forEach((b) =>
      console.log({ type: b.type, text: b.text, bbox: b.bbox, confidence: b.confidence })
    );
  }
}

(async () => {
  if (!API_KEY) {
    console.error("请先设置环境变量 OCR_API_KEY（在管理后台「API Key 管理」中创建）");
    process.exit(1);
  }
  const file = process.argv[2] || "test.png";
  if (!fs.existsSync(file)) {
    console.error(`图片不存在：${file}`);
    process.exit(1);
  }
  console.log(`服务地址：${BASE_URL}`);
  show("方式一：文件上传", await recognizeFile(file));
  show("方式二：base64 JSON", await recognizeBase64(file));
})().catch((e) => {
  console.error(e.message);
  process.exit(1);
});
