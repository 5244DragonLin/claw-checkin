/* ClawCheckin · 龙虾签到台 —— 前端逻辑（无框架、无构建） */
"use strict";

const $ = (sel) => document.querySelector(sel);
const PLATFORM_ICON = {
  workbuddy: "/static/icons/workbuddy.svg",
  autoclaw: "/static/icons/autoclaw.png",
};
const DISPLAY_NAMES = {
  workbuddy_credits: "WorkBuddy 积分",
  autoclaw_credits: "AutoClaw 积分",
};
const displayName = (name) => DISPLAY_NAMES[name] || name;
const iconFor = (name) => {
  const n = name.toLowerCase();
  for (const [k, url] of Object.entries(PLATFORM_ICON)) {
    if (n.includes(k)) return url;
  }
  // 无对应图标时，生成首字母 SVG
  const ch = name.charAt(0).toUpperCase();
  return `data:image/svg+xml,${encodeURIComponent('<svg viewBox="0 0 30 30" xmlns="http://www.w3.org/2000/svg"><rect width="30" height="30" rx="9" fill="#f3f3f1"/><text x="15" y="21" text-anchor="middle" font-size="18" font-weight="700" fill="#6f6f68" font-family="sans-serif">' + ch + '</text></svg>')}`;
};

let logFilter = "all";
let logPage = 1;
const LOG_PAGE_SIZE = 10;

/* ---------- 页面切换 ---------- */
const PAGES = ["overview", "platforms", "calendar", "logs"];
const currentPage = () => {
  const p = document.querySelector(".page.active");
  return p ? p.id.replace("page-", "") : "";
};
// 当前页写进 URL hash（replaceState 不产生历史记录），刷新浏览器时按 hash 还原，不再回到总览
function switchPage(name, syncHash = true) {
  if (!PAGES.includes(name)) name = "overview";
  document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
  $(`#page-${name}`).classList.add("active");
  document.querySelectorAll("#nav a").forEach((a) =>
    a.classList.toggle("active", a.dataset.page === name));
  if (syncHash && location.hash !== `#${name}`) {
    history.replaceState(null, "", `#${name}`);
  }
  if (name === "overview") loadOverview();
  if (name === "platforms") loadPlatforms();
  if (name === "calendar") loadCalendar();
  if (name === "logs") loadLogs();
}
document.querySelectorAll("#nav a").forEach((a) =>
  a.addEventListener("click", (ev) => {
    ev.preventDefault();           // 纯前端切换：不走链接跳转、不做整页刷新
    switchPage(a.dataset.page);
  }));
window.addEventListener("hashchange", () => {
  const name = location.hash.slice(1);
  if (name && name !== currentPage()) switchPage(name, false);
});

/* ---------- 工具 ---------- */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function toast(title, msg, type) {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.innerHTML = `<b>${esc(title)}</b><small>${esc(msg || "")}</small>`;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 5000);
}
async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
    throw new Error(detail);
  }
  return res.json();
}
function badge(status, todayOk) {
  if (todayOk) return '<span class="badge ok">已签到</span>';
  if (status === "PENDING" || !status) return '<span class="badge pending">未签到</span>';
  if (status === "CLAIMED" || status === "ALREADY") return '<span class="badge ok">已签到</span>';
  if (status === "NO_AUTH") return '<span class="badge warn">待验证</span>';
  return '<span class="badge fail">签到失败</span>';
}
function fmtNum(n) { return (n ?? 0).toLocaleString("en-US"); }
function skeletonCards(n, h) {
  return Array.from({ length: n }, () =>
    `<div class="skeleton" style="height:${h}px"></div>`).join("");
}

/* ---------- 签到总览 ---------- */
async function loadOverview() {
  $("#ov-body").style.display = "none";
  $("#ov-error").innerHTML = "";
  $("#ov-sub").textContent = "加载中…";
  try {
    const data = await api("/api/overview");
    renderOverview(data);
    $("#ov-body").style.display = "";
  } catch (e) {
    $("#ov-error").innerHTML = `<div class="error-banner"><b>数据加载失败</b>
      <small>${esc(e.message)} · 面板服务可能未启动，请检查 python main.py</small></div>`;
    $("#ov-sub").textContent = "加载失败";
  }
}

