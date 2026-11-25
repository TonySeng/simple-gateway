/**
 * 前端下载链路验证（fetch → blob）
 *
 * 目的：修复 "body stream already read" 后，用真实 HTTP 响应证明
 *   1) 旧逻辑（api 在 raw 模式下仍先 res.text()）必定抛错；
 *   2) 新逻辑（raw && res.ok 提前 return res）能拿到 blob 与正确文件名。
 *
 * 关键点：不从测试里重写实现，而是从 app/static/app.js 抽取 api() 与
 * downloadFile() 的原文，用最小 DOM stub 执行，避免「测试与实现各写一份」。
 *
 * 用法：node tests/frontend_download_check.mjs [base_url] [username] [password]
 *   默认 http://127.0.0.1:8020  admin  SG-Verify-2026!
 */
import fs from "node:fs";
import path from "node:path";

const BASE = process.argv[2] || "http://127.0.0.1:8020";
const USER = process.argv[3] || "admin";
const PASS = process.argv[4] || "SG-Verify-2026!";

const APP_PATH = path.resolve(process.cwd(), "app/static/app.js");
const SRC = fs.readFileSync(APP_PATH, "utf8");

let passed = 0, failed = 0;
function expect(cond, label, extra) {
  if (cond) { passed++; console.log(`  [PASS] ${label}`); }
  else { failed++; console.log(`  [FAIL] ${label}${extra ? " :: " + extra : ""}`); }
}

/** 按大括号配平抽取一个顶层 async function 的源码 */
function extractFn(name) {
  const start = SRC.indexOf("async function " + name + "(");
  if (start < 0) throw new Error("未在 app.js 中找到 async function " + name);
  let i = SRC.indexOf("{", start), depth = 0;
  for (; i < SRC.length; i++) {
    if (SRC[i] === "{") depth++;
    else if (SRC[i] === "}") { depth--; if (depth === 0) { i++; break; } }
  }
  return SRC.slice(start, i);
}

const API_SRC = extractFn("api");
const DL_SRC = extractFn("downloadFile");

const FAST_PATH = /if \(raw && res\.ok\) return res;/;

expect(FAST_PATH.test(API_SRC), "app.js 中存在 raw 提前返回分支（修复已落盘）");
expect(DL_SRC.includes("res.blob()"), "downloadFile 确实使用 res.blob() 读取 body");

/**
 * 用真实源码构造 downloadFile 环境。
 * @param {string} token 管理端 JWT
 * @param {boolean} legacy true = 抹掉提前返回，还原修复前行为
 */
function buildEnv(token, legacy) {
  let apiCode = API_SRC;
  if (legacy) {
    apiCode = apiCode.replace(FAST_PATH, "/* legacy: no early return */");
  }
  const saved = { blobs: [], names: [], hrefs: [] };

  // 最小 DOM stub：只满足 downloadFile 用到的 createElement / appendChild / click / remove
  const doc = {
    createElement() {
      return {
        href: "",
        download: "",
        click() {},
        remove() {},
      };
    },
    body: { appendChild(node) { saved.names.push(node.download); saved.hrefs.push(node.href); } },
  };
  // Node 无 createObjectURL，桩掉并记录 blob 以便断言大小
  const urlStub = {
    createObjectURL(blob) { saved.blobs.push(blob); return "blob:stub/" + saved.blobs.length; },
    revokeObjectURL() {},
  };
  const App = { token };

  const factory = new Function(
    "App", "document", "URL", "logout", "toast",
    `const $ = () => null;\n${apiCode}\n${DL_SRC}\nreturn { api, downloadFile };`
  );
  const mod = factory(App, doc, urlStub, () => {}, () => {});
  return { mod, saved };
}

async function login() {
  const res = await fetch(BASE + "/api/admin/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: USER, password: PASS }),
  });
  if (!res.ok) throw new Error("登录失败 HTTP " + res.status + "（口令是否已改？）");
  const d = await res.json();
  if (!d.access_token) throw new Error("登录响应无 access_token");
  return d.access_token;
}

const CASES = [
  { label: "导出全部 JSON", url: "/api/admin/safety/libraries/export?fmt=json", fallback: "safety-libraries.json" },
  { label: "JSON 模板", url: "/api/admin/safety/libraries/template?fmt=json", fallback: "tpl.json" },
  { label: "TXT 模板", url: "/api/admin/safety/libraries/template?fmt=txt", fallback: "tpl.txt" },
];

const token = process.env.SG_TOKEN ? process.env.SG_TOKEN.trim() : await login();
console.log(`\n目标 ${BASE}，token 长度 ${token.length}${process.env.SG_TOKEN ? "（来自 SG_TOKEN）" : "（已登录）"}`);

console.log("\n== 修复前行为（期望复现 body stream already read）==");
for (const c of CASES) {
  const { mod } = buildEnv(token, true);
  let err = null;
  try {
    await mod.downloadFile(BASE + c.url, c.fallback);
  } catch (e) {
    err = e;
  }
  const msg = err ? String(err.message || err) : "";
  // 浏览器措辞：Failed to execute 'blob' on 'Response': body stream already read
  // Node 措辞：Body is unusable: Body has already been read
  // 两者是同一缺陷（响应体被消费两次），断言需同时覆盖。
  expect(
    err && /already been read|body stream already read|unusable/i.test(msg),
    `旧逻辑「${c.label}」抛出「响应体已被读取」类错误`,
    err ? msg : "未抛错，说明该端点没走到 text()（与预期不符）"
  );
}

