// 后台页面截图：日志页（默认/高级筛选/设置弹窗）或任意路由
// 用法：node tools/shot_logs.mjs <baseUrl> <outPrefix> <user> <pass> [route]
const [base = "http://127.0.0.1:8000", prefix = "shots/logs-new", user = "admin", pass = "", route = "logs"] = process.argv.slice(2);
const CDP = "http://127.0.0.1:9333";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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

const login = await fetch(base + "/api/admin/login", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ username: user, password: pass }),
}).then((r) => r.json());
if (!login.access_token) throw new Error("登录失败: " + JSON.stringify(login));

const tgt = await fetch(`${CDP}/json/new?${encodeURIComponent(base + "/admin/")}`, { method: "PUT" })
  .then((r) => r.json());
const ws = new WebSocket(tgt.webSocketDebuggerUrl);
await new Promise((r) => ws.addEventListener("open", r));

await rpc(ws, "Page.enable");
await rpc(ws, "Runtime.enable");
await sleep(1200);
// 注入登录态后重新加载
await rpc(ws, "Runtime.evaluate", { expression: `localStorage.setItem('sg_token', ${JSON.stringify(login.access_token)})` });
// 同路径带 hash 的 navigate 不会重载文档，必须显式 reload 让 App 重新读 token
await rpc(ws, "Page.reload", { ignoreCache: true });
await sleep(2500);
await rpc(ws, "Runtime.evaluate", { expression: `location.hash = '#/${route}'` });
await sleep(1800);

async function shot(name) {
  const { data } = await rpc(ws, "Page.captureScreenshot", { format: "png", captureBeyondViewport: true });
  const fs = await import("node:fs");
  const file = `${prefix}-${name}.png`;
  fs.writeFileSync(file, Buffer.from(data, "base64"));
  console.log("saved", file);
}

await shot("default");

if (route === "logs") {
  // 展开更多筛选
  await rpc(ws, "Runtime.evaluate", { expression: `document.querySelector('#f-more')?.click()` });
  await sleep(500);
  await shot("adv");

  // 打开设置弹窗
  await rpc(ws, "Runtime.evaluate", { expression: `document.querySelector('#f-settings')?.click()` });
  await sleep(700);
  await shot("settings");
}

ws.close();
await fetch(`${CDP}/json/close/${tgt.id}`);
