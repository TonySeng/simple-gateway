// 生产只读验收：确认附加字段显示与数据修复生效，不写任何生产数据。
// 前置：ssh -N -L 18020:127.0.0.1:8020 ubuntu@124.221.245.237（本地 18020 → 生产 8020）
//      Chrome headless 开 9333 调试端口
// 用法：node tools/verify_prod_ui.mjs
const BASE = "http://127.0.0.1:18020";
const CDP = "http://127.0.0.1:9333";
const USER = "admin";
const PASS = "Iflytek@2026#";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let passed = 0;
function ok(cond, label, detail = "") {
  if (!cond) { console.error("  [FAIL]", label, detail); throw new Error(label); }
  passed++; console.log("  [PASS]", label);
}
async function rpc(ws, method, params = {}) {
  const id = (rpc.n = (rpc.n || 0) + 1);
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error("timeout " + method)), 20000);
    const onMsg = (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id === id) {
        clearTimeout(t); ws.removeEventListener("message", onMsg);
        m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
      }
    };
    ws.addEventListener("message", onMsg);
  });
}
async function evalJs(ws, expr) {
  const r = await rpc(ws, "Runtime.evaluate", { expression: expr, returnByValue: true });
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
  return r.result.value;
}

const login = await fetch(BASE + "/api/admin/login", {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ username: USER, password: PASS }),
}).then((r) => r.json());
if (!login.access_token) throw new Error("生产登录失败：" + JSON.stringify(login));
const H = { Authorization: "Bearer " + login.access_token };

console.log("\n[1] 生产接口数据");
const list = await fetch(BASE + "/api/admin/providers?service=ocr", { headers: H }).then((r) => r.json());
for (const n of ["移动云152OCR", "移动云中转UOCR"]) {
  const p = list.find((x) => x.name === n);
  ok(!!p, `${n} 存在`);
  ok(p.settings.extra_fields && typeof p.settings.extra_fields === "object"
     && p.settings.extra_fields.trackId === "gateway",
     `${n} 的 extra_fields 是 JSON 对象`, JSON.stringify(p.settings.extra_fields));
}
const cfg = await fetch(BASE + "/api/admin/config").then((r) => r.json());
const gen = cfg.services.find((s) => s.key === "ocr").provider_types.find((t) => t.type === "generic");
ok(gen.setting_fields.some((f) => f.name === "extra_fields" && f.json === true),
  "生产注册表已下发 json 字段标记");

console.log("\n[2] 生产页面渲染（只读）");
const tgt = await fetch(`${CDP}/json/new?${encodeURIComponent(BASE + "/admin/")}`, { method: "PUT" })
  .then((r) => r.json());
const ws = new WebSocket(tgt.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r));
await rpc(ws, "Page.enable"); await rpc(ws, "Runtime.enable");
await sleep(800);
await rpc(ws, "Runtime.evaluate", {
  expression: `localStorage.setItem('sg_token', ${JSON.stringify(login.access_token)})`,
});
await rpc(ws, "Page.reload", { ignoreCache: true });
await sleep(2500);
await evalJs(ws, `location.hash = '#/providers'`);
await sleep(1200);
await evalJs(ws, `document.querySelector('[data-svc="ocr"]')?.click()`);
await sleep(1000);

const pick = `(name) => [...document.querySelectorAll('.provider-card')]
  .find(x => x.querySelector('.pname')?.textContent.trim() === name)`;
for (const n of ["移动云152OCR", "移动云中转UOCR"]) {
  const text = await evalJs(ws, `(${pick})(${JSON.stringify(n)})?.textContent || ''`);
  ok(text.includes('"trackId"') && text.includes('"category"'), `${n} 卡片显示实际附加字段`);
  ok(!text.includes("[object Object]"), `${n} 卡片不再出现 [object Object]`);
}

const N = "移动云152OCR";
ok(await evalJs(ws, `!!(${pick})(${JSON.stringify(N)})?.querySelector('[data-dup]')`),
  "生产页面已有「复制」按钮");
ok(await evalJs(ws, `!!(${pick})(${JSON.stringify(N)})?.querySelector('[data-copy-setting]')`),
  "生产页面已有附加字段「复制」按钮");

await evalJs(ws, `(${pick})(${JSON.stringify(N)}).querySelector('[data-edit]').click()`);
await sleep(400);
const ta = await evalJs(ws,
  `document.querySelector('#modal-root textarea[name="set_extra_fields"]')?.value ?? null`);
ok(!!ta && ta.includes('"trackId": "gateway"') && !ta.includes("[object Object]"),
  "生产编辑页文本框显示真实 JSON", String(ta));
await evalJs(ws, `document.querySelector('#modal-root #cancel').click()`);
await sleep(300);

const shot = await rpc(ws, "Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
const fs = await import("node:fs");
fs.writeFileSync("shots/providers-prod-20261113.png", Buffer.from(shot.data, "base64"));
console.log("  [SHOT] shots/providers-prod-20261113.png");

ws.close();
await fetch(`${CDP}/json/close/${tgt.id}`);
console.log(`\n生产只读验收通过：${passed} 项（未创建/修改任何生产配置）`);