console.log("\n== 修复后行为（期望成功拿到 blob 与文件名）==");
for (const c of CASES) {
  const { mod, saved } = buildEnv(token, false);
  let err = null;
  try {
    await mod.downloadFile(BASE + c.url, c.fallback);
  } catch (e) {
    err = e;
  }
  expect(!err, `新逻辑「${c.label}」不抛错`, err ? String(err.message || err) : "");
  if (err) continue;
  const blob = saved.blobs[0];
  expect(!!blob, `新逻辑「${c.label}」产生 blob`);
  expect(blob && blob.size > 0, `新逻辑「${c.label}」blob 非空（${blob ? blob.size : 0} 字节）`);
  expect(saved.hrefs[0] && saved.hrefs[0].startsWith("blob:stub/"), `新逻辑「${c.label}」生成 object URL`);
  const name = saved.names[0] || "";
  expect(!!name && name !== "download", `新逻辑「${c.label}」文件名非空：${name}`);
}

console.log("\n== 单库导出（带 ids，TXT）==");
const libs = await (await fetch(BASE + "/api/admin/safety/libraries", {
  headers: { Authorization: "Bearer " + token },
})).json();
const one = libs[0];
{
  const { mod, saved } = buildEnv(token, false);
  let err = null;
  try {
    await mod.downloadFile(
      BASE + "/api/admin/safety/libraries/export?fmt=txt&ids=" + encodeURIComponent(one.id),
      "lib.txt"
    );
  } catch (e) { err = e; }
  expect(!err, "单库 TXT 导出不抛错", err ? String(err.message || err) : "");
  if (!err) {
    expect(saved.blobs[0] && saved.blobs[0].size > 0, `单库 TXT blob 非空（${saved.blobs[0] ? saved.blobs[0].size : 0} 字节）`);
    expect(/\.txt$/.test(saved.names[0] || ""), `单库 TXT 文件名为 .txt：${saved.names[0]}`);
  }
}

console.log("\n== 错误响应仍能抛出服务端 detail（未被 raw 提前返回吞掉）==");
{
  const { mod } = buildEnv(token, false);
  let err = null;
  try {
    // ids 为逗号分隔字符串（见 export_libraries 签名）；两套词库 + fmt=txt 应被 400 拒绝
    await mod.downloadFile(
      BASE + "/api/admin/safety/libraries/export?fmt=txt&ids=" +
        encodeURIComponent(libs[0].id + "," + libs[1].id),
      "bad.txt"
    );
  } catch (e) { err = e; }
  expect(!!err, "多库 TXT 请求被抓为错误");
  expect(err && !/already been read|unusable/i.test(String(err.message)), "错误是业务 detail 而非 stream 冲突", err ? String(err.message) : "");
  expect(err && /TXT|单库|仅支持|400/i.test(String(err.message)), `错误信息含服务端 detail：${err ? err.message : ""}`);
}

console.log("\n== 补丁不影响普通 JSON 请求（raw 分支不能误吞响应）==");
{
  const { mod } = buildEnv(token, false);

  const list = await mod.api(BASE + "/api/admin/safety/libraries");
  expect(Array.isArray(list) && list.length > 0, `列表 GET 仍返回数组（${Array.isArray(list) ? list.length : "非数组"} 套）`);

  const imp = await mod.api(BASE + "/api/admin/safety/libraries/import", "POST", {
    mode: "copy",
    libraries: [{ name: "前端验证临时库", terms: "验证词条甲\n验证词条乙" }],
  });
  expect(imp && imp.created === 1, `导入 POST 正常返回结果（created=${imp && imp.created}）`);

  const after = await mod.api(BASE + "/api/admin/safety/libraries");
  const tmp = (after || []).find((x) => x.name === "前端验证临时库");
  expect(!!tmp, "导入结果可在列表中查到");
  if (tmp) {
    expect(tmp.term_count === 2, `词条数正确（${tmp.term_count}）`);
    const del = await mod.api(BASE + "/api/admin/safety/libraries/" + tmp.id, "DELETE");
    expect(del === null, "DELETE 204 返回 null（未误判为错误）");
  }

  const afterDel = await mod.api(BASE + "/api/admin/safety/libraries");
  expect(!(afterDel || []).some((x) => x.name === "前端验证临时库"), "临时库已清理");

  let err2 = null;
  try {
    await mod.api(BASE + "/api/admin/safety/libraries/export?fmt=xml", "GET", null, true);
  } catch (e) { err2 = e; }
  expect(err2 && /json|txt/i.test(err2.message), `raw 模式下错误响应仍抛服务端 detail：${err2 ? err2.message : "未抛错"}`);
}

console.log(`\n通过 ${passed} 项，失败 ${failed} 项`);
if (failed > 0) process.exitCode = 1;