function renderOverview(d) {
  if (!d || !d.platforms) {
    $("#ov-body").style.display = "";
    $("#ov-platforms").innerHTML = emptyBlock("平台数据异常", "请刷新重试");
    return;
  }
  $("#ov-sub").textContent =
    `${d.platform_count} 个平台已接入自动签到，${d.pending_count} 个待处理`;
  $("#ov-date").textContent = `${d.today} ${d.weekday}`;
  $("#ov-schedule").style.display = "";
  if (d.schedule_enabled === false) {
    $("#ov-schedule").className = "badge gray";
    $("#ov-schedule").textContent = "● 自动签到已关闭 · 见配置 schedule.enabled";
  } else {
    $("#ov-schedule").className = "badge ok";
    $("#ov-schedule").textContent = `● 自动签到已开启 · 每日 ${d.schedule_time || "见配置 schedule.time"}`;
  }

  const pend = d.platforms.filter((p) => !p.today_ok).slice(0, 2).map((p) => p.name);
  $("#ov-stats").innerHTML = `
    <div class="stat-card stat-yellow"><div class="label">今日签到进度</div>
      <div class="value">${d.done_count} / ${d.platform_count}</div>
      <div class="note">${d.pending_count ? esc(pend.join(" · ")) + " 待处理" : "全部完成 🎉"}</div></div>
    <div class="stat-card stat-pink"><div class="label">累计积分总数</div>
      <div class="value">${fmtNum(d.total_credits)}</div>
      <div class="note">${d.platform_count} 个平台合计</div></div>
    <div class="stat-card stat-orange"><div class="label">14 天内将过期</div>
      <div class="value">${fmtNum(d.expiring_14d)}</div>
      <div class="note">${d.expiring_covered} / ${d.platform_count} 平台返回有效期${d.expiring_covered < d.platform_count ? " · 其余未提供" : ""}</div></div>
    <div class="stat-card stat-teal"><div class="label">最长连续签到</div>
      <div class="value">${d.max_streak} 天</div>
      <div class="note">保持中 🔥</div></div>`;

  $("#ov-platforms").innerHTML = d.platforms.map((p) => `
    <div class="platform-card ${p.today_ok ? "" : p.status.startsWith("NO") || p.status === "ERROR" || p.status === "NETWORK" ? "failed" : ""}">
      <div class="pc-head">
        <div class="pc-name"><span class="pc-icon"><img src="${iconFor(p.name)}" width="30" height="30" alt=""></span>${esc(p.name)}</div>
        ${badge(p.status, p.today_ok)}
      </div>
      <div class="pc-credits">${fmtNum(p.credits)}</div>
      <div class="pc-meta">
        当前累计积分<br>
        连续 <b>${p.streak}</b> 天${p.has_expiring
          ? (p.earliest_expiring ? ` · 最早 <b>${p.earliest_expiring.slice(5)}</b> 到期` : "") + (p.expiring_14d ? ` · 14 天内 <b>${fmtNum(p.expiring_14d)}</b> 分到期` : "")
          : " · 平台未返回有效期"}
      </div>
      <button class="pc-btn ${p.today_ok ? "done" : "primary"}" data-run="${esc(p.name)}" ${p.today_ok ? "disabled" : ""}>
        ${p.today_ok ? "今日已完成" : "立即签到"}
      </button>
    </div>`).join("") || emptyBlock("还没有接入任何平台", "把签到脚本放进 scripts/ 目录，扫描后会自动出现在这里", "scanNow()", "扫描目录");

  bindRunButtons();

  // 七天临期明细表
  if (!d.expiring_detail.length) {
    $("#ov-exp-title").style.display = "none";
    $("#ov-exp").innerHTML = "";
    return;
  }
  $("#ov-exp-title").style.display = "";
  $("#ov-exp-title").innerHTML =
    `未来 14 天即将过期积分 · ${d.expiring_covered} / ${d.platform_count} 个平台提供数据` +
    ` <a style="float:right;font-size:12.5px;color:#4a6bdc;cursor:pointer" onclick="switchPage('calendar')">查看完整过期日历</a>`;
  const rows = d.expiring_detail.map((e) => {
    const left = e.days_left <= 0 ? '<span class="urgent-red">今天到期</span>'
      : e.days_left <= 2 ? `<span class="urgent-red">剩 ${e.days_left} 天</span>`
      : `<span class="${e.days_left <= 4 ? "urgent-orange" : "muted"}">剩 ${e.days_left} 天</span>`;
    return `<tr><td>${e.date.slice(5)}</td><td>${esc(e.platform)}</td>
      <td class="amount">${fmtNum(e.amount)}</td><td class="muted">${esc(e.source)}</td><td>${left}</td></tr>`;
  }).join("");
  $("#ov-exp").innerHTML = `<div class="table-wrap"><table>
    <thead><tr><th>过期日期</th><th>平台</th><th>即将过期</th><th>积分来源</th><th>剩余时间</th></tr></thead>
    <tbody>${rows}</tbody></table>
    <div class="table-foot"><span>14 天合计将过期</span><b>${fmtNum(d.expiring_14d)}</b></div></div>`;
}

