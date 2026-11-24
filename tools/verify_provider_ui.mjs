// 真机浏览器验收：附加字段显示 + 复制配置后默认进编辑页
// 用法：node tools/verify_provider_ui.mjs
// 前置：本地 8030 端口跑着隔离测试库的服务；Chrome headless 开 9333 调试端口
const BASE = "http://127.0.0.1:8030";
const CDP = "http://127.0.0.1:9333";
const USER = "admin";
const PASS = "Ui-Test-2026!Abc";
const NAME = "移动云152OCR";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let passed = 0;
function ok(cond, label, detail = "") {
  if (!cond) {
    console.error("  [FAIL]", label, detail);
    throw new Error(label + " " + detail);
  }
  passed++;
  console.log("  [PASS]", label);
}

async function rpc(ws, method, params = {}) {
  const id = (rpc.n = (rpc.n || 0) + 1);
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error("timeout " + method)), 20000);
    const onMsg = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id === id) {
        clearTimeout(t);
        ws.removeEventListener("message", onMsg);
        m.error ? reject(new Error(method + ": " + JSON.stringify(m.error))) : resolve(m.result);
      }
    };
    ws.addEventListener("message", onMsg);
  });
}
async function evalJs(ws, expr) {
  const r = await rpc(ws, "Runtime.evaluate", {
    expression: expr, returnByValue: true, awaitPromise: true,
  });
  if (r.exceptionDetails) throw new Error("JS: " + JSON.stringify(r.exceptionDetails));
  return r.result.value;
}

const login = await fetch(BASE + "/api/admin/login", {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ username: USER, password: PASS }),
}).then((r) => r.json());
if (!login.access_token) throw new Error("登录失败: " + JSON.stringify(login));
const H = { "Content-Type": "application/json", Authorization: "Bearer " + login.access_token };
const api = (path, opts = {}) => fetch(BASE + path, { headers: H, ...opts }).then(async (r) => {
  const t = await r.text();
  return t ? JSON.parse(t) : null;
});

console.log("\n[0] 准备与生产同构的测试数据");
const existing = await api("/api/admin/providers?service=ocr");
for (const p of existing.filter((x) => x.name.startsWith(NAME))) {
  await api("/api/admin/providers/" + p.id, { method: "DELETE" });
}
const SRC = {
  service: "ocr", type: "generic", name: NAME,
  is_enabled: true, is_default: true, weight: 88, priority: 7, timeout_s: 45,
  credentials: { api_key: "secret-abc-1234" },
  settings: {
    url: "http://223.111.149.152:8090/tuling/uocr/v2/recognize",
    image_mode: "multipart",
    extra_fields: { category: "", trackId: "gateway" },
  },
  comment: "UI 验收用",
};
const src = await api("/api/admin/providers", { method: "POST", body: JSON.stringify(SRC) });
ok(src && src.id, "测试配置已创建（附加字段为 JSON 对象）");
const srcId = src.id;

const tgt = await fetch(`${CDP}/json/new?${encodeURIComponent(BASE + "/admin/")}`, { method: "PUT" })
  .then((r) => r.json());
const ws = new WebSocket(tgt.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r));
await rpc(ws, "Page.enable");
await rpc(ws, "Runtime.enable");
await sleep(800);
await rpc(ws, "Runtime.evaluate", {
  expression: `localStorage.setItem('sg_token', ${JSON.stringify(login.access_token)})`,
});
await rpc(ws, "Page.reload", { ignoreCache: true });
await sleep(2200);
await evalJs(ws, `location.hash = '#/providers'`);
await sleep(1200);
await evalJs(ws, `document.querySelector('[data-svc="ocr"]')?.click()`);
await sleep(900);

const pickCard = `(name) => [...document.querySelectorAll('.provider-card')]
  .find(x => x.querySelector('.pname')?.textContent.includes(name))`;

console.log("\n[1] 列表卡片直接看到附加字段实际内容");
const cardText = await evalJs(ws, `(${pickCard})(${JSON.stringify(NAME)})?.textContent || ''`);
ok(cardText.includes('"trackId"') && cardText.includes('"category"'),
  "卡片展示附加字段的完整 JSON", cardText.slice(0, 300));
ok(!cardText.includes("[object Object]"), "卡片不再出现 [object Object]");
ok(!!(await evalJs(ws, `!!(${pickCard})(${JSON.stringify(NAME)})?.querySelector('[data-copy-setting]')`)),
  "卡片提供附加字段「复制」按钮");

console.log("\n[2] 编辑弹窗显示真实内容（不再是 [object Object]）");
await evalJs(ws, `(${pickCard})(${JSON.stringify(NAME)}).querySelector('[data-edit]').click()`);
await sleep(400);
const taValue = await evalJs(ws,
  `document.querySelector('#modal-root textarea[name="set_extra_fields"]')?.value ?? null`);
