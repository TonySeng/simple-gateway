/* Simple Gateway 管理后台 —— 原生 JS 单页应用（无外部依赖，可离线/内网运行） */
(function () {
  "use strict";

  /* ===================== 状态 ===================== */
  const App = {
    token: localStorage.getItem("sg_token") || null,
    config: null,          // /api/admin/config
    services: [],          // 服务元信息
    route: "dashboard",
    providers: [],
    activeService: null,
    keys: [],
    logs: { page: 1, size: 20, total: 0, filters: {}, adv: false },
    liveTimer: null,
    liveBusy: false,
    days: 7,
    statService: "",
    timer: null,
    safetyTab: "policies",
    safetyLibraries: [],
    safetyPolicies: [],
    safetyRecords: { page: 1, size: 30, total: 0, filters: {} },
  };

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  /* ===================== 工具 ===================== */
  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }
  function num(v, digits = 0) {
    if (v == null || v === "") return "0";
    return Number(v).toLocaleString("zh-CN", { maximumFractionDigits: digits });
  }
  /* 复杂设置值（JSON 对象/数组）必须完整展示成可读文本。
     直接 String(obj) 会得到 "[object Object]"，旧版后台就是这么把附加字段
     显示坏、并在保存时把这个字符串写回数据库的。 */
  function settingText(v) {
    if (v == null) return "";
    if (typeof v === "object") {
      try { return JSON.stringify(v, null, 2); } catch (e) { return String(v); }
    }
    return String(v);
  }
  function money(v) {
    const n = Number(v || 0);
    return n === 0 ? "¥0" : "¥" + n.toFixed(n < 0.01 ? 4 : 2);
  }
  function fmtDate(s) {
    if (!s) return "-";
    const d = new Date(s.endsWith && s.endsWith("Z") ? s : s + "Z");
    if (isNaN(d)) return s;
    return d.toLocaleString("zh-CN", { hour12: false });
  }
  function svcLabel(key) {
    const s = App.services.find((x) => x.key === key);
    return s ? s.label : key;
  }
  function toast(msg, type) {
    const el = document.createElement("div");
    el.className = "toast " + (type || "");
    el.textContent = msg;
    $("#toast-root").appendChild(el);
    setTimeout(() => el.remove(), 3400);
  }

  /* ===================== API ===================== */
  async function api(path, method, body, raw, reqOpts) {
    reqOpts = reqOpts || {};
    const headers = {};
    if (App.token) headers["Authorization"] = "Bearer " + App.token;
    const opts = { method: method || "GET", headers };
    if (body instanceof FormData) {
      opts.body = body;
    } else if (body !== undefined && body !== null) {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    if (res.status === 401) {
      if (!reqOpts.noAutoLogout) {
        logout();
        throw new Error("登录已过期，请重新登录");
      }
      // 不自动登出：交给下方统一错误处理，保留服务端 detail（如改密时当前密码错误）
    }
    if (res.status === 204) return null;
    // raw 模式要把「未读过的 Response」交给调用方（下载场景紧接着调 res.blob()）。
    // 这里必须先于 res.text() 返回，否则 body 被消费，blob() 会抛
    // "Failed to execute 'blob' on 'Response': body stream already read"。
    if (raw && res.ok) return res;
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = text; }
    if (!res.ok) {
      let msg = (data && data.detail) || "HTTP " + res.status;
      if (Array.isArray(msg)) msg = msg.map((m) => m.msg || JSON.stringify(m)).join("; ");
      throw new Error(msg);
    }
    return raw ? res : data;
  }

  /* 带登录态下载文件：用 fetch 拿到 blob 再触发保存，避免 <a href> 丢 Authorization 头 */
  async function downloadFile(url, fallbackName) {
    const res = await api(url, "GET", null, true);
    const blob = await res.blob();
    const cd = res.headers.get("Content-Disposition") || "";
    let name = fallbackName || "download";
    const star = /filename\*=UTF-8''([^;]+)/i.exec(cd);
    const plain = /filename="([^"]+)"/i.exec(cd);
    if (star) { try { name = decodeURIComponent(star[1]); } catch (e) { /* 保留默认名 */ } }
    else if (plain) name = plain[1];
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }

  /* ===================== 弹窗 ===================== */
  function modal(html, wide) {
    $("#modal-root").innerHTML =
      '<div class="overlay"><div class="modal ' + (wide ? "wide" : "") + '">' + html + "</div></div>";
    const overlay = $("#modal-root .overlay");
    overlay.addEventListener("click", (e) => { if (e.target === overlay) closeModal(); });
  }
  function closeModal() { $("#modal-root").innerHTML = ""; }

  /* ===================== 登录 ===================== */
  function logout() {
    App.token = null;
    localStorage.removeItem("sg_token");
    $("#main-view").classList.add("hidden");
    $("#login-view").classList.remove("hidden");
  }

  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    $("#login-error").textContent = "";
    try {
      const r = await api("/api/admin/login", "POST", {
        username: f.get("username"), password: f.get("password"),
      });
      App.token = r.access_token;
      localStorage.setItem("sg_token", App.token);
      if (r.must_change_password) { openChangePassword(true); return; }
      await boot();
    } catch (err) {
      $("#login-error").textContent = err.message;
    }
  });
  $("#logout-btn").addEventListener("click", logout);
  const _cpb = $("#change-pwd-btn");
  if (_cpb) _cpb.addEventListener("click", () => openChangePassword(false));

  /* ===================== 密码强度与强制改密 ===================== */
  function checkPw(pw, username) {
    const base = [
      pw.length >= 12,
      /[a-z]/.test(pw),
      /[A-Z]/.test(pw),
      /[0-9]/.test(pw),
      /[!@#$%^&*()_+\-=\[\]{}|;:,.<>?/`~"' ]/.test(pw),
    ];
    const passed = base.filter(Boolean).length;
    const reasons = [];
    if (!base[0]) reasons.push("至少 12 位");
    if (!base[1]) reasons.push("含小写字母");
    if (!base[2]) reasons.push("含大写字母");
    if (!base[3]) reasons.push("含数字");
    if (!base[4]) reasons.push("含特殊符号");
    if (username && pw.toLowerCase() === username.toLowerCase()) reasons.push("不能与用户名相同");
    const ok = base.every(Boolean) && !(username && pw.toLowerCase() === username.toLowerCase());
    return { ok, reasons, score: passed };
  }

  function openChangePassword(forced) {
    const meEl = $("#me-name");
    const uname = (meEl && meEl.textContent && meEl.textContent !== "-")
      ? meEl.textContent
      : ((App.config && App.config.admin_username) || "admin");
    const html = `
      <div class="modal-head">
        <h3>${forced ? "安全提示：必须修改管理员密码" : "修改管理员密码"}</h3>
        ${forced ? "" : '<button class="link" id="x">✕</button>'}
      </div>
      <div class="modal-body">
        ${forced ? '<div class="hint-box" style="border-color:rgba(251,191,36,.5);color:var(--warn)">当前密码为默认或弱口令，安全策略要求首次登录后必须设置为强密码，方可继续使用后台。</div>' : ""}
        <div class="field"><span>当前密码</span><input name="cur" type="password" autocomplete="current-password" /></div>
        <div class="field"><span>新密码</span><input name="np" type="password" autocomplete="new-password" /></div>
        <div class="field"><span>确认新密码</span><input name="np2" type="password" autocomplete="new-password" /></div>
        <div class="pw-meter">
          <div class="pw-bars"><i></i><i></i><i></i><i></i><i></i></div>
          <div class="pw-reqs" id="pw-reqs"></div>
        </div>
        <div id="pw-error" class="login-error"></div>
      </div>
      <div class="modal-foot">
        ${forced ? "" : '<button class="btn-ghost" id="cancel">取消</button>'}
        <button class="btn-primary" id="save-pw">保存新密码</button>
      </div>`;
    modal(html);
    const root = $("#modal-root");
    if (!forced) {
      root.querySelector("#x").onclick = closeModal;
      root.querySelector("#cancel").onclick = closeModal;
    } else {
      const ov = root.querySelector(".overlay");
      ov.onclick = (e) => { if (e.target === ov) { /* 强制改密不可关闭 */ } };
    }
    const npEl = root.querySelector('input[name="np"]');
    const reqsEl = root.querySelector("#pw-reqs");
    const bars = root.querySelectorAll(".pw-bars i");
    function refresh() {
      const v = npEl.value;
      const r = checkPw(v, uname);
      bars.forEach((b, i) => { b.className = i < r.score ? "on s" + r.score : ""; });
      if (!v) { reqsEl.innerHTML = ""; return; }
      reqsEl.innerHTML = r.reasons.length
        ? '<span class="muted small">还缺少：' + esc(r.reasons.join("、")) + "</span>"
        : '<span class="ok small">✓ 密码强度达标</span>';
    }
    npEl.oninput = refresh;
    root.querySelector("#save-pw").onclick = async () => {
      const cur = root.querySelector('input[name="cur"]').value;
      const np = npEl.value;
      const np2 = root.querySelector('input[name="np2"]').value;
      const err = root.querySelector("#pw-error");
      err.textContent = "";
      if (!cur) { err.textContent = "请输入当前密码"; return; }
      if (np !== np2) { err.textContent = "两次输入的新密码不一致"; return; }
      const chk = checkPw(np, uname);
      if (!chk.ok) { err.textContent = "新密码强度不足：" + chk.reasons.join("、"); return; }
      try {
        const r = await api("/api/admin/change-password", "POST",
          { current_password: cur, new_password: np }, undefined, { noAutoLogout: true });
        App.token = r.access_token;
        localStorage.setItem("sg_token", App.token);
        closeModal();
        toast("密码已更新");
        if (forced) { await boot(); } else { route(); }
      } catch (e) { err.textContent = e.message; }
    };
  }

  /* ===================== 启动与路由 ===================== */
  async function boot() {
    App.config = await api("/api/admin/config");
    App.services = App.config.services || [];
    $("#brand-ver").textContent = "v" + (App.config.version || "");

    if (App.config.auth_enabled && !App.token) {
      $("#login-view").classList.remove("hidden");
      const u = $("#login-form input[name=username]");
      if (u && !u.value) u.value = App.config.admin_username || "admin";
      return;
    }
    try {
      const me = await api("/api/admin/me");
      $("#me-name").textContent = me.username;
      if (me.must_change_password) { openChangePassword(true); }
    } catch (e) { return; }

    $("#login-view").classList.add("hidden");
    $("#main-view").classList.remove("hidden");
    if (!App.activeService) App.activeService = App.services[0] && App.services[0].key;
    route();
  }

  function route() {
    const hash = (location.hash || "#/dashboard").replace(/^#\/?/, "");
    App.route = hash.split("?")[0] || "dashboard";
    stopLive();  // 离开实时页必须停掉轮询，否则后台一直在打接口
    $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === App.route));
    const views = {
      dashboard: renderDashboard,
      providers: renderProviders,
      keys: renderKeys,
      live: renderLive,
      safety: renderSafety,
      logs: renderLogs,
      docs: renderDocs,
    };
    // 同步/异步视图统一按 Promise 处理，避免某页返回 undefined 时抛错
    const fn = views[App.route] || renderDashboard;
    Promise.resolve()
      .then(() => fn())
      .catch((e) => {
        $("#content").innerHTML = `<div class="card"><p class="muted">加载失败：${esc(e.message)}</p></div>`;
      });
  }
  window.addEventListener("hashchange", route);

  /* ===================== 仪表盘 ===================== */
  function chartSeries(points) {
    if (!points.length) return "<div class='empty'>暂无数据</div>";
    const w = 720, h = 180, pad = 26;
    const max = Math.max(1, ...points.map((p) => p.requests));
    const bw = (w - pad * 2) / points.length;
    let bars = "";
    points.forEach((p, i) => {
      const bh = Math.round(((h - 46) * p.requests) / max);
      const x = pad + i * bw + bw * 0.18;
      const y = h - 28 - bh;
      bars += `<rect class="bar" x="${x.toFixed(1)}" y="${y}" width="${(bw * 0.64).toFixed(1)}" height="${Math.max(bh, p.requests ? 2 : 0)}" rx="3"><title>${esc(p.date)} · ${p.requests} 次 · ${money(p.cost)}</title></rect>`;
      if (points.length <= 14 || i % Math.ceil(points.length / 10) === 0) {
        bars += `<text x="${(x + bw * 0.32).toFixed(1)}" y="${h - 10}" text-anchor="middle">${esc(p.date.slice(5))}</text>`;
      }
    });
    return `<svg class="chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${bars}</svg>`;
  }

  /* 失败原因分布：横向条形，点击跳到日志页并按该原因过滤 */
  function errDist(rows) {
    if (!rows.length) return `<div class="empty">窗口内没有失败的调用</div>`;
    const max = Math.max(...rows.map((r) => r.requests || 0), 1);
    return `<div style="display:flex;flex-direction:column;gap:8px">` + rows.map((r) => {
      const label = (ERROR_TYPES.find((t) => t[0] === r.name) || [null, r.name])[1];
      const pct = Math.round(((r.requests || 0) / max) * 100);
      return `<div class="err-row" data-type="${esc(r.name)}" style="cursor:pointer;display:grid;grid-template-columns:190px 1fr 64px;gap:12px;align-items:center">
        <span class="small" title="${esc(r.name)}">${esc(label)}</span>
        <div class="meter" style="margin:0"><i style="width:${pct}%"></i></div>
        <span class="small mono" style="text-align:right">${num(r.requests)}</span>
      </div>`;
    }).join("") + `</div>`;
  }

  async function renderDashboard() {
    const [ov, series, bd, logs] = await Promise.all([
      api(`/api/admin/stats/overview?days=${App.days}${App.statService ? "&service=" + App.statService : ""}`),
      api(`/api/admin/stats/series?days=${Math.max(App.days, 7)}${App.statService ? "&service=" + App.statService : ""}`),
      api(`/api/admin/stats/breakdown?days=${App.days}${App.statService ? "&service=" + App.statService : ""}`),
      api(`/api/admin/logs?page=1&page_size=8${App.statService ? "&service=" + App.statService : ""}`),
    ]);

    const svcCards = ov.services.map((s) => {
      const rate = s.requests ? Math.round((s.success / s.requests) * 100) : 0;
      return `<div class="card">
        <h3>${esc(s.label)} <span class="tag svc">${esc(s.service)}</span></h3>
        <div class="meta muted small" style="display:flex;gap:14px;flex-wrap:wrap">
          <span>请求 <b style="color:var(--text)">${num(s.requests)}</b></span>
          <span>成功 <b style="color:var(--ok)">${num(s.success)}</b></span>
          <span>失败 <b style="color:var(--err)">${num(s.error)}</b></span>
          ${s.tokens ? `<span>tokens <b style="color:var(--text)">${num(s.tokens)}</b></span>` : ""}
          ${s.chars ? `<span>字符 <b style="color:var(--text)">${num(s.chars)}</b></span>` : ""}
          <span>费用 <b style="color:var(--text)">${money(s.cost)}</b></span>
        </div>
        <div class="meter"><i style="width:${rate}%"></i></div>
        <div class="small muted" style="margin-top:6px">成功率 ${rate}%</div>
      </div>`;
    }).join("");

    const recent = (logs.items || []).map((l) => `<tr>
      <td class="small muted">${fmtDate(l.created_at)}</td>
      <td><span class="tag svc">${esc(l.service)}</span></td>
      <td>${esc(l.provider_name || "-")}</td>
      <td class="mono small">${esc(l.model || "-")}</td>
      <td>${l.status === "success" ? '<span class="tag ok">成功</span>' : '<span class="tag err">失败</span>'}</td>
      <td class="small">${num(l.latency_ms)} ms</td>
      <td class="small">${l.tokens_in + l.tokens_out > 0 ? num(l.tokens_in + l.tokens_out) + " tok" : num(l.char_count) + " 字符"}</td>
    </tr>`).join("") || `<tr><td colspan="7" class="muted small">暂无调用记录</td></tr>`;

    $("#content").innerHTML = `
      <div class="page-head">
        <div>
          <h2>仪表盘</h2>
          <p>统一查看所有中转服务的调用量、成功率、tokens 与费用</p>
        </div>
        <div class="head-actions">
          <select id="stat-service" style="width:150px">
            <option value="">全部服务</option>
            ${App.services.map((s) => `<option value="${s.key}" ${App.statService === s.key ? "selected" : ""}>${esc(s.label)}</option>`).join("")}
          </select>
          <select id="days" style="width:110px">
            ${[1, 7, 30, 90].map((d) => `<option value="${d}" ${App.days === d ? "selected" : ""}>近 ${d} 天</option>`).join("")}
          </select>
        </div>
      </div>

      <div class="kpis">
        <div class="kpi"><div class="label">总请求</div><div class="value">${num(ov.total_requests)}</div><div class="hint">成功 ${num(ov.success)} · 失败 ${num(ov.error)}</div></div>
        <div class="kpi"><div class="label">成功率</div><div class="value">${ov.success_rate}%</div><div class="hint">平均耗时 ${num(ov.avg_latency_ms)} ms</div></div>
        <div class="kpi"><div class="label">Tokens</div><div class="value">${num(ov.tokens)}</div><div class="hint">OCR 字符 ${num(ov.chars)}</div></div>
        <div class="kpi accent"><div class="label">累计费用</div><div class="value">${money(ov.cost)}</div><div class="hint">按模型单价估算</div></div>
        <div class="kpi"><div class="label">API Key</div><div class="value">${num(ov.active_keys)}<span class="muted" style="font-size:14px">/${num(ov.total_keys)}</span></div><div class="hint">启用 / 总数</div></div>
        <div class="kpi"><div class="label">厂商配置</div><div class="value">${num(ov.providers_enabled)}<span class="muted" style="font-size:14px">/${num(ov.providers_total)}</span></div><div class="hint">启用 / 总数</div></div>
        <div class="kpi"><div class="label">安全拦截</div><div class="value" style="color:var(--err)">${num((ov.safety_blocked_input || 0) + (ov.safety_blocked_output || 0) + (ov.safety_blocked_overflow || 0))}</div><div class="hint">输入 ${num(ov.safety_blocked_input)} · 输出 ${num(ov.safety_blocked_output)}</div></div>
      </div>

      <div class="card">
        <h3>请求趋势 <span class="sub">近 ${Math.max(App.days, 7)} 天</span></h3>
        ${chartSeries(series)}
      </div>

      <div class="grid-2" style="margin-top:16px">
        <div class="card">
          <h3>服务概览</h3>
          <div style="display:flex;flex-direction:column;gap:12px">${svcCards || '<div class="empty">暂无服务</div>'}</div>
        </div>
        <div class="card">
          <h3>按模型 / 厂商 <span class="sub">Top 8</span></h3>
          <div class="table-wrap" style="border:none">
            <table>
              <thead><tr><th>名称</th><th>请求</th><th>tokens</th><th>费用</th></tr></thead>
              <tbody>
                ${(bd.by_model || []).slice(0, 5).map((m) => `<tr><td class="mono small">${esc(m.name)}</td><td>${num(m.requests)}</td><td>${num(m.tokens)}</td><td>${money(m.cost)}</td></tr>`).join("")}
                ${(bd.by_provider || []).slice(0, 3).map((m) => `<tr><td>${esc(m.name)}</td><td>${num(m.requests)}</td><td>${num(m.tokens)}</td><td>${money(m.cost)}</td></tr>`).join("") || '<tr><td colspan="4" class="muted small">暂无数据</td></tr>'}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <div class="card" style="margin-top:16px">
        <h3>失败原因分布 <span class="sub">近 ${App.days} 天 · 点击原因可查看明细</span></h3>
        ${errDist(bd.by_error || [])}
      </div>

      <div class="card" style="margin-top:16px">
        <h3>最近调用 <span class="sub"><a href="#/logs">查看全部 →</a></span></h3>
        <div class="table-wrap">
          <table>
            <thead><tr><th>时间</th><th>服务</th><th>厂商</th><th>模型</th><th>状态</th><th>耗时</th><th>用量</th></tr></thead>
            <tbody>${recent}</tbody>
          </table>
        </div>
      </div>`;

    $("#days").addEventListener("change", (e) => { App.days = Number(e.target.value); route(); });
    $("#stat-service").addEventListener("change", (e) => { App.statService = e.target.value; route(); });
    $$(".err-row").forEach((el) => {
      el.addEventListener("click", () => {
        App.logs.filters.error_type = el.dataset.type;
        App.logs.filters.status = "error";
        App.logs.page = 1;
        location.hash = "#/logs";
      });
    });
  }

  /* ===================== 服务与提供商 ===================== */
  function typeSpec(serviceKey, type) {
    const svc = App.services.find((s) => s.key === serviceKey);
    if (!svc) return null;
    return (svc.provider_types || []).find((t) => t.type === type) || null;
  }

  function providerFormHtml(p, serviceKey, opts) {
    opts = opts || {};
    const svc = App.services.find((s) => s.key === serviceKey);
    const isEdit = !!(p && p.id);
    const types = (svc && svc.provider_types) || [];
    const currentType = p ? p.type : (types[0] && types[0].type);
    const spec = typeSpec(serviceKey, currentType) || { fields: [], setting_fields: [], hint: "" };
    const settings = (p && p.settings) || {};
    const masked = (p && p.masked_credentials) || {};

    const credFields = (spec.fields || []).map((f) => `
      <div class="field">
        <span>${esc(f.label)} ${f.required ? '<b class="req">*</b>' : ""}</span>
        <input name="cred_${esc(f.name)}" type="${f.secret ? "password" : "text"}"
               placeholder="${isEdit ? "留空则保留原值" + (masked[f.name] ? "（当前 " + esc(masked[f.name]) + "）" : "") : esc(f.placeholder || "")}"
               autocomplete="off" />
      </div>`).join("");

    const settingFields = (spec.setting_fields || []).map((f) => {
      const text = settingText(settings[f.name]);
      const corrupted = text.trim() === "[object Object]";
      // 注册表里标了 json 的字段用多行文本框，方便查看/复制完整内容
      const ctrl = f.json
        ? `<textarea name="set_${esc(f.name)}" rows="4" spellcheck="false" placeholder="${esc(f.placeholder || "")}">${esc(text)}</textarea>`
        : `<input name="set_${esc(f.name)}" value="${esc(text)}" placeholder="${esc(f.placeholder || "")}" />`;
      return `
      <div class="field">
        <span>${esc(f.label)}</span>
        ${ctrl}
        ${corrupted ? '<div class="hint" style="color:var(--err)">⚠ 该字段曾被旧版后台写坏（值为 [object Object]），请重新填写 JSON 后再保存，直接保存会被拦截。</div>' : ""}
      </div>`;
    }).join("");

    const known = new Set((spec.setting_fields || []).map((f) => f.name));
    const rest = {};
    Object.keys(settings).forEach((k) => { if (!known.has(k)) rest[k] = settings[k]; });

    return `
      <div class="modal-head"><h3>${isEdit ? "编辑配置" : "新建配置"} · ${esc(svc ? svc.label : serviceKey)}</h3><button class="link" id="x">✕</button></div>
      <div class="modal-body">
        ${opts.duplicated ? '<div class="hint-box" style="border-color:rgba(251,191,36,.5);color:var(--warn)">这是复制出来的副本：已保留原配置的凭证、接入设置与超时，名称追加了「（副本）」。副本默认<b>停用且不设为默认</b>，不会参与生产路由；改好并「配置即测」通过后，勾选「启用」再保存即可。LLM 配置的模型列表不随副本复制，需另行拉取。</div>' : ""}
        <div class="form-row">
          <div class="field"><span>配置名称</span><input name="name" value="${esc(p ? p.name : "")}" placeholder="如 腾讯云-主账号" /></div>
          <div class="field"><span>厂商类型</span>
            <select name="type" ${isEdit ? "" : ""}>
              ${types.map((t) => `<option value="${esc(t.type)}" ${t.type === currentType ? "selected" : ""}>${esc(t.label)}</option>`).join("")}
            </select>
          </div>
        </div>
        ${serviceKey === "llm" ? `<div class="field"><span>Base URL</span><input name="base_url" value="${esc(p ? p.base_url : "")}" placeholder="https://api.openai.com/v1 （填到 /v1 层级）" /></div>` : ""}
        <div id="type-hint" class="hint-box">${esc(spec.hint || spec.description || "")}</div>
        <div class="form-row">
          <div class="field"><span>降级权重（越大越优先）</span><input name="weight" type="number" value="${p ? p.weight : 100}" /></div>
          <div class="field"><span>优先级（越小越优先）</span><input name="priority" type="number" value="${p ? p.priority : 100}" /></div>
        </div>
        <div class="field"><span>上游超时（秒）</span>
          <input name="timeout_s" type="number" min="0" max="3600" step="1" value="${p && p.timeout_s ? p.timeout_s : ""}" placeholder="留空 = 全局默认${p && p.effective_timeout_s ? `（当前生效 ${p.effective_timeout_s}s）` : ""}" />
          <div class="hint">非流式按整段响应计；流式按「两个数据块之间的间隔」计。慢模型（长推理、长文补全）调到 300~600；想让故障更快降级就调小。0 或留空 = 用全局默认。</div>
        </div>
        <div style="display:flex;gap:18px">
          <label class="inline"><input type="checkbox" name="is_enabled" ${!p || p.is_enabled ? "checked" : ""} /> 启用</label>
          <label class="inline"><input type="checkbox" name="is_default" ${p && p.is_default ? "checked" : ""} /> 设为该服务默认</label>
        </div>
        <div id="cred-section">
          <div class="section-title">凭证（加密存储，不回显明文）</div>
          <div class="form-row" style="margin-top:8px">${credFields || '<div class="small muted">该类型无需凭证</div>'}</div>
        </div>
        <div id="setting-section">
          <div class="section-title">接入设置</div>
          <div class="form-row" style="margin-top:8px">${settingFields || '<div class="small muted">无额外接入设置</div>'}</div>
        </div>
        <div class="field"><span>高级设置（JSON，可选）</span>
          <textarea name="advanced" placeholder='{"headers":{"X-Org":"abc"}}'>${Object.keys(rest).length ? esc(JSON.stringify(rest, null, 2)) : ""}</textarea>
        </div>
        <div class="field"><span>备注</span><input name="comment" value="${esc(p ? p.comment || "" : "")}" /></div>
      </div>
      <div class="modal-foot">
        <button class="btn-ghost" id="cancel">取消</button>
        <button class="btn-primary" id="save">保存</button>
      </div>`;
  }

  function openProviderForm(p, serviceKey, opts) {
    opts = opts || {};
    const isEdit = !!(p && p.id);
    modal(providerFormHtml(p, serviceKey, opts));
    $("#modal-root #x").onclick = closeModal;
    $("#modal-root #cancel").onclick = closeModal;

    // 切换厂商类型时重建表单；保留用户已填的通用字段（名称/权重/开关等）
    const typeSel = $('#modal-root select[name="type"]');
    typeSel.onchange = () => {
      const root = $("#modal-root");
      const get = (n) => { const el = root.querySelector(`[name="${n}"]`); return el ? el.value : ""; };
      const chk = (n) => { const el = root.querySelector(`[name="${n}"]`); return !!(el && el.checked); };
      // 切换厂商类型时保留已填的接入设置（含附加字段），避免复制后改类型把内容清空
      const keptSettings = {};
      $$('#modal-root [name^="set_"]').forEach((el) => {
        if (el.value.trim()) keptSettings[el.name.slice(4)] = el.value;
      });
      const draft = {
        type: typeSel.value,
        name: get("name"),
        base_url: get("base_url"),
        weight: Number(get("weight") || 100),
        priority: Number(get("priority") || 100),
        timeout_s: Number(get("timeout_s") || 0),
        is_enabled: chk("is_enabled"),
        is_default: chk("is_default"),
        comment: get("comment"),
        settings: keptSettings,
        masked_credentials: {},
      };
      openProviderForm(draft, serviceKey, opts);
    };

    $("#modal-root #save").onclick = async () => {
      const root = $("#modal-root");
      const val = (n) => { const el = root.querySelector(`[name="${n}"]`); return el ? el.value : ""; };
      const checked = (n) => { const el = root.querySelector(`[name="${n}"]`); return !!(el && el.checked); };
      const spec = typeSpec(serviceKey, val("type")) || { fields: [], setting_fields: [] };

      const credentials = {};
      (spec.fields || []).forEach((f) => {
        const v = val("cred_" + f.name).trim();
        if (v) credentials[f.name] = v;
      });
      const settings = {};
      for (const f of (spec.setting_fields || [])) {
        const v = val("set_" + f.name).trim();
        if (!v) continue;                    // 空 = 不写入该设置项
        if (v === "[object Object]") {       // 防呆：绝不再把损坏值写回数据库
          toast(f.label + " 仍是 [object Object]，请填写实际 JSON 内容后再保存", "err");
          return;
        }
        if (f.json) {
          let parsed;
          try { parsed = JSON.parse(v); }
          catch (e) { toast(f.label + " 不是合法 JSON：" + e.message, "err"); return; }
          if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
            toast(f.label + " 需要是 JSON 对象，例如 {\"category\":\"\"}", "err");
            return;
          }
          settings[f.name] = parsed;         // 存回对象，而不是一整串 JSON 文本
        } else {
          settings[f.name] = v;
        }
      }
      const adv = val("advanced").trim();
      if (adv) {
        try { Object.assign(settings, JSON.parse(adv)); }
        catch (e) { toast("高级设置不是合法 JSON：" + e.message, "err"); return; }
      }

      const rawTimeout = val("timeout_s").trim();
      const payload = {
        name: val("name").trim(),
        type: val("type"),
        base_url: val("base_url").trim(),
        weight: Number(val("weight") || 100),
        priority: Number(val("priority") || 100),
        timeout_s: rawTimeout === "" ? 0 : Number(rawTimeout),
        is_enabled: checked("is_enabled"),
        is_default: checked("is_default"),
        settings,
        comment: val("comment").trim() || null,
      };
      if (!(payload.timeout_s >= 0) || payload.timeout_s > 3600) {
        toast("上游超时需为 0~3600 秒（0 表示用全局默认）", "err");
        return;
      }
      if (!payload.name) { toast("请填写配置名称", "err"); return; }
      if (Object.keys(credentials).length) payload.credentials = credentials;
      else if (!isEdit) payload.credentials = {};

      try {
        if (isEdit) await api("/api/admin/providers/" + p.id, "PUT", payload);
        else await api("/api/admin/providers", "POST", { ...payload, service: serviceKey });
        closeModal();
        toast(opts.duplicated ? "副本已保存（当前停用，测试通过后请勾选「启用」）" : "已保存");
        renderProviders();
      } catch (e) { toast(e.message, "err"); }
    };
  }

  function openTestModal(p) {
    const spec = typeSpec(p.service, p.type);
    const isOcr = p.service === "ocr";
    modal(`
      <div class="modal-head"><h3>配置即测 · ${esc(p.name)}</h3><button class="link" id="x">✕</button></div>
      <div class="modal-body">
        <div class="hint-box">使用已保存的真实凭证实际调用一次上游，验证地址、鉴权与网络连通性。</div>
        ${isOcr ? `
          <div class="field"><span>测试图片（可选，留空用内置测试图）</span><input name="file" type="file" accept="image/*" /></div>
          <div class="field"><span>engine（可选）</span><input name="engine" placeholder="general / accurate / idcard ..." /></div>`
        : `<div class="hint-box">将请求上游「模型列表」接口（OpenAI 风格），返回模型数量即视为连通。</div>`}
        <div id="test-result"></div>
      </div>
      <div class="modal-foot">
        <button class="btn-ghost" id="cancel">关闭</button>
        <button class="btn-primary" id="run">开始测试</button>
      </div>`);
    $("#modal-root #x").onclick = closeModal;
    $("#modal-root #cancel").onclick = closeModal;
    $("#modal-root #run").onclick = async () => {
      const root = $("#modal-root");
      const box = $("#test-result", root);
      box.innerHTML = '<div class="muted small">测试中…</div>';
      const fd = new FormData();
      let hasBody = false;
      const fileEl = root.querySelector('input[name="file"]');
      if (fileEl && fileEl.files[0]) { fd.append("file", fileEl.files[0]); hasBody = true; }
      const eng = root.querySelector('input[name="engine"]');
      if (eng && eng.value.trim()) { fd.append("params", JSON.stringify({ engine: eng.value.trim() })); hasBody = true; }
      try {
        const r = await api(`/api/admin/providers/${p.id}/test`, "POST", hasBody ? fd : undefined);
        if (r.ok) {
          let detail = "";
          if (r.detail && r.detail.text) detail = `<pre class="code" style="margin-top:8px">${esc(r.detail.text)}</pre>`;
          if (r.detail && r.detail.models) detail = `<div class="small muted" style="margin-top:8px">示例模型：${esc((r.detail.models || []).slice(0, 10).join(", "))}</div>`;
          box.innerHTML = `<div class="hint-box" style="border-color:rgba(52,211,153,.4);color:var(--ok)">
            ✓ 成功 · ${r.latency_ms} ms · ${esc(r.message)}</div>${detail}`;
        } else {
          box.innerHTML = `<div class="hint-box" style="border-color:rgba(248,113,113,.4);color:var(--err)">
            ✗ 失败 · ${r.latency_ms} ms<br/>${esc(r.message)}</div>`;
        }
      } catch (e) {
        box.innerHTML = `<div class="hint-box" style="color:var(--err)">${esc(e.message)}</div>`;
      }
    };
  }

  /* ---------- 模型管理（仅 LLM）---------- */
  async function openModelsModal(p) {
    const draw = async () => {
      const models = await api("/api/admin/models?provider_id=" + p.id);
      const rows = models.map((m) => `<tr>
        <td class="mono">${esc(m.name)}</td>
        <td class="mono small muted">${esc(m.provider_model)}</td>
        <td>${esc(m.input_price)}</td>
        <td>${esc(m.output_price)}</td>
        <td>${num(m.context_window)}</td>
        <td><button class="btn-danger btn-sm" data-del="${esc(m.id)}">删除</button></td>
      </tr>`).join("") || '<tr><td colspan="6" class="muted small">还没有模型，先拉取或手动添加</td></tr>';

      $("#models-body").innerHTML = `
        <div class="hint-box">对外暴露的模型名 → 上游真实模型 ID，并配置每 1K tokens 单价用于计费。</div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>模型名</th><th>上游 ID</th><th>输入价</th><th>输出价</th><th>上下文</th><th></th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
        <div class="section-title">新增模型</div>
        <div class="form-row">
          <div class="field"><span>模型名（对外）</span><input id="m-name" placeholder="gpt-4o" /></div>
          <div class="field"><span>上游模型 ID</span><input id="m-up" placeholder="留空同模型名" /></div>
        </div>
        <div class="form-row">
          <div class="field"><span>输入价 / 1K</span><input id="m-in" type="number" step="0.0001" value="0" /></div>
          <div class="field"><span>输出价 / 1K</span><input id="m-out" type="number" step="0.0001" value="0" /></div>
        </div>
        <div class="form-row">
          <div class="field"><span>上下文长度</span><input id="m-ctx" type="number" value="0" /></div>
          <div class="field"><span>&nbsp;</span><button class="btn-primary" id="m-add">添加模型</button></div>
        </div>
        <div id="fetched"></div>`;

      $$("#models-body [data-del]").forEach((b) => {
        b.onclick = async () => {
          if (!confirm("确定删除该模型？")) return;
          await api("/api/admin/models/" + b.dataset.del, "DELETE");
          toast("已删除");
          draw();
        };
      });
      $("#m-add").onclick = async () => {
        const name = $("#m-name").value.trim();
        if (!name) { toast("请填写模型名", "err"); return; }
        try {
          await api("/api/admin/models", "POST", {
            provider_id: p.id, name,
            provider_model: $("#m-up").value.trim() || name,
            input_price: Number($("#m-in").value || 0),
            output_price: Number($("#m-out").value || 0),
            context_window: Number($("#m-ctx").value || 0),
          });
          toast("已添加");
          draw();
        } catch (e) { toast(e.message, "err"); }
      };
    };

    modal(`
      <div class="modal-head"><h3>模型管理 · ${esc(p.name)}</h3><button class="link" id="x">✕</button></div>
      <div class="modal-body" id="models-body">加载中…</div>
      <div class="modal-foot">
        <button class="btn-ghost" id="fetch">从上游拉取模型</button>
        <button class="btn-ghost" id="cancel">关闭</button>
      </div>`, true);
    $("#modal-root #x").onclick = closeModal;
    $("#modal-root #cancel").onclick = closeModal;

    $("#modal-root #fetch").onclick = async () => {
      $("#modal-root #fetched").innerHTML = '<div class="small muted">正在拉取…</div>';
      try {
        const r = await api(`/api/admin/providers/${p.id}/fetch-models`, "POST");
        if (!r.ok) { $("#modal-root #fetched").innerHTML = `<div class="hint-box" style="color:var(--err)">${esc(r.message)}</div>`; return; }
        $("#modal-root #fetched").innerHTML = `<div class="section-title">上游返回 ${r.models.length} 个（点击添加）</div>
          <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:8px">
            ${r.models.slice(0, 80).map((m) => `<button class="btn-ghost btn-sm" data-add="${esc(m.id)}">${esc(m.id)}</button>`).join("")}
          </div>`;
        $$("#modal-root [data-add]").forEach((b) => {
          b.onclick = async () => {
            try {
              await api("/api/admin/models", "POST", {
                provider_id: p.id, name: b.dataset.add, provider_model: b.dataset.add,
              });
              toast("已添加 " + b.dataset.add);
              draw();
            } catch (e) { toast(e.message, "err"); }
          };
        });
      } catch (e) { $("#modal-root #fetched").innerHTML = `<div class="hint-box" style="color:var(--err)">${esc(e.message)}</div>`; }
    };

    await draw();
  }

  async function renderProviders() {
    const list = await api("/api/admin/providers" + (App.activeService ? "?service=" + App.activeService : ""));
    App.providers = list;
    const svc = App.services.find((s) => s.key === App.activeService) || {};

    const SENSITIVE_KEY = /auth|token|secret|password|api_?key|header/i;
    const cards = list.map((p) => {
      const missing = (p.missing_credentials || []).length;
      const masked = Object.entries(p.masked_credentials || {})
        .map(([k, v]) => `<span>${esc(k)}: ${esc(v)}</span>`).join("") || '<span class="muted">无凭证</span>';
      // 复杂设置（如 OCR 附加字段）直接在卡片上完整展示，不用点开编辑也能看到实际内容。
      // 含凭证语义的键一律跳过，避免在列表页泄漏令牌。
      const complex = Object.entries(p.settings || {}).filter(([k, v]) => {
        if (SENSITIVE_KEY.test(k)) return false;
        if (v && typeof v === "object") return true;
        const t = typeof v === "string" ? v.trim() : "";
        return t.startsWith("{") || t.startsWith("[");
      });
      const complexHtml = complex.length ? `<div style="margin-top:8px">
        ${complex.map(([k, v]) => `<div style="margin-top:6px">
          <div class="small muted" style="display:flex;justify-content:space-between;align-items:center;gap:8px">
            <span>${esc(k)}</span><button class="link small" data-copy-setting="${esc(p.id)}|${esc(k)}">复制</button>
          </div>
          <pre class="code small" style="margin:4px 0 0;white-space:pre-wrap;word-break:break-all;max-height:160px;overflow:auto">${esc(settingText(v))}</pre>
        </div>`).join("")}
      </div>` : "";
      return `<div class="provider-card ${p.is_enabled ? "" : "off"}">
        <div class="top">
          <div>
            <div class="pname">${esc(p.name)}</div>
            <div class="ptype">${esc(p.type_label)} · ${esc(p.type)}</div>
          </div>
          <div style="display:flex;gap:5px;flex-wrap:wrap;justify-content:flex-end">
            ${p.is_default ? '<span class="tag info">默认</span>' : ""}
            ${p.is_enabled ? '<span class="tag ok">启用</span>' : '<span class="tag">停用</span>'}
          </div>
        </div>
        <div class="meta">
          <span class="tag">权重 ${p.weight}</span>
          <span class="tag">优先级 ${p.priority}</span>
          <span class="tag" title="上游读超时；括号内为实际生效值">超时 ${p.timeout_s ? `${p.timeout_s}s` : `${p.effective_timeout_s}s（默认）`}</span>
          ${p.models_count ? `<span class="tag">${p.models_count} 个模型</span>` : ""}
          ${p.base_url ? `<span class="tag">${esc(p.base_url)}</span>` : ""}
        </div>
        ${missing ? `<div class="warn-line">⚠ 缺少凭证：${esc(p.missing_credentials.join("、"))}</div>` : ""}
        <div class="masked">${masked}</div>
        ${complexHtml}
        <div class="actions">
          <button class="btn-ghost btn-sm" data-test="${esc(p.id)}">测试</button>
          <button class="btn-ghost btn-sm" data-edit="${esc(p.id)}">编辑</button>
          <button class="btn-ghost btn-sm" data-dup="${esc(p.id)}">复制</button>
          ${p.service === "llm" ? `<button class="btn-ghost btn-sm" data-models="${esc(p.id)}">模型</button>` : ""}
          <button class="btn-ghost btn-sm" data-toggle="${esc(p.id)}">${p.is_enabled ? "停用" : "启用"}</button>
          <button class="btn-danger btn-sm" data-del="${esc(p.id)}">删除</button>
        </div>
      </div>`;
    }).join("");

    $("#content").innerHTML = `
      <div class="page-head">
        <div><h2>服务与提供商</h2><p>${esc(svc.description || "按服务维护上游接入配置，凭证加密存储")}</p></div>
        <div class="head-actions"><button class="btn-primary" id="new-provider">新建配置</button></div>
      </div>
      <div class="tabs">
        ${App.services.map((s) => `<button data-svc="${s.key}" class="${s.key === App.activeService ? "active" : ""}">${esc(s.label)}</button>`).join("")}
      </div>
      ${list.length ? `<div class="provider-grid">${cards}</div>` : `<div class="card"><div class="empty">
        <div class="big">还没有配置任何「${esc(svc.label || "")}」厂商</div>
        <div>点击右上角「新建配置」接入云端厂商或私有化服务</div></div></div>`}
      <div class="card" style="margin-top:16px">
        <h3>扩展新服务</h3>
        <p class="small muted" style="line-height:1.8;margin:0">
          该页面的表单、路由、鉴权与统计均由「服务注册表」驱动。新增一个中转服务只需在
          <span class="mono">app/services/&lt;key&gt;/service.py</span> 中注册
          <span class="mono">ServiceSpec</span>（含厂商字段元信息与对外路由），
          后台会自动出现对应标签页、动态凭证表单与用量统计，无需改动任何管理端代码。
        </p>
      </div>`;

    $$("#content [data-svc]").forEach((b) => {
      b.onclick = () => { App.activeService = b.dataset.svc; renderProviders(); };
    });
    $("#new-provider").onclick = () => openProviderForm(null, App.activeService);
    $$("#content [data-edit]").forEach((b) => {
      b.onclick = () => openProviderForm(App.providers.find((p) => p.id === b.dataset.edit), App.activeService);
    });
    $$("#content [data-dup]").forEach((b) => {
      b.onclick = async () => {
        const p = App.providers.find((x) => x.id === b.dataset.dup);
        if (!p) return;
        b.disabled = true;
        try {
          const created = await api("/api/admin/providers/" + p.id + "/duplicate", "POST");
          await renderProviders();                       // 先刷新列表，再打开副本编辑页
          openProviderForm(created, created.service, { duplicated: true });
          toast("已创建副本「" + created.name + "」，请修改后保存");
        } catch (e) {
          toast(e.message, "err");
          b.disabled = false;
        }
      };
    });
    $$("#content [data-copy-setting]").forEach((b) => {
      b.onclick = async () => {
        const [pid, key] = b.dataset.copySetting.split("|");
        const p = App.providers.find((x) => x.id === pid);
        if (!p) return;
        try {
          await navigator.clipboard.writeText(settingText((p.settings || {})[key]));
          toast("已复制 " + key);
        } catch (e) { toast("复制失败，请手动选中", "err"); }
      };
    });
    $$("#content [data-test]").forEach((b) => {
      b.onclick = () => openTestModal(App.providers.find((p) => p.id === b.dataset.test));
    });
    $$("#content [data-models]").forEach((b) => {
      b.onclick = () => openModelsModal(App.providers.find((p) => p.id === b.dataset.models));
    });
    $$("#content [data-toggle]").forEach((b) => {
      b.onclick = async () => {
        const p = App.providers.find((x) => x.id === b.dataset.toggle);
        await api("/api/admin/providers/" + p.id, "PUT", { is_enabled: !p.is_enabled });
        renderProviders();
      };
    });
    $$("#content [data-del]").forEach((b) => {
      b.onclick = async () => {
        const p = App.providers.find((x) => x.id === b.dataset.del);
        if (!confirm(`删除配置「${p.name}」？其下模型会一并删除。`)) return;
        await api("/api/admin/providers/" + p.id, "DELETE");
        toast("已删除");
        renderProviders();
      };
    });

    // 深链：#/providers?new=1 直接打开新建表单
    if (location.hash.indexOf("new=1") >= 0) {
      history.replaceState(null, "", "#/providers");
      openProviderForm(null, App.activeService);
    }
  }

  /* ===================== API Key ===================== */
  function keyScopeHtml(k, services) {
    const allowed = (k && k.allowed_services) || ["*"];
    return services.map((s) => {
      const on = allowed.includes("*") || allowed.includes(s.key);
      return `<label class="inline" style="margin-right:14px">
        <input type="checkbox" name="svc_${s.key}" ${on ? "checked" : ""} /> ${esc(s.label)}
      </label>`;
    }).join("");
  }

  function keyProviderHtml(k, services) {
    const map = (k && k.allowed_providers) || {};
    return services.map((s) => {
      const types = (s.provider_types || []).map((t) => t.type);
      const cur = map[s.key] || [];
      const all = cur.includes("*") || cur.length === 0;
      return `<div class="field">
        <span>${esc(s.label)} 可用厂商</span>
        <select name="prov_${s.key}">
          <option value="*" ${all ? "selected" : ""}>全部厂商</option>
          ${types.map((t) => `<option value="${esc(t)}" ${cur.includes(t) ? "selected" : ""}>仅 ${esc(t)}</option>`).join("")}
        </select>
      </div>`;
    }).join("");
  }

  function keyChannelHtml(k, services, providers) {
    const map = (k && k.channels) || {};
    const bySvc = {};
    (providers || []).forEach((p) => { (bySvc[p.service] = bySvc[p.service] || []).push(p); });
    return services.map((s) => {
      const list = bySvc[s.key] || [];
      const cur = map[s.key] || "";
      if (!list.length) {
        return `<div class="field"><span>${esc(s.label)} 渠道</span>
          <select name="chan_${s.key}" disabled><option value="">（该服务暂无启用厂商）</option></select></div>`;
      }
      return `<div class="field"><span>${esc(s.label)} 渠道</span>
        <select name="chan_${s.key}">
          <option value="">不指定（自动路由）</option>
          ${list.map((p) => `<option value="${esc(p.id)}" ${cur === p.id ? "selected" : ""}>${esc(p.name)} · ${esc(p.type)}</option>`).join("")}
        </select></div>`;
    }).join("");
  }

  function keySafetyHtml(k, policies) {
    const cur = k && k.safety_policy_id !== undefined && k.safety_policy_id !== null
      ? k.safety_policy_id
      : "__inherit__";
    const sel = cur === "" ? "__off__" : cur;
    return `<div class="field"><span>安全审查规则</span>
      <select name="safety_policy_id">
        <option value="__inherit__" ${sel === "__inherit__" ? "selected" : ""}>继承默认规则</option>
        <option value="__off__" ${sel === "__off__" ? "selected" : ""}>关闭审查（仅此 Key）</option>
        ${(policies || []).map((p) => `<option value="${esc(p.id)}" ${sel === p.id ? "selected" : ""}>${esc(p.name)}${p.is_default ? "（默认）" : ""}</option>`).join("")}
      </select>
      <div class="small muted">规则决定使用哪些词库、输入/输出方向与命中阈值；默认规则可在「安全审查」页配置。</div>
    </div>`;
  }

  async function openKeyForm(k) {
    const isEdit = !!k;
    let providers = [];
    let policies = [];
    try {
      const [p1, p2] = await Promise.all([
        api("/api/admin/providers?enabled=true"),
        api("/api/admin/safety/policies"),
      ]);
      providers = p1 || [];
      policies = p2 || [];
    } catch (e) { providers = providers || []; }
    modal(`
      <div class="modal-head"><h3>${isEdit ? "编辑 Key" : "新建 API Key"}</h3><button class="link" id="x">✕</button></div>
      <div class="modal-body">
        <div class="field"><span>名称</span><input name="name" value="${esc(k ? k.name : "")}" placeholder="如 业务系统A" /></div>
        <div class="section-title">可调用的服务</div>
        <div>${keyScopeHtml(k, App.services)}</div>
        <div class="section-title">按服务限定厂商</div>
        <div class="form-row">${keyProviderHtml(k, App.services)}</div>
        <div class="section-title">指定渠道（按服务，可选）</div>
        <div class="small muted" style="margin-bottom:6px">每个服务可单独钉定到不同的上游厂商；留空则按模型/厂商白名单自动路由。</div>
        <div class="form-row">${keyChannelHtml(k, App.services, providers)}</div>
        <div class="section-title">安全审查</div>
        ${keySafetyHtml(k, policies)}
        <div class="section-title">配额与限流（0 = 不限）</div>
        <div class="form-row">
          <div class="field"><span>请求次数上限</span><input name="quota_requests" type="number" value="${k ? k.quota_requests : 0}" /></div>
          <div class="field"><span>速率限制（次/分钟）</span><input name="rate_limit" type="number" value="${k ? k.rate_limit : 0}" /></div>
        </div>
        <div class="form-row">
          <div class="field"><span>并发限制（同时等待上游的请求数）</span><input name="max_concurrency" type="number" min="0" value="${k ? (k.max_concurrency ?? 0) : 0}" /></div>
          <div class="field"><span class="muted small">0 = 继承全局配置（默认不限）；达到上限的新请求将直接返回 429，不调用上游</span></div>
        </div>
        <div class="form-row">
          <div class="field"><span>tokens 上限</span><input name="quota_tokens" type="number" value="${k ? k.quota_tokens : 0}" /></div>
          <div class="field"><span>费用上限（元）</span><input name="quota_cost" type="number" step="0.01" value="${k ? k.quota_cost : 0}" /></div>
        </div>
        <div class="field"><span>过期时间（可选）</span><input name="expires_at" type="datetime-local" value="${k && k.expires_at ? esc(k.expires_at.slice(0, 16)) : ""}" /></div>
        <div class="field"><span>备注</span><input name="comment" value="${esc(k ? k.comment || "" : "")}" /></div>
      </div>
      <div class="modal-foot">
        <button class="btn-ghost" id="cancel">取消</button>
        <button class="btn-primary" id="save">${isEdit ? "保存" : "创建"}</button>
      </div>`);
    $("#modal-root #x").onclick = closeModal;
    $("#modal-root #cancel").onclick = closeModal;

    $("#modal-root #save").onclick = async () => {
      const root = $("#modal-root");
      const val = (n) => { const el = root.querySelector(`[name="${n}"]`); return el ? el.value : ""; };
      const services = App.services.filter((s) => root.querySelector(`[name="svc_${s.key}"]`).checked).map((s) => s.key);
      if (!services.length) { toast("至少勾选一个服务", "err"); return; }
      const allowed_providers = {};
      App.services.forEach((s) => {
        const v = root.querySelector(`[name="prov_${s.key}"]`).value;
        if (v !== "*") allowed_providers[s.key] = [v];
      });
      const channels = services.reduce((acc, sv) => {
        const el = root.querySelector(`[name="chan_${sv}"]`);
        const v = el ? el.value : "";
        if (v) acc[sv] = v;
        return acc;
      }, {});
      const safetySel = val("safety_policy_id");
      const payload = {
        name: val("name").trim() || "未命名 Key",
        allowed_services: services.length === App.services.length ? ["*"] : services,
        allowed_providers,
        channels,
        safety_policy_id: safetySel === "__inherit__" ? null : (safetySel === "__off__" ? "" : safetySel),
        quota_requests: Number(val("quota_requests") || 0),
        quota_tokens: Number(val("quota_tokens") || 0),
        quota_cost: Number(val("quota_cost") || 0),
        rate_limit: Number(val("rate_limit") || 0),
        max_concurrency: Math.max(0, Number(val("max_concurrency") || 0)),
        comment: val("comment").trim() || null,
      };
      const exp = val("expires_at");
      payload.expires_at = exp ? new Date(exp).toISOString() : null;

      try {
        if (isEdit) {
          await api("/api/admin/keys/" + k.id, "PUT", payload);
          closeModal(); toast("已保存"); renderKeys();
        } else {
          const r = await api("/api/admin/keys", "POST", payload);
          showNewKey(r);
          renderKeys();
        }
      } catch (e) { toast(e.message, "err"); }
    };
  }

  function showNewKey(k) {
    modal(`
      <div class="modal-head"><h3>Key 已创建</h3><button class="link" id="x">✕</button></div>
      <div class="modal-body">
        <div class="hint-box" style="border-color:rgba(251,191,36,.4);color:var(--warn)">
          明文仅显示这一次，请立即复制保存。服务端只保存 SHA-256 哈希。
        </div>
        <pre class="code" id="new-key">${esc(k.key)}</pre>
        <div class="small muted">名称：${esc(k.name)} · 服务：${esc((k.allowed_services || []).join(", "))}</div>
      </div>
      <div class="modal-foot">
        <button class="btn-ghost" id="copy">复制</button>
        <button class="btn-primary" id="done">我已保存</button>
      </div>`);
    $("#modal-root #x").onclick = closeModal;
    $("#modal-root #done").onclick = closeModal;
    $("#modal-root #copy").onclick = async () => {
      try { await navigator.clipboard.writeText(k.key); toast("已复制"); }
      catch (e) { toast("复制失败，请手动选择", "err"); }
    };
  }

  async function renderKeys() {
    const [list, policies] = await Promise.all([
      api("/api/admin/keys"),
      api("/api/admin/safety/policies").catch(() => []),
    ]);
    App.keys = list;
    App.safetyPolicies = policies || [];
    const policyMap = {};
    App.safetyPolicies.forEach((p) => { policyMap[p.id] = p.name; });
    const provMap = {};
    (App.providers || []).forEach((p) => { provMap[p.id] = p.name; });
    const rows = list.map((k) => {
      const quota = [
        k.quota_requests ? `请求 ${num(k.quota_requests)}` : "",
        k.quota_tokens ? `tokens ${num(k.quota_tokens)}` : "",
        k.quota_cost ? `费用 ${money(k.quota_cost)}` : "",
      ].filter(Boolean).join(" · ") || "不限";
      const usedPct = k.quota_requests ? Math.min(100, (k.used_requests / k.quota_requests) * 100) : 0;
      const channelTags = Object.entries(k.channels || {}).map(([sv, pid]) =>
        `<span class="tag svc" title="指定渠道 · ${esc(sv)}">★ ${esc(svcLabel(sv))} → ${esc(provMap[pid] || pid)}</span>`
      ).join(" ");
      const safetyId = k.safety_policy_id;
      const safetyTag = safetyId === ""
        ? '<span class="tag warn">审查关闭</span>'
        : (safetyId && policyMap[safetyId]
          ? `<span class="tag info">审查：${esc(policyMap[safetyId])}</span>`
          : '<span class="tag">审查：继承默认</span>');
      return `<tr>
        <td>${esc(k.name)}<div class="small muted mono">${esc(k.key_prefix)}…</div>${safetyTag}</td>
        <td>${(k.allowed_services || []).map((s) => `<span class="tag svc">${s === "*" ? "全部" : esc(svcLabel(s))}</span>`).join(" ")} ${channelTags}</td>
        <td class="small">${esc(quota)}${k.max_concurrency ? `<div class="muted">并发 ≤ ${num(k.max_concurrency)}</div>` : ""}</td>
        <td class="small">${num(k.used_requests)} 次<div class="muted">${num(k.used_tokens)} tok · ${money(k.used_cost)}</div>
          ${k.quota_requests ? `<div class="meter"><i style="width:${usedPct}%"></i></div>` : ""}</td>
        <td>${k.status === "active" ? '<span class="tag ok">启用</span>' : '<span class="tag err">停用</span>'}</td>
        <td class="small muted">${k.expires_at ? fmtDate(k.expires_at) : "永久"}</td>
        <td class="small muted">${k.last_used_at ? fmtDate(k.last_used_at) : "-"}</td>
        <td class="nowrap">
          <button class="btn-ghost btn-sm" data-edit="${esc(k.id)}">编辑</button>
          <button class="btn-ghost btn-sm" data-reset="${esc(k.id)}">重置</button>
          <button class="btn-ghost btn-sm" data-toggle="${esc(k.id)}">${k.status === "active" ? "停用" : "启用"}</button>
          <button class="btn-danger btn-sm" data-del="${esc(k.id)}">删除</button>
        </td>
      </tr>`;
    }).join("");

    $("#content").innerHTML = `
      <div class="page-head">
        <div><h2>API Key</h2><p>一个 Key 可同时授权多个服务，并分别限制各服务可用厂商</p></div>
        <div class="head-actions"><button class="btn-primary" id="new-key">新建 Key</button></div>
      </div>
      <div class="card">
        <div class="table-wrap">
          <table>
            <thead><tr><th>名称</th><th>服务范围</th><th>配额</th><th>用量</th><th>状态</th><th>到期</th><th>最近使用</th><th>操作</th></tr></thead>
            <tbody>${rows || '<tr><td colspan="8" class="muted small">还没有创建 Key</td></tr>'}</tbody>
          </table>
        </div>
      </div>`;

    $("#new-key").onclick = () => openKeyForm(null);
    $$("#content [data-edit]").forEach((b) => {
      b.onclick = () => openKeyForm(App.keys.find((k) => k.id === b.dataset.edit));
    });
    $$("#content [data-reset]").forEach((b) => {
      b.onclick = async () => {
        if (!confirm("重置后旧 Key 立即失效，确定继续？")) return;
        const r = await api(`/api/admin/keys/${b.dataset.reset}/reset`, "POST");
        showNewKey(r);
        renderKeys();
      };
    });
    $$("#content [data-toggle]").forEach((b) => {
      b.onclick = async () => {
        const k = App.keys.find((x) => x.id === b.dataset.toggle);
        await api("/api/admin/keys/" + k.id, "PUT", { status: k.status === "active" ? "disabled" : "active" });
        renderKeys();
      };
    });
    $$("#content [data-del]").forEach((b) => {
      b.onclick = async () => {
        if (!confirm("确定删除该 Key？")) return;
        await api("/api/admin/keys/" + b.dataset.del, "DELETE");
        toast("已删除");
        renderKeys();
      };
    });
  }

  /* 失败归因类型（与 app/services/llm/proxy.py 的 ERROR_* 常量对应） */
  const ERROR_TYPES = [
    ["upstream_status", "上游返回错误状态码"],
    ["upstream_timeout", "上游超时"],
    ["upstream_connect", "连接上游失败"],
    ["upstream_non_json", "上游返回非 JSON"],
    ["upstream_stream_error", "上游流内报错"],
    ["upstream_error", "上游厂商调用失败（OCR）"],
    ["stream_interrupted", "流传输中断"],
    ["empty_stream", "空流式响应"],
    ["model_not_found", "模型不存在/未启用"],
    ["channel_unavailable", "Key 渠道不可用"],
    ["provider_unavailable", "上游厂商已停用"],
    ["invalid_request", "请求参数不合法"],
    ["internal_error", "网关内部错误"],
  ];

  /* ===================== 实时监控 ===================== */
  const STAGE_CLASS = { routing: "", reviewing: "warn", connecting: "warn", streaming: "info", writing: "ok" };

  function stopLive() {
    if (App.liveTimer) {
      clearInterval(App.liveTimer);
      App.liveTimer = null;
    }
    App.liveBusy = false;
  }

  function fmtDur(ms) {
    const s = Math.max(0, Number(ms) || 0) / 1000;
    if (s < 60) return (s < 10 ? s.toFixed(1) : Math.round(s)) + " s";
    const m = Math.floor(s / 60);
    return m + " 分 " + Math.round(s % 60) + " 秒";
  }

  function fmtClock(ms) {
    const d = new Date(ms || Date.now());
    const p = (n) => String(n).padStart(2, "0");
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  function liveRow(l) {
    // 30s 黄色、60s 红色：一眼看出哪个请求卡住了
    const tone = l.elapsed_ms > 60000 ? "var(--err)" : l.elapsed_ms > 30000 ? "var(--warn)" : "";
    const ua = l.user_agent
      ? `<div class="small muted" style="max-width:230px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(l.user_agent)}">${esc(l.user_agent)}</div>`
      : "";
    return `<tr>
      <td class="small muted mono">${fmtClock(l.started_at_ms)}</td>
      <td class="small mono" style="${tone ? `color:${tone};font-weight:600` : ""}">${fmtDur(l.elapsed_ms)}</td>
      <td><span class="tag svc">${esc(l.service)}</span></td>
      <td class="mono small">${esc(l.endpoint)}</td>
      <td class="mono small">${esc(l.model || "-")}${l.stream ? ' <span class="tag">流式</span>' : ""}</td>
      <td>${esc(l.key_name || "-")}</td>
      <td class="small mono">${esc(l.client_ip || "-")}${ua}</td>
      <td><span class="tag ${STAGE_CLASS[l.stage] ?? ""}">${esc(l.stage_label || l.stage)}</span>
        ${l.provider_name ? `<div class="small muted" style="margin-top:3px">${esc(l.provider_name)}</div>` : ""}</td>
    </tr>`;
  }

  function paintLive(data) {
    const body = $("#live-body");
    if (!body) return;
    const items = data.items || [];
    body.innerHTML = items.length
      ? items.map(liveRow).join("")
      : '<tr><td colspan="8" class="muted small" style="padding:22px 12px;text-align:center">当前没有进行中的请求 · 请求一到站就会出现在这里</td></tr>';

    const el = items.map((x) => x.elapsed_ms);
    const longest = el.length ? Math.max(...el) : 0;
    const avg = el.length ? Math.round(el.reduce((a, b) => a + b, 0) / el.length) : 0;
    const sum = $("#live-summary");
    if (sum) {
      sum.innerHTML = items.length
        ? `进行中 <b style="color:var(--fg)">${items.length}</b> 个 · 最长 ${fmtDur(longest)} · 平均 ${fmtDur(avg)}`
        : '进行中 <b style="color:var(--fg)">0</b> 个 · 空闲中';
    }
  }

  async function renderLive() {
    stopLive();
    $("#content").innerHTML = `
      <div class="page-head">
        <div><h2>实时监控</h2><p>请求一到中转站就出现在这里（含尚未返回的）；结束后自动消失，明细去「用量与日志」查</p></div>
        <div class="head-actions"><span class="live-pill"><i class="live-dot"></i><span id="live-state">实时刷新 · 1s</span></span></div>
      </div>
      <div class="card">
        <div id="live-summary" class="small muted" style="margin-bottom:12px">正在读取…</div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>到达时间</th><th>已持续</th><th>服务</th><th>接口</th><th>模型</th><th>请求来源</th><th>客户端</th><th>状态</th></tr></thead>
            <tbody id="live-body"><tr><td colspan="8" class="muted small">正在读取…</td></tr></tbody>
          </table>
        </div>
      </div>`;

    const tick = async () => {
      if (App.liveBusy || App.route !== "live") return;
      App.liveBusy = true;
      try {
        const data = await api("/api/admin/live");
        if (App.route !== "live") return;
        paintLive(data);
        const st = $("#live-state");
        if (st && st.textContent !== "实时刷新 · 1s") st.textContent = "实时刷新 · 1s";
      } catch (e) {
        const st = $("#live-state");
        if (st) st.textContent = "刷新失败：" + e.message;
      } finally {
        App.liveBusy = false;
      }
    };

    await tick();
    if (App.route === "live") App.liveTimer = setInterval(tick, 1000);
  }

  /* ===================== 安全审查 ===================== */
  const SAFETY_ACTIONS = {
    blocked: ["拦截", "err"],
    sanitized: ["脱敏", "info"],
    review: ["待复核", "warn"],
    error: ["审查异常", "warn"],
    overflow: ["超限拦截", "warn"],
  };

  const SAFETY_RISK = {
    low: ["低", "ok"],
    medium: ["中", "info"],
    high: ["高", "warn"],
    critical: ["严重", "err"],
  };

  const SAFETY_HIT_ACTION = {
    block: ["拦截", "err"],
    sanitize: ["脱敏", "info"],
    review: ["复核", "warn"],
  };

  function safetyRiskTag(level) {
    const [label, tone] = SAFETY_RISK[level] || SAFETY_RISK.medium;
    return `<span class="tag ${tone}">风险 ${label}</span>`;
  }

  function safetyActionTag(action) {
    const [label, tone] = SAFETY_HIT_ACTION[action] || SAFETY_HIT_ACTION.block;
    return `<span class="tag ${tone}">${label}</span>`;
  }

  function safetyResultTag(result) {
    if (!result) return '<span class="muted small">—</span>';
    if (result === "passed") return '<span class="tag ok">通过</span>';
    if (result === "blocked_input") return '<span class="tag err">输入拦截</span>';
    if (result === "blocked_output") return '<span class="tag err">输出拦截</span>';
    if (result === "sanitized_input") return '<span class="tag info">输入脱敏</span>';
    if (result === "sanitized_output") return '<span class="tag info">输出脱敏</span>';
    if (result === "review_input") return '<span class="tag warn">输入待复核</span>';
    if (result === "review_output") return '<span class="tag warn">输出待复核</span>';
    if (result === "blocked_overflow") return '<span class="tag warn">超限拦截</span>';
    if (result === "error") return '<span class="tag warn">审查异常</span>';
    if (result === "skipped") return '<span class="tag">未审查</span>';
    return `<span class="tag">${esc(result)}</span>`;
  }

  function safetyDirectionLabel(d) {
    return d === "output" ? "输出" : "输入";
  }

  function safetyActionLabel(a) {
    const [label, tone] = SAFETY_ACTIONS[a] || [a || "-", ""];
    return `<span class="tag ${tone}">${esc(label)}</span>`;
  }

  async function renderSafety() {
    const tab = App.safetyTab || "policies";
    const [ov, libs, policies, builtins] = await Promise.all([
      api("/api/admin/safety/overview"),
      api("/api/admin/safety/libraries"),
      api("/api/admin/safety/policies"),
      api("/api/admin/safety/builtins").catch(() => null),
    ]);
    App.safetyLibraries = libs || [];
    App.safetyPolicies = policies || [];

    let records = { items: [], total: 0, page: 1, page_size: App.safetyRecords.size };
    if (tab === "records") {
      if (!App.keys.length) {
        try { App.keys = await api("/api/admin/keys"); } catch (e) { /* 筛选器降级 */ }
      }
      const f = App.safetyRecords.filters;
      const qs = new URLSearchParams({ page: App.safetyRecords.page, page_size: App.safetyRecords.size });
      if (f.direction) qs.set("direction", f.direction);
      if (f.action) qs.set("action", f.action);
      if (f.policy_id) qs.set("policy_id", f.policy_id);
      if (f.key_id) qs.set("key_id", f.key_id);
      records = await api("/api/admin/safety/records?" + qs.toString());
      App.safetyRecords.total = records.total;
    }

    const head = `
      <div class="page-head">
        <div><h2>安全审查</h2><p>词库 → 规则 → API Key 绑定；命中后直接返回默认内容，审查结果同时写入独立记录与请求日志</p></div>
        <div class="head-actions">
          <button class="btn-ghost btn-sm" id="new-lib">新建词库</button>
          <button class="btn-primary btn-sm" id="new-policy">新建规则</button>
        </div>
      </div>
      <div class="kpis">
        <div class="kpi"><div class="label">敏感词库</div><div class="value">${num(ov.libraries)}</div><div class="hint">可被多条规则复用</div></div>
        <div class="kpi"><div class="label">审查规则</div><div class="value">${num(ov.policies)}</div><div class="hint">Key 可单独绑定</div></div>
        <div class="kpi"><div class="label">今日审查记录</div><div class="value">${num(ov.reviews_today)}</div><div class="hint">仅记录命中与异常</div></div>
        <div class="kpi"><div class="label">今日输入拦截</div><div class="value" style="color:var(--err)">${num(ov.blocked_input_today)}</div><div class="hint">未发往上游</div></div>
        <div class="kpi"><div class="label">今日输出拦截</div><div class="value" style="color:var(--warn)">${num(ov.blocked_output_today)}</div><div class="hint">已替换为默认内容</div></div>
      </div>
      ${builtins ? (() => {
        const missing = (builtins.items || []).filter((x) => !x.exists).length;
        const modified = (builtins.items || []).filter((x) => x.modified).length;
        return `<div class="card" style="margin-bottom:16px;display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap">
          <div>
            <b>内置安全模板</b>
            <span class="tag info">v${esc(builtins.version || "-")}</span>
            <div class="small muted" style="margin-top:4px">
              7 套词库 / 4 条规则开箱即用；内置项可像普通词库一样直接编辑。
              ${missing ? `<span style="color:var(--warn)">缺失 ${missing} 项</span> · ` : ""}${modified ? `<span style="color:var(--warn)">已修改 ${modified} 项</span> · ` : ""}自建数据不会被恢复出厂覆盖。
            </div>
          </div>
          <div class="head-actions">
            <button class="btn-ghost btn-sm" id="bi-reinstall">补齐缺失</button>
            <button class="btn-ghost btn-sm" id="bi-restore">恢复出厂</button>
          </div>
        </div>`;
      })() : ""}
      <div class="tabs">
        ${[["policies", "审查规则"], ["libraries", "敏感词库"], ["records", "审查记录"]].map(([k, label]) =>
          `<button data-stab="${k}" class="${tab === k ? "active" : ""}">${label}</button>`).join("")}
      </div>`;

    let body = "";
    if (tab === "libraries") {
      const rows = App.safetyLibraries.map((l) => `<tr>
        <td><b>${esc(l.name)}</b>${l.is_builtin ? ' <span class="tag info">内置</span>' : ""}<div class="small muted">${esc(l.description || "")}</div></td>
        <td><span class="tag info">${num(l.term_count)} 条</span></td>
        <td>${safetyRiskTag(l.risk_level)} ${safetyActionTag(l.hit_action)}</td>
        <td>${l.is_enabled ? '<span class="tag ok">启用</span>' : '<span class="tag err">停用</span>'}</td>
        <td class="small muted">${fmtDate(l.updated_at)}</td>
        <td class="nowrap">
          <button class="btn-ghost btn-sm" data-export-lib="${esc(l.id)}" data-lib-name="${esc(l.name)}">导出</button>
          <button class="btn-ghost btn-sm" data-edit-lib="${esc(l.id)}">编辑</button>
          <button class="btn-danger btn-sm" data-del-lib="${esc(l.id)}">删除</button>
        </td>
      </tr>`).join("");
      body = `<div class="card">
        <div class="table-toolbar">
          <div class="small muted">共 ${num(App.safetyLibraries.length)} 套词库；JSON 导出可在另一环境整批导入，TXT 便于人工编辑。</div>
          <div class="head-actions">
            <button class="btn-ghost btn-sm" id="lib-import">导入</button>
            <button class="btn-ghost btn-sm" id="lib-export-all">导出全部</button>
          </div>
        </div>
        <div class="table-wrap"><table class="sticky-last">
        <thead><tr><th>词库</th><th>词条</th><th>风险 / 动作</th><th>状态</th><th>更新时间</th><th>操作</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="6" class="muted small">还没有词库，先新建一套敏感词</td></tr>'}</tbody>
      </table></div></div>`;
    } else if (tab === "records") {
      const f = App.safetyRecords.filters;
      const rows = records.items.map((r) => `<tr class="safety-record-row" data-review="${esc(r.id)}" style="cursor:pointer">
        <td class="small muted">${fmtDate(r.created_at)}</td>
        <td>${esc(r.key_name || "-")}</td>
        <td class="small">${esc(r.policy_name || "-")}</td>
        <td class="small">${safetyDirectionLabel(r.direction)}</td>
        <td>${safetyActionLabel(r.action)}</td>
        <td class="small">${(r.matched_terms || []).slice(0, 3).map((m) => `<span class="tag warn">${esc(m.term)}</span>`).join(" ") || '<span class="muted">—</span>'}</td>
        <td class="small muted" style="max-width:360px;overflow:hidden;text-overflow:ellipsis">${esc((r.text_excerpt || "").slice(0, 120))}</td>
        <td class="mono small" title="${esc(r.request_id)}">${esc((r.request_id || "").slice(0, 12))}…</td>
      </tr>`).join("");
      const pages = Math.max(1, Math.ceil(records.total / App.safetyRecords.size));
      body = `<div class="card">
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
          <select id="sr-direction" style="width:120px">
            <option value="">全部方向</option>
            <option value="input" ${f.direction === "input" ? "selected" : ""}>输入</option>
            <option value="output" ${f.direction === "output" ? "selected" : ""}>输出</option>
          </select>
          <select id="sr-action" style="width:130px">
            <option value="">全部动作</option>
            <option value="blocked" ${f.action === "blocked" ? "selected" : ""}>拦截</option>
            <option value="sanitized" ${f.action === "sanitized" ? "selected" : ""}>脱敏</option>
            <option value="review" ${f.action === "review" ? "selected" : ""}>待复核</option>
            <option value="error" ${f.action === "error" ? "selected" : ""}>审查异常</option>
            <option value="overflow" ${f.action === "overflow" ? "selected" : ""}>超限拦截</option>
          </select>
          <select id="sr-policy" style="width:190px">
            <option value="">全部规则</option>
            ${App.safetyPolicies.map((p) => `<option value="${esc(p.id)}" ${f.policy_id === p.id ? "selected" : ""}>${esc(p.name)}</option>`).join("")}
          </select>
          <select id="sr-key" style="width:180px">
            <option value="">全部 Key</option>
            ${(App.keys || []).map((k) => `<option value="${esc(k.id)}" ${f.key_id === k.id ? "selected" : ""}>${esc(k.name)}</option>`).join("")}
          </select>
          <button class="btn-primary btn-sm" id="sr-apply">查询</button>
          <button class="btn-ghost btn-sm" id="sr-reset">重置</button>
        </div>
        <div class="table-wrap"><table>
          <thead><tr><th>时间</th><th>Key</th><th>规则</th><th>方向</th><th>动作</th><th>命中词</th><th>原文摘录</th><th>request_id</th></tr></thead>
          <tbody>${rows || '<tr><td colspan="8" class="muted small">暂无命中或异常记录</td></tr>'}</tbody>
        </table></div>
        <div class="pager"><span>共 ${num(records.total)} 条 · 第 ${App.safetyRecords.page}/${pages} 页</span>
          <div class="btns">
            <button class="btn-ghost btn-sm" id="sr-prev" ${App.safetyRecords.page <= 1 ? "disabled" : ""}>上一页</button>
            <button class="btn-ghost btn-sm" id="sr-next" ${App.safetyRecords.page >= pages ? "disabled" : ""}>下一页</button>
          </div>
        </div>
      </div>`;
    } else {
      const libMap = {};
      App.safetyLibraries.forEach((l) => { libMap[l.id] = l.name; });
      const policyRows = App.safetyPolicies.map((p) => {
        const libNames = (p.library_ids || []).map((id) => libMap[id] || id);
        const shown = libNames.slice(0, 3).map((n) => `<span class="tag">${esc(n)}</span>`).join("");
        const more = libNames.length > 3
          ? `<span class="tag info" title="${esc(libNames.slice(3).join("、"))}">+${libNames.length - 3}</span>`
          : "";
        const libsCell = libNames.length
          ? `<div class="lib-tags" title="${esc(libNames.join("、"))}">${shown}${more}</div>`
          : '<span class="muted small">未引用词库</span>';
        const dirs = [p.input_enabled ? "输入" : "", p.output_enabled ? "输出" : ""].filter(Boolean).join(" + ") || "无";
        return `<tr>
          <td><b>${esc(p.name)}</b>${p.is_builtin ? ' <span class="tag info">内置</span>' : ""}${p.is_default ? ' <span class="tag ok">默认</span>' : ""}<div class="small muted">${esc(p.description || "")}</div></td>
          <td class="cell-libs small">${libsCell}</td>
          <td class="small">${esc(dirs)}</td>
          <td class="small">${esc(p.match_mode === "word" ? "词边界" : "子串")} · 阈值 ${num(p.threshold)}</td>
          <td class="small">${num(p.bound_keys)} 个</td>
          <td>${p.is_enabled ? '<span class="tag ok">启用</span>' : '<span class="tag err">停用</span>'}</td>
          <td class="nowrap">
            <button class="btn-ghost btn-sm" data-edit-policy="${esc(p.id)}">编辑</button>
            <button class="btn-danger btn-sm" data-del-policy="${esc(p.id)}">删除</button>
          </td>
        </tr>`;
      }).join("");
      body = `<div class="card" style="margin-bottom:16px">
        <h3>规则试审查 <span class="sub">不会写入记录</span></h3>
        <div class="form-row">
          <div class="field"><span>使用规则</span><select id="test-policy">
            ${App.safetyPolicies.map((p) => `<option value="${esc(p.id)}">${esc(p.name)}${p.is_default ? "（默认）" : ""}</option>`).join("")}
          </select></div>
          <div class="field"><span>审查方向</span><select id="test-direction"><option value="input">输入</option><option value="output">输出</option></select></div>
        </div>
        <div class="field"><span>测试文本</span><textarea id="test-text" rows="3" placeholder="粘贴一段文本，检查会命中哪些词"></textarea></div>
        <div style="display:flex;gap:8px;align-items:flex-start">
          <button class="btn-primary btn-sm" id="test-run" style="flex:0 0 auto;white-space:nowrap">开始测试</button>
          <span id="test-result" class="small muted" style="min-width:0;overflow-wrap:anywhere"></span>
        </div>
      </div>
      <div class="card"><div class="table-wrap"><table class="sticky-last">
        <thead><tr><th>规则</th><th>引用词库</th><th>方向</th><th>匹配</th><th>绑定 Key</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>${policyRows || '<tr><td colspan="7" class="muted small">还没有审查规则；新建规则后会自动成为默认规则</td></tr>'}</tbody>
      </table></div></div>`;
    }

    $("#content").innerHTML = head + body;

    $$("#content [data-stab]").forEach((b) => {
      b.onclick = () => { App.safetyTab = b.dataset.stab; renderSafety(); };
    });
    $("#new-lib").onclick = () => openLibraryForm(null);
    $("#new-policy").onclick = () => openPolicyForm(null);
    const builtinAction = async (path, tip) => {
      try {
        const r = await api("/api/admin/safety/builtins/" + path, "POST", {});
        toast(`${tip}：词库 ${num(r.libraries)} 项，规则 ${num(r.policies)} 项`);
        renderSafety();
      } catch (e) { toast(e.message, "err"); }
    };
    if ($("#bi-reinstall")) $("#bi-reinstall").onclick = () => builtinAction("reinstall", "已补齐缺失模板");
    if ($("#bi-restore")) $("#bi-restore").onclick = async () => {
      if (!confirm("恢复出厂会覆盖所有内置词库与规则的修改（自建数据不受影响），继续？")) return;
      await builtinAction("restore", "已恢复出厂模板");
    };

    $$("#content [data-edit-lib]").forEach((b) => {
      b.onclick = () => openLibraryForm(App.safetyLibraries.find((x) => x.id === b.dataset.editLib));
    });
    if ($("#lib-import")) $("#lib-import").onclick = () => openLibraryImport();
    if ($("#lib-export-all")) $("#lib-export-all").onclick = async () => {
      try {
        await downloadFile("/api/admin/safety/libraries/export?fmt=json", "safety-libraries.json");
        toast("已导出全部词库");
      } catch (e) { toast(e.message, "err"); }
    };
    $$("#content [data-export-lib]").forEach((b) => {
      b.onclick = async () => {
        const fallback = (b.dataset.libName || "library") + ".txt";
        try {
          await downloadFile("/api/admin/safety/libraries/export?fmt=txt&ids=" + encodeURIComponent(b.dataset.exportLib), fallback);
        } catch (e) { toast(e.message, "err"); }
      };
    });
    $$("#content [data-del-lib]").forEach((b) => {
      b.onclick = async () => {
        if (!confirm("删除该词库？被规则引用时会被拒绝。")) return;
        try { await api("/api/admin/safety/libraries/" + b.dataset.delLib, "DELETE"); toast("已删除"); renderSafety(); }
        catch (e) { toast(e.message, "err"); }
      };
    });
    $$("#content [data-edit-policy]").forEach((b) => {
      b.onclick = () => openPolicyForm(App.safetyPolicies.find((x) => x.id === b.dataset.editPolicy));
    });
    $$("#content [data-del-policy]").forEach((b) => {
      b.onclick = async () => {
        if (!confirm("删除该规则？仍有 Key 绑定时会被拒绝。")) return;
        try { await api("/api/admin/safety/policies/" + b.dataset.delPolicy, "DELETE"); toast("已删除"); renderSafety(); }
        catch (e) { toast(e.message, "err"); }
      };
    });

    if (tab === "policies") {
      const run = async () => {
        const text = $("#test-text").value;
        const policy_id = $("#test-policy").value;
        const direction = $("#test-direction").value;
        const out = $("#test-result");
        out.textContent = "审查中…";
        try {
          const r = await api("/api/admin/safety/test", "POST", { text, policy_id, direction });
          out.innerHTML = r.blocked
            ? `<span style="color:var(--err)">命中，将拦截：</span>${(r.matched || []).map((m) => `<span class="tag err">${esc(m.term)}</span>`).join(" ")} <span class="muted">${esc(r.detail || "")}</span>`
            : '<span style="color:var(--ok)">未命中，将放行</span>';
        } catch (e) { out.textContent = e.message; }
      };
      $("#test-run").onclick = run;
    }

    if (tab === "records") {
      $$("#content .safety-record-row").forEach((tr) => {
        tr.onclick = () => {
          const item = records.items.find((x) => x.id === tr.dataset.review);
          if (item) openSafetyReview(item);
        };
      });
      const collect = () => ({
        direction: $("#sr-direction").value,
        action: $("#sr-action").value,
        policy_id: $("#sr-policy").value,
        key_id: $("#sr-key").value,
      });
      $("#sr-apply").onclick = () => { App.safetyRecords.filters = collect(); App.safetyRecords.page = 1; renderSafety(); };
      $("#sr-reset").onclick = () => { App.safetyRecords.filters = {}; App.safetyRecords.page = 1; renderSafety(); };
      $("#sr-prev").onclick = () => { App.safetyRecords.page = Math.max(1, App.safetyRecords.page - 1); renderSafety(); };
      $("#sr-next").onclick = () => { App.safetyRecords.page += 1; renderSafety(); };
    }
  }

  /* 解析导入文件：本系统导出的 JSON（可批量）或纯词条 TXT（一份一个词库） */
  function parseLibraryFile(fileName, text) {
    const clean = (text || "").replace(/^\uFEFF/, "");
    const looksJson = /\.json$/i.test(fileName || "") || /^[\s]*[\[{]/.test(clean);
    const countTerms = (t) => (t || "").split(/\r?\n/).filter((x) => x.trim() && !x.trim().startsWith("#")).length;

    if (!looksJson) {
      const base = (fileName || "导入词库").replace(/\.[^.\\/]+$/, "").trim() || "导入词库";
      return [{
        name: base,
        description: `从 ${fileName} 导入`,
        terms: clean,
        risk_level: "medium",
        hit_action: "block",
        is_enabled: true,
        builtin_key: "",
        _count: countTerms(clean),
      }];
    }

    let data;
    try { data = JSON.parse(clean); } catch (e) { throw new Error("JSON 格式有误：" + e.message); }
    const list = Array.isArray(data) ? data : (Array.isArray(data.libraries) ? data.libraries : [data]);
    if (!list.length) throw new Error("文件里没有可导入的词库");
    return list.map((x, i) => {
      if (!x || typeof x !== "object" || Array.isArray(x)) throw new Error(`第 ${i + 1} 项不是词库对象`);
      const terms = String(x.terms == null ? "" : x.terms);
      return {
        name: String(x.name || "").trim() || `导入词库 ${i + 1}`,
        description: String(x.description || ""),
        terms,
        risk_level: ["low", "medium", "high", "critical"].includes(x.risk_level) ? x.risk_level : "medium",
        hit_action: ["block", "sanitize", "review"].includes(x.hit_action) ? x.hit_action : "block",
        is_enabled: x.is_enabled !== false,
        builtin_key: String(x.builtin_key || ""),
        _count: countTerms(terms),
      };
    });
  }

  function openLibraryImport() {
    modal(`
      <div class="modal-head"><h3>导入敏感词库</h3><button class="link" id="x">✕</button></div>
      <div class="modal-body">
        <div class="field"><span>选择文件</span>
          <input type="file" id="import-file" accept=".json,.txt,application/json,text/plain" />
        </div>
        <div class="hint-box small">支持本系统导出的 JSON（可批量，含风险等级与命中动作），也支持纯词条 TXT（一份文件一个词库，文件名作为词库名）。</div>
        <div class="field"><span>没有现成文件？先下载模板</span>
          <div class="head-actions" style="justify-content:flex-start">
            <button class="btn-ghost btn-sm" id="tpl-json">下载 JSON 模板</button>
            <button class="btn-ghost btn-sm" id="tpl-txt">下载 TXT 模板</button>
          </div>
        </div>
        <div class="field"><span>遇到同名词库时</span>
          <select id="import-mode">
            <option value="merge">合并去重（推荐：保留原库的风险等级与动作，只补新词）</option>
            <option value="overwrite">覆盖同名库（文件即最终结果：文件中没有的词条会被删除）</option>
            <option value="copy">另存副本（原库不动，新建「名称（导入）」）</option>
          </select>
        </div>
        <div id="import-preview" class="hint-box small">选择文件后会在这里显示解析结果。</div>
      </div>
      <div class="modal-foot">
        <button class="btn-ghost" id="cancel">取消</button>
        <button class="btn-primary" id="do-import" disabled>开始导入</button>
      </div>`, true);

    let parsed = null;
    const preview = $("#modal-root #import-preview");
    const submit = $("#modal-root #do-import");
    $("#modal-root #x").onclick = closeModal;
    $("#modal-root #cancel").onclick = closeModal;

    const grabTemplate = (fmt, fallback) => {
      downloadFile("/api/admin/safety/libraries/template?fmt=" + fmt, fallback)
        .catch((err) => toast(err.message, "err"));
    };
    $("#modal-root #tpl-json").onclick = () => grabTemplate("json", "safety-libraries-template.json");
    $("#modal-root #tpl-txt").onclick = () => grabTemplate("txt", "safety-library-template.txt");

    $("#modal-root #import-file").onchange = (e) => {
      parsed = null;
      submit.disabled = true;
      const f = e.target.files && e.target.files[0];
      if (!f) { preview.textContent = "选择文件后会在这里显示解析结果。"; return; }
      if (f.size > 8 * 1024 * 1024) { preview.textContent = "文件超过 8MB，请拆分后再导入。"; return; }
      const reader = new FileReader();
      reader.onload = () => {
        try {
          const items = parseLibraryFile(f.name, String(reader.result || ""));
          const total = items.reduce((s, x) => s + x._count, 0);
          parsed = items;
          preview.innerHTML = `<div>共 <b>${items.length}</b> 个词库、<b>${num(total)}</b> 条词条：</div>`
            + items.slice(0, 20).map((x) =>
              `<div style="margin-top:4px">${esc(x.name)} · ${num(x._count)} 条 · ${safetyRiskTag(x.risk_level)} ${safetyActionTag(x.hit_action)}</div>`).join("")
            + (items.length > 20 ? `<div style="margin-top:4px">…另有 ${items.length - 20} 个词库</div>` : "");
          submit.disabled = false;
        } catch (err) {
          preview.textContent = "解析失败：" + err.message;
        }
      };
      reader.readAsText(f, "utf-8");
    };

    submit.onclick = async () => {
      if (!parsed) return;
      const mode = $("#modal-root #import-mode").value;
      const libraries = parsed.map((x) => {
        const { _count, ...rest } = x;
        return rest;
      });
      submit.disabled = true;
      try {
        const r = await api("/api/admin/safety/libraries/import", "POST", { libraries, mode });
        closeModal();
        const removedTip = r.terms_removed ? ` · 移除词条 ${num(r.terms_removed)}` : "";
        toast(
          `导入完成：新建 ${r.created} · 合并 ${r.merged} · 覆盖 ${r.overwritten} · 跳过 ${r.skipped}，`
            + `新增词条 ${num(r.terms_added)}${removedTip}`,
          r.terms_removed ? "warn" : ""
        );
        renderSafety();
      } catch (err) {
        submit.disabled = false;
        toast(err.message, "err");
      }
    };
  }

  function openLibraryForm(lib) {
    const isEdit = !!lib;
    modal(`
      <div class="modal-head"><h3>${isEdit ? "编辑词库" : "新建敏感词库"}</h3><button class="link" id="x">✕</button></div>
      <div class="modal-body">
        <div class="field"><span>名称</span><input name="name" value="${esc(lib ? lib.name : "")}" placeholder="如 通用违规词 / 某业务专有词" /></div>
        <div class="field"><span>说明</span><input name="description" value="${esc(lib ? lib.description || "" : "")}" placeholder="用途或维护人（可选）" /></div>
        <div class="field"><span>词条（一行一个）</span>
          <textarea name="terms" rows="14" spellcheck="false" placeholder="普通词：直接写&#10;正则：re:你的表达式&#10;注释：# 这行会被忽略">${esc(lib ? lib.terms || "" : "")}</textarea>
        </div>
        <label class="inline"><input type="checkbox" name="is_enabled" ${!lib || lib.is_enabled ? "checked" : ""} /> 启用该词库</label>
        <div class="form-row">
          <div class="field"><span>风险等级</span>
            <select name="risk_level">
              ${[["low","低"],["medium","中"],["high","高"],["critical","严重"]].map(([v,t]) =>
                `<option value="${v}" ${((lib && lib.risk_level) || "medium") === v ? "selected" : ""}>${t}（${v}）</option>`).join("")}
            </select>
          </div>
          <div class="field"><span>命中动作</span>
            <select name="hit_action">
              <option value="block" ${!lib || lib.hit_action === "block" ? "selected" : ""}>拦截（返回默认回复）</option>
              <option value="sanitize" ${lib && lib.hit_action === "sanitize" ? "selected" : ""}>脱敏（替换为 *** 后放行）</option>
              <option value="review" ${lib && lib.hit_action === "review" ? "selected" : ""}>复核（放行并标记待审）</option>
            </select>
          </div>
        </div>
        <div class="hint-box small">一条规则引用多个词库时按最严动作汇总：拦截 &gt; 脱敏 &gt; 复核。涉未成年色情等零容忍词库应保持「拦截」。</div>
        ${lib && lib.is_builtin ? '<div class="hint-box small">这是内置词库，可直接修改并长期保留；需要还原时到页面顶部点「恢复出厂」。</div>' : ""}
        <div class="hint-box small">词条会做全半角/大小写/零宽字符归一化；正则只作用于归一化后的文本，保存前会校验语法。</div>
      </div>
      <div class="modal-foot">
        <button class="btn-ghost" id="cancel">取消</button>
        <button class="btn-primary" id="save">保存</button>
      </div>`, true);
    $("#modal-root #x").onclick = closeModal;
    $("#modal-root #cancel").onclick = closeModal;
    $("#modal-root #save").onclick = async () => {
      const root = $("#modal-root");
      const val = (n) => root.querySelector(`[name="${n}"]`).value;
      const payload = {
        name: val("name").trim(),
        description: val("description").trim(),
        terms: val("terms"),
        is_enabled: root.querySelector('[name="is_enabled"]').checked,
        risk_level: val("risk_level"),
        hit_action: val("hit_action"),
      };
      if (!payload.name) { toast("名称不能为空", "err"); return; }
      try {
        if (isEdit) await api("/api/admin/safety/libraries/" + lib.id, "PUT", payload);
        else await api("/api/admin/safety/libraries", "POST", payload);
        closeModal(); toast("词库已保存"); renderSafety();
      } catch (e) { toast(e.message, "err"); }
    };
  }

  function openPolicyForm(p) {
    const isEdit = !!p;
    const chosen = new Set((p && p.library_ids) || []);
    const libChecks = App.safetyLibraries.map((l) => `<label class="inline" style="margin:0 14px 8px 0">
      <input type="checkbox" name="lib_${esc(l.id)}" ${chosen.has(l.id) ? "checked" : ""} /> ${esc(l.name)}
      <span class="muted small">(${num(l.term_count)})</span>
    </label>`).join("") || '<div class="small muted">还没有词库，先到「敏感词库」新建</div>';
    modal(`
      <div class="modal-head"><h3>${isEdit ? "编辑审查规则" : "新建审查规则"}</h3><button class="link" id="x">✕</button></div>
      <div class="modal-body">
        <div class="form-row">
          <div class="field"><span>规则名称</span><input name="name" value="${esc(p ? p.name : "")}" placeholder="如 默认规则 / 严格模式" /></div>
          <div class="field"><span>命中阈值</span><input name="threshold" type="number" min="1" value="${p ? p.threshold : 1}" /></div>
        </div>
        <div class="field"><span>说明</span><input name="description" value="${esc(p ? p.description || "" : "")}" /></div>
        <div class="section-title">引用词库（可多选）</div>
        <div>${libChecks}</div>
        <div class="form-row">
          <div class="field"><span>审查方向</span>
            <div style="display:flex;gap:14px;padding-top:4px">
              <label class="inline"><input type="checkbox" name="input_enabled" ${!p || p.input_enabled ? "checked" : ""} /> 输入</label>
              <label class="inline"><input type="checkbox" name="output_enabled" ${!p || p.output_enabled ? "checked" : ""} /> 输出</label>
            </div>
          </div>
          <div class="field"><span>匹配方式</span>
            <select name="match_mode">
              <option value="contains" ${!p || p.match_mode === "contains" ? "selected" : ""}>子串匹配（中文友好）</option>
              <option value="word" ${p && p.match_mode === "word" ? "selected" : ""}>词边界（拉丁词不误伤）</option>
            </select>
          </div>
        </div>
        <div class="form-row">
          <div class="field"><span>最大审查字符数</span><input name="max_text_chars" type="number" min="1000" value="${p ? p.max_text_chars : 200000}" /></div>
          <div class="field"><span>异常处理</span>
            <select name="fail_closed">
              <option value="0" ${!p || !p.fail_closed ? "selected" : ""}>放行并记录异常</option>
              <option value="1" ${p && p.fail_closed ? "selected" : ""}>拦截（fail-closed）</option>
            </select>
          </div>
        </div>
        <div class="field"><span>默认回复（命中后返回给调用方）</span><textarea name="default_reply" rows="3">${esc(p ? p.default_reply : "抱歉，该内容未通过安全审查，无法为您生成回复。")}</textarea></div>
        <div style="display:flex;gap:18px;flex-wrap:wrap">
          <label class="inline"><input type="checkbox" name="is_enabled" ${!p || p.is_enabled ? "checked" : ""} /> 启用规则</label>
          <label class="inline"><input type="checkbox" name="is_default" ${p && p.is_default ? "checked" : ""} /> 设为默认规则</label>
          <label class="inline"><input type="checkbox" name="normalize" ${!p || p.normalize ? "checked" : ""} /> 归一化（全半角/零宽/大小写）</label>
          <label class="inline"><input type="checkbox" name="aggressive_normalize" ${p && p.aggressive_normalize ? "checked" : ""} /> 拆字识别（毒 品 / 毒-品）</label>
          <label class="inline"><input type="checkbox" name="case_sensitive" ${p && p.case_sensitive ? "checked" : ""} /> 区分大小写</label>
        </div>
        ${p && p.is_builtin ? '<div class="hint-box small">这是内置规则，可直接修改并长期保留；需要还原时到页面顶部点「恢复出厂」。</div>' : ""}
        <div class="hint-box small">默认规则用于「继承默认」的 Key；输出审查在流式场景会整段缓冲后再返回，命中时已生成的内容不会发给调用方。</div>
      </div>
      <div class="modal-foot">
        <button class="btn-ghost" id="cancel">取消</button>
        <button class="btn-primary" id="save">保存</button>
      </div>`, true);
    $("#modal-root #x").onclick = closeModal;
    $("#modal-root #cancel").onclick = closeModal;
    $("#modal-root #save").onclick = async () => {
      const root = $("#modal-root");
      const val = (n) => root.querySelector(`[name="${n}"]`).value;
      const checked = (n) => root.querySelector(`[name="${n}"]`).checked;
      const library_ids = App.safetyLibraries
        .filter((l) => root.querySelector(`[name="lib_${l.id}"]`).checked)
        .map((l) => l.id);
      const payload = {
        name: val("name").trim(),
        description: val("description").trim(),
        library_ids,
        input_enabled: checked("input_enabled"),
        output_enabled: checked("output_enabled"),
        match_mode: val("match_mode"),
        threshold: Number(val("threshold") || 1),
        max_text_chars: Number(val("max_text_chars") || 200000),
        fail_closed: val("fail_closed") === "1",
        default_reply: val("default_reply").trim() || "抱歉，该内容未通过安全审查，无法为您生成回复。",
        is_enabled: checked("is_enabled"),
        is_default: checked("is_default"),
        normalize: checked("normalize"),
        aggressive_normalize: checked("aggressive_normalize"),
        case_sensitive: checked("case_sensitive"),
      };
      if (!payload.name) { toast("规则名称不能为空", "err"); return; }
      if (!library_ids.length) { toast("至少引用一个词库", "err"); return; }
      try {
        if (isEdit) await api("/api/admin/safety/policies/" + p.id, "PUT", payload);
        else await api("/api/admin/safety/policies", "POST", payload);
        closeModal(); toast("规则已保存"); renderSafety();
      } catch (e) { toast(e.message, "err"); }
    };
  }

  function openSafetyReview(item) {
    const matched = (item.matched_terms || []).map((m) =>
      `<span class="tag err">${esc(m.term)}${m.library ? `<span class="muted"> · ${esc(m.library)}</span>` : ""}</span>`
    ).join(" ") || '<span class="muted small">无命中词（可能是审查异常）</span>';
    modal(`
      <div class="modal-head"><h3>审查记录 <span class="sub mono">${esc(item.request_id || item.id)}</span></h3><button class="link" id="x">✕</button></div>
      <div class="modal-body">
        <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
          ${safetyActionLabel(item.action)}
          <span class="tag svc">${esc(item.key_name || "-")}</span>
          <span class="tag info">${esc(item.policy_name || "-")}</span>
          <span class="tag">${safetyDirectionLabel(item.direction)}</span>
          ${item.is_stream ? '<span class="tag">流式</span>' : ""}
          <span class="muted small">${fmtDate(item.created_at)}</span>
        </div>
        <div class="small muted">命中词</div>
        <div>${matched}</div>
        <div class="small muted">词库</div>
        <div class="small">${esc((item.library_names || []).join("、") || "-")}</div>
        <div class="small muted">命中说明</div>
        <div class="small">${esc(item.detail || "-")}</div>
        <div class="small muted">原文摘录</div>
        ${codeBlock(item.text_excerpt)}
        <div class="small muted">模型 / 接口</div>
        <div class="small mono">${esc(item.model || "-")} · ${esc(item.endpoint || "-")}</div>
      </div>
      <div class="modal-foot"><button class="btn-ghost" id="close">关闭</button></div>`, true);
    $("#modal-root #x").onclick = closeModal;
    $("#modal-root #close").onclick = closeModal;
  }

  /* ===================== 日志 ===================== */
  async function renderLogs() {
    const f = App.logs.filters;
    const qs = new URLSearchParams({ page: App.logs.page, page_size: App.logs.size });
    if (f.service) qs.set("service", f.service);
    if (f.status) qs.set("status_filter", f.status);
    if (f.key_id) qs.set("key_id", f.key_id);
    if (f.model) qs.set("model", f.model);
    if (f.error_type) qs.set("error_type", f.error_type);
    if (f.safety_result) qs.set("safety_result", f.safety_result);
    if (f.request_id) qs.set("request_id", f.request_id);
    if (f.start) qs.set("start", new Date(f.start).toISOString());
    if (f.end) qs.set("end", new Date(f.end).toISOString());

    const data = await api("/api/admin/logs?" + qs.toString());
    App.logs.total = data.total;
    const keys = App.keys.length ? App.keys : await api("/api/admin/keys");
    let ret = { days: 7, detail_rows: 0, total_rows: 0 };
    try { ret = await api("/api/admin/logs/retention"); } catch (e) { /* 不影响列表 */ }

    const rows = data.items.map((l) => {
      const etLabel = (ERROR_TYPES.find((t) => t[0] === l.error_type) || [null, l.error_type])[1];
      return `<tr class="log-row" data-log="${esc(l.id)}" title="点击查看详情">
      <td class="small muted">${fmtDate(l.created_at)}</td>
      <td><span class="tag svc">${esc(l.service)}</span></td>
      <td class="mono small">${esc(l.endpoint)}</td>
      <td>${esc(l.provider_name || "-")}</td>
      <td class="mono small">${esc(l.model || "-")}</td>
      <td>${l.status === "success"
        ? '<span class="tag ok">成功</span>'
        : `<span class="tag err">失败</span>${l.error_type ? `<div class="small muted" style="margin-top:3px">${esc(etLabel)}</div>` : ""}`}</td>
      <td>${safetyResultTag(l.safety_result)}${l.safety_policy ? `<div class="small muted" style="margin-top:3px">${esc(l.safety_policy)}</div>` : ""}</td>
      <td class="small">${num(l.latency_ms)} ms</td>
      <td class="small">${l.tokens_in + l.tokens_out > 0 ? `${num(l.tokens_in)}→${num(l.tokens_out)}` : "-"}</td>
      <td class="small">${l.char_count ? num(l.char_count) : "-"}</td>
      <td class="small">${money(l.cost)}</td>
      <td class="small">${esc(l.key_name || "-")}</td>
      <td class="small muted">${l.has_detail ? "查看 ›" : "<span title='该条记录保留，详情内容未记录或已按保留策略清空'>—</span>"}</td>
    </tr>`;
    }).join("");

    const pages = Math.max(1, Math.ceil(data.total / App.logs.size));
    const adv = !!App.logs.adv;
    // 收起状态下如果模型名/request_id 有值，按钮上标出隐藏条件数，避免「悄悄生效」
    const hidCount = [f.model, f.request_id].filter(Boolean).length;
    $("#content").innerHTML = `
      <div class="page-head">
        <div><h2>用量与日志</h2><p>所有服务的调用明细，含 tokens、费用与错误原因；点击任意一行查看请求与上游返回详情</p></div>
        <div class="head-actions">
          <button class="btn-ghost btn-sm" id="f-settings" title="日志详情保留与清理">日志设置</button>
        </div>
      </div>
      <div class="card">
        <div id="f-bar" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:${adv ? "10px" : "14px"}">
          <select id="f-service" style="width:160px">
            <option value="">全部服务</option>
            ${App.services.map((s) => `<option value="${s.key}" ${f.service === s.key ? "selected" : ""}>${esc(s.label)}</option>`).join("")}
          </select>
          <select id="f-status" style="width:120px">
            <option value="">全部状态</option>
            <option value="success" ${f.status === "success" ? "selected" : ""}>成功</option>
            <option value="error" ${f.status === "error" ? "selected" : ""}>失败</option>
          </select>
          <select id="f-error-type" style="width:190px">
            <option value="">全部错误类型</option>
            ${ERROR_TYPES.map(([v, label]) => `<option value="${v}" ${f.error_type === v ? "selected" : ""}>${esc(label)}</option>`).join("")}
          </select>
          <select id="f-safety" style="width:150px">
            <option value="">全部审查结果</option>
            <option value="passed" ${f.safety_result === "passed" ? "selected" : ""}>通过</option>
            <option value="blocked_input" ${f.safety_result === "blocked_input" ? "selected" : ""}>输入拦截</option>
            <option value="blocked_output" ${f.safety_result === "blocked_output" ? "selected" : ""}>输出拦截</option>
            <option value="blocked_overflow" ${f.safety_result === "blocked_overflow" ? "selected" : ""}>超限拦截</option>
            <option value="error" ${f.safety_result === "error" ? "selected" : ""}>审查异常</option>
            <option value="skipped" ${f.safety_result === "skipped" ? "selected" : ""}>未审查</option>
          </select>
          <select id="f-key" style="width:180px">
            <option value="">全部 Key</option>
            ${keys.map((k) => `<option value="${esc(k.id)}" ${f.key_id === k.id ? "selected" : ""}>${esc(k.name)}</option>`).join("")}
          </select>
          <input id="f-start" type="datetime-local" value="${esc(f.start || "")}" style="width:172px" />
          <input id="f-end" type="datetime-local" value="${esc(f.end || "")}" style="width:172px" />
          <button class="btn-primary btn-sm" id="f-apply">查询</button>
          <button class="btn-ghost btn-sm" id="f-reset">重置</button>
          <button class="btn-ghost btn-sm" id="f-more">${adv ? "收起筛选 ▴" : `更多筛选 ▾${hidCount ? ` · ${hidCount}` : ""}`}</button>
        </div>
        <div id="f-adv" class="log-adv${adv ? "" : " hidden"}">
          <input id="f-model" placeholder="模型名（可选）" value="${esc(f.model || "")}" style="width:200px" />
          <input id="f-request-id" placeholder="request_id（可选）" value="${esc(f.request_id || "")}" style="width:280px" />
          <span class="small muted">按模型名 / request_id 精确定位单次调用</span>
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>时间</th><th>服务</th><th>接口</th><th>厂商</th><th>模型</th><th>状态</th><th>审查</th><th>耗时</th><th>tokens</th><th>字符</th><th>费用</th><th>Key</th><th>详情</th></tr></thead>
            <tbody>${rows || '<tr><td colspan="13" class="muted small">没有匹配的调用记录</td></tr>'}</tbody>
          </table>
        </div>
        <div class="pager">
          <span>共 ${num(data.total)} 条 · 第 ${App.logs.page}/${pages} 页</span>
          <div class="btns">
            <button class="btn-ghost btn-sm" id="prev" ${App.logs.page <= 1 ? "disabled" : ""}>上一页</button>
            <button class="btn-ghost btn-sm" id="next" ${App.logs.page >= pages ? "disabled" : ""}>下一页</button>
          </div>
        </div>
      </div>`;

    const collect = () => {
      App.logs.filters = {
        service: $("#f-service").value,
        status: $("#f-status").value,
        error_type: $("#f-error-type").value,
        safety_result: $("#f-safety").value,
        key_id: $("#f-key").value,
        model: $("#f-model").value.trim(),
        request_id: $("#f-request-id").value.trim(),
        start: $("#f-start").value,
        end: $("#f-end").value,
      };
    };
    $("#f-apply").onclick = () => { collect(); App.logs.page = 1; renderLogs(); };
    $("#f-reset").onclick = () => {
      App.logs.filters = {};
      App.logs.page = 1;
      App.logs.adv = false;
      renderLogs();
    };
    $("#prev").onclick = () => { collect(); App.logs.page = Math.max(1, App.logs.page - 1); renderLogs(); };
    $("#next").onclick = () => { collect(); App.logs.page += 1; renderLogs(); };

    // 高级筛选默认收起：只切显隐，不重新请求，已填的条件不会丢
    $("#f-more").onclick = () => {
      App.logs.adv = !App.logs.adv;
      $("#f-adv").classList.toggle("hidden", !App.logs.adv);
      $("#f-bar").style.marginBottom = App.logs.adv ? "10px" : "14px";
      $("#f-more").textContent = App.logs.adv ? "收起筛选 ▴" : "更多筛选 ▾";
    };

    // 点整行看详情（列表本身不展示任何大字段）
    $$("#content tr.log-row").forEach((tr) => {
      tr.onclick = () => openLogDetail(tr.getAttribute("data-log"));
    });

    $("#f-settings").onclick = () => openLogSettings(ret);
  }

  /* ---------- 日志详情保留设置（弹窗） ---------- */
  function openLogSettings(ret) {
    const oldest = ret.oldest_detail_at ? `最早 ${fmtDate(ret.oldest_detail_at)}` : "暂无";
    const infoLine = () => `
      记录 <b>${num(ret.total_rows)}</b> 条（永久保留）· 带详情 <b>${num(ret.detail_rows)}</b> 条 · ${esc(oldest)}`;

    modal(`
      <div class="modal-head">
        <h3>日志设置 <span class="sub">· 详情保留与清理</span></h3>
        <button class="link" id="x">✕</button>
      </div>
      <div class="modal-body">
        <div class="section-title">详情保留期</div>
        <div style="display:flex;gap:8px;align-items:center">
          <input id="set-days" type="number" min="0" max="365" value="${ret.days}" style="width:90px" />
          <span class="small muted">天</span>
        </div>
        <div class="hint-box small" style="margin-top:10px">
          详情指「请求输入」与「上游返回」的内容。清理只清空这部分内容，
          日志记录本身（时间、服务、状态、耗时、tokens、费用、失败原因）<b>永久保留，一条都不会删除</b>。
          0 = 不保留详情（新记录的详情也会在下次清理时被清空）。
        </div>
        <div class="small muted" id="set-info" style="margin-top:12px">${infoLine()}</div>
      </div>
      <div class="modal-foot" style="justify-content:space-between">
        <button class="btn-ghost btn-sm" id="set-clean">清空超期详情</button>
        <div style="display:flex;gap:8px">
          <button class="btn-ghost" id="cancel">关闭</button>
          <button class="btn-primary" id="save">保存</button>
        </div>
      </div>`, true);

    $("#modal-root #x").onclick = closeModal;
    $("#modal-root #cancel").onclick = closeModal;

    $("#modal-root #save").onclick = async () => {
      const days = Number($("#modal-root #set-days").value);
      if (!(days >= 0) || days > 365) { toast("保留天数需为 0~365", "err"); return; }
      try {
        const r = await api("/api/admin/logs/retention?days=" + days, "PUT");
        ret.days = r.days;
        ret.detail_rows = r.detail_rows;
        ret.total_rows = r.total_rows;
        ret.oldest_detail_at = r.oldest_detail_at;
        $("#modal-root #set-info").innerHTML = infoLine();
        toast(`已保存：详情保留 ${r.days} 天，记录不受影响`);
      } catch (e) { toast(e.message, "err"); }
    };

    $("#modal-root #set-clean").onclick = async () => {
      if (!confirm(`将清空超过 ${ret.days} 天的日志详情内容（请求输入与上游返回）。\n\n日志记录本身不会被删除，用量统计、耗时、费用、失败原因都保留。继续？`)) return;
      try {
        const r = await api("/api/admin/logs/retention/cleanup", "POST");
        ret.detail_rows = 0;
        ret.oldest_detail_at = null;
        ret.total_rows = r.kept_rows;
        $("#modal-root #set-info").innerHTML = infoLine();
        toast(r.cleared
          ? `已清空 ${r.cleared} 条日志的详情，${num(r.kept_rows)} 条记录全部保留`
          : `没有超过 ${r.days} 天的详情可清空（${num(r.kept_rows)} 条记录未受影响）`);
      } catch (e) { toast(e.message, "err"); }
    };
  }

  /* ---------- 单条日志详情 ---------- */
  function prettyJson(text) {
    if (!text) return "";
    const s = String(text).trim();
    if (!s || (s[0] !== "{" && s[0] !== "[")) return text;
    try { return JSON.stringify(JSON.parse(s), null, 2); } catch (e) { return text; }
  }

  function codeBlock(text, color) {
    if (!text) return `<div class="small muted">（无内容）</div>`;
    return `<pre class="mono small" style="margin:0;white-space:pre-wrap;word-break:break-all;
      max-height:360px;overflow:auto;background:var(--bg2,rgba(0,0,0,.22));padding:10px 12px;
      border-radius:6px;line-height:1.55;color:${color || "var(--fg)"}">${esc(prettyJson(text))}</pre>`;
  }

  async function openLogDetail(id) {
    if (!id) return;
    let d;
    try { d = await api("/api/admin/logs/" + id); }
    catch (e) { toast(e.message, "err"); return; }

    const etLabel = (ERROR_TYPES.find((t) => t[0] === d.error_type) || [null, d.error_type])[1];
    const kv = (k, v) => v || v === 0 ? `<div><span class="muted">${k}</span><div class="mono small">${esc(String(v))}</div></div>` : "";

    modal(`
      <div class="modal-head">
        <h3>调用详情 <span class="sub mono">${esc(d.request_id || d.id)}</span></h3>
        <button class="link" id="x">✕</button>
      </div>
      <div class="modal-body">
        <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px">
          ${d.status === "success" ? '<span class="tag ok">成功</span>' : '<span class="tag err">失败</span>'}
          ${d.error_type ? `<span class="tag err">${esc(etLabel)}</span><span class="muted mono small">${esc(d.error_type)}</span>` : ""}
          ${d.safety_result ? `<span class="muted small">审查</span>${safetyResultTag(d.safety_result)}${d.safety_policy ? `<span class="tag info">${esc(d.safety_policy)}</span>` : ""}` : ""}
          <span class="muted small">${fmtDate(d.created_at)}</span>
          <span class="tag svc">${esc(d.service)}</span>
          <span class="mono small muted">${esc(d.endpoint)}</span>
        </div>

        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px 16px;margin-bottom:16px">
          ${kv("厂商", d.provider_name)}
          ${kv("模型", d.model)}
          ${kv("Key", d.key_name)}
          ${kv("耗时", d.latency_ms + " ms")}
          ${kv("tokens", d.tokens_in + " → " + d.tokens_out)}
          ${kv("费用", money(d.cost))}
          ${kv("网关返回", d.http_status)}
          ${kv("上游状态", d.upstream_status)}
          ${kv("客户端 IP", d.ip)}
        </div>

        ${d.upstream_url ? `<div class="small muted" style="margin-bottom:6px">上游地址</div>
          <div class="mono small" style="word-break:break-all;margin-bottom:14px">${esc(d.upstream_url)}</div>` : ""}

        ${d.error_message ? `<div class="small muted" style="margin-bottom:6px">失败原因</div>
          <div class="small" style="color:var(--err);margin-bottom:14px;word-break:break-word">${esc(d.error_message)}</div>` : ""}

        ${d.safety_detail ? `<div class="small muted" style="margin-bottom:6px">安全审查说明</div>
          <div class="small" style="color:var(--warn);margin-bottom:14px;word-break:break-word">${esc(d.safety_detail)}</div>` : ""}

        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
          <span class="small muted">请求内容</span>
          <button class="link small" data-copy="req">复制</button>
        </div>
        ${codeBlock(d.request_body)}

        <div style="display:flex;justify-content:space-between;align-items:center;margin:16px 0 6px">
          <span class="small muted">上游返回内容</span>
          <button class="link small" data-copy="resp">复制</button>
        </div>
        ${codeBlock(d.response_body || d.upstream_body, "var(--fg)")}

        ${!d.request_body && !d.response_body && !d.upstream_body
          ? `<div class="small muted" style="margin-top:12px">该条记录完好保留（时间、状态、耗时、tokens、费用、失败原因都在），仅请求与返回的详情内容未记录或已按保留策略清空。</div>` : ""}
      </div>
      <div class="modal-foot">
        <button class="btn-ghost" id="close">关闭</button>
      </div>`, true);

    $("#modal-root #x").onclick = closeModal;
    $("#modal-root #close").onclick = closeModal;
    const copy = async (text, btn) => {
      if (!text) { toast("没有可复制的内容"); return; }
      try {
        await navigator.clipboard.writeText(text);
        btn.textContent = "已复制";
        setTimeout(() => { btn.textContent = "复制"; }, 1500);
      } catch (e) { toast("复制失败，请手动选中", "err"); }
    };
    const btnReq = $('#modal-root [data-copy="req"]');
    const btnResp = $('#modal-root [data-copy="resp"]');
    if (btnReq) btnReq.onclick = () => copy(d.request_body || "", btnReq);
    if (btnResp) btnResp.onclick = () => copy(d.response_body || d.upstream_body || "", btnResp);
  }

  /* ===================== 接入说明 ===================== */
  function renderDocs() {
    const origin = location.origin;
    const svcBlocks = App.services.map((s) => {
      const ep = (s.endpoints || []).map((e) => `<tr><td><span class="tag info">${esc(e.method)}</span></td><td class="mono">${esc(e.path)}</td><td class="small muted">${esc(e.summary)}</td></tr>`).join("");
      let example = "";
      if (s.key === "ocr") {
        example = `curl -X POST ${origin}/v1/ocr/recognize \\
  -H "Authorization: Bearer $SG_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"image_base64":"<BASE64>","engine":"general"}'`;
      } else if (s.key === "llm") {
        example = `# OpenAI 兼容
curl ${origin}/v1/chat/completions \\
  -H "Authorization: Bearer $SG_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"你好"}]}'

# Anthropic 兼容
curl ${origin}/v1/messages \\
  -H "Authorization: Bearer $SG_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"claude-3-5-sonnet","max_tokens":1024,"messages":[{"role":"user","content":"你好"}]}'`;
      }
      return `<div class="card" style="margin-top:16px">
        <h3>${esc(s.label)} <span class="tag svc">${esc(s.key)}</span></h3>
        <p class="small muted" style="line-height:1.7;margin:0 0 12px">${esc(s.description)}</p>
        <div class="table-wrap" style="margin-bottom:14px">
          <table><thead><tr><th>方法</th><th>路径</th><th>说明</th></tr></thead><tbody>${ep}</tbody></table>
        </div>
        <div class="copy-line"><span class="small muted">调用示例（Base URL 即本网关地址）</span></div>
        <pre class="code"><code>${esc(example)}</code></pre>
      </div>`;
    }).join("");

    const ocrDemos = App.services.some((s) => s.key === "ocr") ? `
      <div class="card" style="margin-top:16px">
        <h3>OCR 调用 Demo</h3>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <a class="btn-ghost btn-sm" href="/admin/demo/demo_python.py" download>Python Demo</a>
          <a class="btn-ghost btn-sm" href="/admin/demo/demo_curl.sh" download>curl Demo</a>
          <a class="btn-ghost btn-sm" href="/admin/demo/demo_node.js" download>Node.js Demo</a>
          <a class="btn-ghost btn-sm" href="/admin/demo/USAGE.md" download>使用说明（MD）</a>
        </div>
      </div>` : "";

    $("#content").innerHTML = `
      <div class="page-head">
        <div><h2>接入说明</h2><p>统一入口：<span class="mono">${esc(origin)}</span> · 鉴权：<span class="mono">Authorization: Bearer &lt;Key&gt;</span></p></div>
      </div>
      ${svcBlocks}
      ${ocrDemos}
      <div class="card" style="margin-top:16px">
        <h3>如何接入新的中转服务</h3>
        <p class="small muted" style="line-height:1.85;margin:0 0 10px">
          网关内置「服务注册表」。新增 ASR、翻译、向量化等服务时，只需新建
          <span class="mono">app/services/&lt;key&gt;/service.py</span>，在其中注册
          <span class="mono">ServiceSpec</span>（服务名、对外路由、厂商表单元信息、配置即测钩子），
          并实现自己的 <span class="mono">router</span>。启动时会自动发现并装配：
        </p>
        <pre class="code"><code>register_service(ServiceSpec(
    key="asr",
    label="语音识别",
    description="统一对接云端 ASR 与私有化服务",
    endpoints=[{"method": "POST", "path": "/v1/asr/transcribe", "summary": "语音转写"}],
    provider_types=[ProviderTypeSpec(
        type="generic",
        label="通用 HTTP ASR",
        fields=[{"name": "api_key", "label": "API Key", "secret": True, "required": False}],
        setting_fields=[{"name": "url", "label": "服务地址"}],
    )],
    router=router,
    test_provider=test_provider,
))</code></pre>
        <p class="small muted" style="line-height:1.85;margin:10px 0 0">
          完成后：管理后台自动出现该服务标签页与动态凭证表单、API Key 可勾选该服务、
          调用日志与仪表盘自动带 service 维度 —— 不需要改数据库、不需要改前端。
        </p>
      </div>`;
  }

  /* ===================== 入口 ===================== */
  boot().catch((e) => {
    $("#login-view").classList.remove("hidden");
    $("#login-error").textContent = e.message;
  });
})();