function emptyBlock(title, desc, onclick, btn) {
  return `<div class="empty-state" style="grid-column:1/-1">
    <div class="ico">🗂️</div><b>${title}</b><p>${desc}</p>
    ${btn ? `<button class="btn-dark" onclick="${onclick}">${btn}</button>` : ""}</div>`;
}

/* ---------- 签到动作 ---------- */
function bindRunButtons() {
  document.querySelectorAll("[data-run]").forEach((btn) =>
    btn.addEventListener("click", () => runOne(btn.dataset.run, btn)));
}
async function runOne(name, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "签到中…"; }
  try {
    const r = await api(`/api/run/${encodeURIComponent(name)}`, { method: "POST" });
    if (r.result === "CLAIMED")
      toast(`${name} 签到成功`, `${r.report || "完成"} · 连续签到重新计数见明细`, "ok");
    else if (r.result === "ALREADY")
      toast(`${name} 今日已签`, r.report || "已签到，跳过", "ok");
    else
      toast(`${name} 签到失败`, r.report || r.result, "fail");
    loadOverview();
  } catch (e) {
    toast(`${name} 签到失败`, e.message, "fail");
    if (btn) { btn.disabled = false; btn.textContent = "立即签到"; }
  }
}
$("#btn-run-all").addEventListener("click", async (ev) => {
  const btn = ev.currentTarget;
  btn.disabled = true; btn.textContent = "签到中…";
  try {
    const r = await api("/api/run_all", { method: "POST" });
    const ok = r.results.filter((x) => x.ok).length;
    const fail = r.results.filter((x) => !x.ok);
    // 隐藏的积分源（如 workbuddy_credits）跑失败时，卡片上的总积分会是旧值
    const credFail = (r.credit_sources || []).filter((x) => !x.ok);
    const credNote = credFail.length
      ? `；积分刷新失败：${credFail.map((c) => c.source_for || c.platform).join("、")}`
      : "";
    toast(`一键签到完成`, `${ok} / ${r.ran} 个平台成功` +
      (fail.length ? `，失败：${fail.map((f) => f.platform).join("、")}` : "") + credNote,
      fail.length || credFail.length ? "fail" : "ok");
    loadOverview();
  } catch (e) { toast("一键签到失败", e.message, "fail"); }
  btn.disabled = false; btn.textContent = "一键全部签到";
});
function scanNow() {
  switchPage("platforms");
  doScan();
}