ok(!!taValue, "附加字段渲染为多行文本框");
ok(taValue.includes('"trackId": "gateway"') && taValue.includes('"category": ""'),
  "文本框显示实际 JSON 内容", String(taValue));
ok(!taValue.includes("[object Object]"), "文本框不再出现 [object Object]");

console.log("\n[3] 防呆：损坏值不允许再写回数据库");
await evalJs(ws, `(() => {
  document.querySelector('#modal-root textarea[name="set_extra_fields"]').value = '[object Object]';
  document.querySelector('#modal-root #save').click(); return true;
})()`);
await sleep(400);
ok(await evalJs(ws, `!!document.querySelector('#modal-root .modal')`), "含 [object Object] 的保存被拦截");
const guard = await api("/api/admin/providers/" + srcId);
ok(JSON.stringify(guard.settings.extra_fields) === JSON.stringify({ category: "", trackId: "gateway" }),
  "源配置的附加字段未被写坏", JSON.stringify(guard.settings.extra_fields));

console.log("\n[4] 修改附加字段后保存，仍是 JSON 对象");
await evalJs(ws, `(() => {
  document.querySelector('#modal-root textarea[name="set_extra_fields"]').value =
    '{"category":"A","trackId":"gw2"}';
  document.querySelector('#modal-root #save').click(); return true;
})()`);
await sleep(700);
const edited = await api("/api/admin/providers/" + srcId);
ok(edited.settings.extra_fields.category === "A" && edited.settings.extra_fields.trackId === "gw2",
  "编辑内容以对象形式存回 settings", JSON.stringify(edited.settings.extra_fields));
ok(typeof edited.settings.extra_fields === "object", "不是一整串 JSON 文本");

console.log("\n[5] 点「复制」→ 默认打开编辑页 + 名称追加（副本）");
await evalJs(ws, `(${pickCard})(${JSON.stringify(NAME)}).querySelector('[data-dup]').click()`);
await sleep(900);
const modal = await evalJs(ws, `(() => {
  const m = document.querySelector('#modal-root .modal');
  if (!m) return null;
  return {
    title: m.querySelector('h3')?.textContent || '',
    name: m.querySelector('[name="name"]')?.value || '',
    hint: m.querySelector('.modal-body .hint-box')?.textContent || '',
    extra: m.querySelector('textarea[name="set_extra_fields"]')?.value || '',
    enabled: m.querySelector('[name="is_enabled"]')?.checked,
    isDefault: m.querySelector('[name="is_default"]')?.checked,
  };
})()`);
ok(!!modal, "复制后弹窗已打开");
ok(modal.title.includes("编辑配置"), "默认是编辑页面", modal.title);
ok(modal.name === NAME + "（副本）", "名称自动追加（副本）", modal.name);
ok(modal.hint.includes("副本") && modal.hint.includes("停用"), "编辑页提示副本默认停用", modal.hint.slice(0, 120));
ok(modal.extra.includes('"category": "A"'), "副本带上了附加字段实际内容", modal.extra);
ok(modal.enabled === false && modal.isDefault === false, "副本未启用、未设为默认");

console.log("\n[6] 保存副本并核对服务端数据");
await evalJs(ws, `document.querySelector('#modal-root #save').click()`);
await sleep(800);
const list2 = await api("/api/admin/providers?service=ocr");
const dup = list2.find((p) => p.name === NAME + "（副本）");
ok(!!dup, "副本已落库并出现在列表");
ok(dup.is_enabled === false && dup.is_default === false, "副本停用且不抢默认位");
ok(dup.has_credentials === true, "副本保留了加密凭证");
ok(JSON.stringify(dup.settings.extra_fields) === JSON.stringify(edited.settings.extra_fields),
  "副本附加字段与源一致", JSON.stringify(dup.settings.extra_fields));
ok(dup.weight === 88 && dup.priority === 7 && dup.timeout_s === 45, "权重/优先级/超时一并复制");
const srcAfter = list2.find((p) => p.id === srcId);
ok(srcAfter.is_enabled === true && srcAfter.is_default === true, "源配置状态未变");

// 截图存证（列表页，能看到附加字段与复制按钮）
await rpc(ws, "Runtime.evaluate", { expression: `location.hash = '#/providers'` });
await sleep(800);
const shot = await rpc(ws, "Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
const fs = await import("node:fs");
fs.writeFileSync("shots/providers-fix-20261113.png", Buffer.from(shot.data, "base64"));
console.log("  [SHOT] shots/providers-fix-20261113.png");

// 清理测试数据
for (const p of list2.filter((x) => x.name.startsWith(NAME))) {
  await api("/api/admin/providers/" + p.id, { method: "DELETE" });
}
ws.close();
await fetch(`${CDP}/json/close/${tgt.id}`);
console.log(`\n全部通过：${passed} 项浏览器断言`);