/* ---------- 平台接入 ---------- */
async function loadPlatforms() {
  $("#pf-sub").textContent = "扫描中…";
  $("#pf-cards").innerHTML = skeletonCards(4, 150);
  try {
    await doScan(false);
  } catch (e) {
    $("#pf-sub").textContent = "扫描失败";
    $("#pf-cards").innerHTML = `<div class="error-banner" style="grid-column:1/-1"><b>脚本目录扫描失败</b><small>${esc(e.message)}</small></div>`;
  }
}
async function doScan(withHealthToast = true) {
  const r = await api("/api/scan", { method: "POST" });
  const passed = r.platforms.filter((p) => p.health !== undefined).length;
  $("#pf-sub").textContent =
    `${"scripts"} 目录自动发现 ${r.found} 个脚本 · ${passed} 个通过契约校验 · ${r.found - passed} 个待验证`;
  renderPlatformCards(r.platforms);
  await renderChecklist(r.platforms);
  if (withHealthToast) toast("扫描完成", `发现 ${r.found} 个脚本`, "ok");
}
function renderPlatformCards(platforms) {
  if (!platforms || !platforms.length) {
    $("#pf-cards").innerHTML = emptyBlock("还没有接入任何平台", "把签到脚本放进 scripts/ 目录，扫描后会自动出现在这里", "doScan()", "扫描目录");
    return;
  }
  $("#pf-cards").innerHTML = platforms.map((p) => {
    const h = p.health;
    const healthBadge = !h
      ? '<span class="badge gray">待体检</span>'
      : h.overall === "已校验" ? '<span class="badge ok">已校验</span>'
      : h.overall === "未通过" ? '<span class="badge fail">未通过</span>'
      : '<span class="badge warn">待验证</span>';
    return `<div class="platform-card ${h && h.overall === "未通过" ? "failed" : ""}">
      <div class="pc-head">
        <div class="pc-name"><span class="pc-icon"><img src="${iconFor(p.name)}" width="30" height="30" alt=""></span>${esc(p.name)}</div>
        ${healthBadge}
      </div>
      <div class="pc-meta">凭据<br><span class="exec-tag">脚本自带 · 面板不管理</span></div>
      <div class="pc-meta">执行器<br><span class="exec-tag">${esc(p.executor_label)}</span></div>
      <div class="pc-actions">
        <a data-refresh="${esc(p.name)}" style="color:#4a6bdc">刷新凭证</a>
        <a data-try="${esc(p.name)}">立即试跑</a>
        <a onclick="switchPage('logs')">查看日志</a>
      </div>
    </div>`;
  }).join("") || emptyBlock("还没有接入任何平台", "把签到脚本放进 scripts/ 目录，扫描后会自动出现在这里", "doScan()", "扫描目录");

  document.querySelectorAll("[data-try]").forEach((a) =>
    a.addEventListener("click", () => tryHealth(a.dataset.try)));
  document.querySelectorAll("[data-refresh]").forEach((a) =>
    a.addEventListener("click", () => refreshToken(a.dataset.refresh)));
}
async function renderChecklist(platforms) {
  if (!platforms.length) { $("#pf-checks").innerHTML = ""; return; }
  // 汇总展示首个已体检平台的体检单；未体检的先体检
  let target = platforms.find((p) => p.health);
  if (!target) {
    try { target = await api(`/api/health/${encodeURIComponent(platforms[0].name)}`, { method: "POST" }); }
    catch (e) { $("#pf-checks").innerHTML = ""; return; }
  }
  renderChecksInto($("#pf-checks"), target, false);
}
function refreshToken(name) {
  toast("刷新凭证", `${name} 正在刷新凭证…`, "ok");
  fetch(`/api/refresh/${encodeURIComponent(name)}`, { method: "POST" })
    .then(r => r.json())
    .then(d => {
      toast(name + " 凭证", d.report || "刷新完成", d.ok ? "ok" : "fail");
      doScan(false);
    })
    .catch(e => toast(name + " 凭证刷新失败", e.message, "fail"));
}

async function tryHealth(name) {
  toast("体检中", `${name} 正在双跑校验（约需数秒）…`, "ok");
  try {
    const h = await api(`/api/health/${encodeURIComponent(name)}`, { method: "POST" });
    renderChecksInto($("#pf-checks"), h, true);
    $("#modal-title").textContent = `${name} · 接入体检`;
    $("#modal-sub").textContent = `总体：${h.overall}`;
    renderChecksInto($("#modal-checks"), h, false);
    $("#modal-mask").classList.add("show");
    doScan(false);
  } catch (e) { toast("体检失败", e.message, "fail"); }
}
function renderChecksInto(el, h, withPlatform) {
  if (!h || !h.checks || !h.checks.length) { el.innerHTML = ""; return; }
  el.innerHTML = h.checks.map((c) => {
    const cls = c.status === "pass" ? "pass" : c.status === "fail" ? "fail" : "skip";
    const label = c.status === "pass" ? "通过" : c.status === "fail" ? "未通过" : "待验证";
    return `<div class="check-row">
      <span class="check-status ${cls}">${label}</span>
      <span class="check-name">${esc(c.name)}</span>
      <span class="check-desc">${esc(c.desc)}${c.detail ? ` · ${esc(c.detail)}` : ""}</span>
    </div>`;
  }).join("");
}
$("#btn-rescan").addEventListener("click", () => doScan().catch((e) => toast("扫描失败", e.message, "fail")));

/* ---------- 过期日历 ---------- */
async function loadCalendar() {
  $("#cal-sub").textContent = "加载中…";
  $("#cal-grid").innerHTML = skeletonCards(28, 96);
  $("#cal-weeks").innerHTML = skeletonCards(3, 90);
  try {
    const d = await api("/api/expiring");
    const ov = await api("/api/overview");
    const total28 = d.weeks.reduce((a, b) => a + b, 0);
    $("#cal-sub").textContent =
      `未来 28 天共 ${d.days.filter((x) => x.amount > 0).length} 笔积分将过期 · 合计 ${fmtNum(total28)} 分` +
      (ov ? ` · 数据来自 ${ov.expiring_covered} / ${ov.platform_count} 个平台` : "");
    const weekLabels = ["第 1 周", "第 2 周", "第 3 周", "第 4 周"];
    const weekStyles = ["stat-yellow", "stat-pink", "stat-orange", "stat-teal"];
    $("#cal-weeks").innerHTML =
      d.weeks.map((w, i) =>
        `<div class="week-card ${weekStyles[i]}"><div class="label">${weekLabels[i]}</div><div class="value">${fmtNum(w)} 分</div></div>`
      ).join("") +
      `<div class="week-card stat-gray"><div class="label">过去 30 天已作废</div><div class="value">${fmtNum(d.void30)} 分</div></div>`;
    const noExpiring = total28 + d.void30 === 0 && d.days.every((x) => !x.items.length);
    if (noExpiring) {
      $("#cal-tip").style.display = "";
      $("#cal-tip").textContent = "暂无平台返回积分有效期：有平台支持时，脚本补上 expiring 字段即可自动纳入";
    } else {
      $("#cal-tip").style.display = "none";
    }
    $("#cal-grid").innerHTML = d.days.map((day) => {
      if (!day.amount) {
        return `<div class="cal-cell empty"><div class="d">${day.date.slice(5)} · ${day.weekday}</div>
          <div class="p">无到期积分</div><div class="v">—</div></div>`;
      }
      const days = Math.round((new Date(day.date) - new Date(new Date().toDateString())) / 86400000);
      const cls = days <= 2 ? "cal-red" : days <= 4 ? "cal-orange" : "cal-teal";
      const plats = [...new Set(day.items.map((i) => i.platform))].join("、");
      return `<div class="cal-cell ${cls}"><div class="d">${day.date.slice(5)} · ${day.weekday}</div>
        <div class="p">${esc(plats)}</div><div class="v">${fmtNum(day.amount)}</div></div>`;
    }).join("");
  } catch (e) {
    $("#cal-sub").textContent = "加载失败";
    $("#cal-weeks").innerHTML = `<div class="error-banner" style="grid-column:1/-1"><b>数据拉取失败</b><small>${esc(e.message)}，已保留上次缓存数据</small>
      <button class="btn-dark" style="margin-top:10px" onclick="loadCalendar()">重新加载</button></div>`;
    $("#cal-grid").innerHTML = "";
  }
}

/* ---------- 运行日志 ---------- */
document.querySelectorAll("#log-chips .chip").forEach((chip) =>
  chip.addEventListener("click", () => {
    document.querySelectorAll("#log-chips .chip").forEach((c) => c.classList.remove("active"));
    chip.classList.add("active");
    logFilter = chip.dataset.f;
    logPage = 1;
    loadLogs();
  }));
function pagerHtml(page, totalPages, total) {
  if (totalPages <= 1) return "";
  const btn = (p, label, disabled) =>
    `<button class="pager-btn" ${disabled ? "disabled" : ""} onclick="goLogPage(${p})">${label}</button>`;
  return `<div class="pager">
    ${btn(page - 1, "‹ 上一页", page <= 1)}
    <span class="pager-info">第 ${page} / ${totalPages} 页 · 共 ${total} 条</span>
    ${btn(page + 1, "下一页 ›", page >= totalPages)}
  </div>`;
}
function goLogPage(p) {
  if (p < 1) return;
  loadLogs(p);
}
async function loadLogs(page = logPage) {
  logPage = page;
  $("#log-sub").textContent = "加载中…";
  $("#log-body").innerHTML = `<div class="table-wrap">${skeletonCards(LOG_PAGE_SIZE, 40)}</div>`;
  try {
    const offset = (page - 1) * LOG_PAGE_SIZE;
    const d = await api(`/api/logs?filter_name=${logFilter}&limit=${LOG_PAGE_SIZE}&offset=${offset}`);
    const totalPages = Math.max(1, Math.ceil(d.total / LOG_PAGE_SIZE));
    // 从全量日志统计今日数字（简版：用当前返回展示条数）
    $("#log-sub").textContent = "数据来源：各脚本 stdout 的一行 JSON + data/ledger.db 的 checkin_log 表 · 本地保留 90 天";
    if (!d.logs.length) {
      if (page > 1) { loadLogs(1); return; }
      if (logFilter === "all") {
        $("#log-body").innerHTML = emptyBlock("今天还没有签到记录",
          "调度器将在每日配置时刻触发，也可以现在手动跑一次", "runAllFromLogs()", "立即执行");
      } else {
        $("#log-body").innerHTML = emptyBlock("没有匹配的日志",
          logFilter === "failed" ? "当前筛选下没有失败记录" : "当前筛选下暂无记录");
      }
      return;
    }
    const rows = d.logs.map((l, i) => {
      const t = l.trigger === "schedule" ? "定时" : l.trigger === "health" ? "体检" : l.trigger === "refresh" ? "积分" : "手动";
      const credit = l.credit ? `+${l.credit}` : "—";
      const cls = (l.result || "").replace(/[^A-Z_]/g, "");
      return `<tr class="clickable" data-raw="${esc(l.raw || "")}">
        <td class="muted" style="white-space:nowrap">${esc(l.ts.replace("T", " ").slice(5))}</td>
        <td><b>${esc(displayName(l.platform))}</b></td>
        <td class="muted">${esc(l.script)}</td>
        <td><span class="result-tag ${cls}">${esc(l.result)}</span></td>
        <td>${credit === "—" ? '<span class="muted">—</span>' : `<b style="color:#2e7d43">${credit}</b>`}</td>
        <td class="muted" style="max-width:320px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(l.report || "")}</td>
        <td class="muted">${t}</td></tr>`;
    }).join("");
    $("#log-body").innerHTML = `<div class="table-wrap"><table>
      <thead><tr><th>时间</th><th>平台</th><th>脚本</th><th>状态</th><th>积分</th><th>返回明细</th><th>触发</th></tr></thead>
      <tbody>${rows}</tbody></table></div>${pagerHtml(page, totalPages, d.total)}`;
    document.querySelectorAll("#log-body tr.clickable").forEach((tr) =>
      tr.addEventListener("click", () => {
        const raw = tr.dataset.raw;
        const block = $("#log-raw");
        block.style.display = "block";
        block.innerHTML = `<div class="hint">查看本行原始返回</div>${esc(
          raw || "（无 stdout 返回：执行层失败，详见返回明细列）")}`;
      }));
  } catch (e) {
    $("#log-body").innerHTML = `<div class="error-banner"><b>数据拉取失败</b><small>${esc(e.message)}</small></div>`;
  }
}
function runAllFromLogs() {
  $("#btn-run-all").click();
  switchPage("overview");
}

/* ---------- 启动 ---------- */
// 带 #logs 之类 hash 打开（或刷新）时直接还原到那一页，默认总览
switchPage(PAGES.includes(location.hash.slice(1)) ? location.hash.slice(1) : "overview", false);
// 启动后积分源在后台补跑，10 秒后重拉一次总览让卡片余额跟上
setTimeout(() => { if (currentPage() === "overview") loadOverview(); }, 10000);
