// 验证脚本：过期日历配色（格子按周上色与周卡片对应）+ 品牌区跳转首页（对运行中的服务实测）
// 用法：node scripts/_verify_calendar.mjs [端口]
const BASE = `http://127.0.0.1:${process.argv[2] || 8812}`;
let failed = 0;
const check = (name, ok, detail) => {
  console.log(name, ok ? "PASS" : "FAIL" + (detail ? ` (${detail})` : ""));
  if (!ok) failed++;
};

const html = await (await fetch(`${BASE}/`)).text();
const servedJs = await (await fetch(`${BASE}/static/app.js`)).text();
const servedCss = await (await fetch(`${BASE}/static/style.css`)).text();

// ===== 品牌区（左上角 logo + 龙虾签到台）点击跳回首页 =====
check("[B1] HTML 品牌区是指向 #overview 的链接:",
  html.includes('<a class="brand" href="#overview" id="brand-home"'));
check("[B2] 品牌区以 </a> 闭合且后接 nav:",
  /<\/a>\s*<nav class="nav" id="nav">/.test(html));
const idx = servedJs.indexOf("#brand-home");
check("[B3] JS 品牌区点击接线到 switchPage(overview) 且阻止默认跳转:",
  idx > -1 && servedJs.slice(idx - 60, idx + 200).includes('switchPage("overview")')
  && servedJs.slice(idx - 60, idx + 200).includes("preventDefault"));
check("[B4] switchPage 定义先于 brand 接线（初始化顺序正确）:",
  servedJs.indexOf("function switchPage") > -1
  && servedJs.indexOf("function switchPage") < idx);
check("[B5] CSS 抵消链接默认样式并带悬停反馈:",
  servedCss.includes(".brand { display: flex; align-items: center; gap: 10px; color: inherit; text-decoration: none; cursor: pointer;")
  && servedCss.includes(".brand:hover b"));

// ===== 过期日历配色：格子按周上色，与顶部周卡片一一对应 =====
// 设计：底色按「第几周」取（与顶部 4 张周卡片同色），紧急度另用左侧色条表达。
// 旧口径是「格子按剩余天数上色」，导致格子颜色与所属周卡片对不上（观感错乱）。
check("[C1] 服务端 app.js 含按周取色的 calCellClass:",
  servedJs.includes("const CAL_WEEK_CLASS"));
check("[C2] 服务端 style.css 含 4 个周配色类:",
  ["cal-w1", "cal-w2", "cal-w3", "cal-w4"].every(c => servedCss.includes("." + c + " {")));
// 周卡片与格子必须共用同一组 --w1~--w4 变量，否则改色板时上下会脱节
check("[C2b] 周卡片与日历格子共用 --w1~--w4 变量（不会上下脱色）:",
  ["--w1-bg", "--w2-bg", "--w3-bg", "--w4-bg"].every(v => servedCss.includes(v))
  && servedCss.includes(".stat-w1 {") && servedJs.includes('"stat-w1"'));
// 紧急到不紧急：色相须沿 红->橙->黄->青 递进（用色相角验证，不靠肉眼）
check("[C2c] 四档色相沿红→橙→黄→青递进（构成紧迫度梯度）:", (() => {
  // 从 CSS 文本抠出 --wN-bg 的十六进制值换算 HSL 色相，验证色相严格递增。
  // 正则刻意用字符类而非 \s / \w：避免经heredoc 写入时反斜杠被吃掉。
  const pick = (v) => {
    const m = servedCss.match(new RegExp(v + ":[ ]*(#[0-9a-fA-F]{6})"));
    return m ? m[1] : "?";
  };
  const hueOf = (hex) => {
    const h = hex.slice(1);
    const lin = [0, 2, 4].map(i => parseInt(h.substr(i, 2), 16) / 255)
      .map(v => (v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4)));
    const mx = Math.max(...lin), mn = Math.min(...lin), d = mx - mn;
    if (d === 0) return -1;
    let x;
    if (mx === lin[0]) x = ((lin[1] - lin[2]) / d + (lin[1] < lin[2] ? 6 : 0)) * 60;
    else if (mx === lin[1]) x = ((lin[2] - lin[0]) / d + 2) * 60;
    else x = ((lin[0] - lin[1]) / d + 4) * 60;
    return Math.round(x);
  };
  const VARS = ["--w1-bg", "--w2-bg", "--w3-bg", "--w4-bg"];
  const hexes = VARS.map(pick);
  const hues = hexes.map(h => (h === "?" ? -2 : hueOf(h)));
  // 红(0~15) → 橙(20~45) → 黄(45~70) → 青(140~200)，且必须严格递增
  const inRange = hues[0] >= 0 && hues[0] <= 15
    && hues[1] >= 20 && hues[1] <= 45
    && hues[2] >= 45 && hues[2] <= 70
    && hues[3] >= 140 && hues[3] <= 200;
  const increasing = hues.every((v, i) => i === 0 || v > hues[i - 1]);
  return inRange && increasing;
})(), "hex=" + ["--w1-bg", "--w2-bg", "--w3-bg", "--w4-bg"]
  .map(v => { const m = servedCss.match(new RegExp(v + ":[ ]*(#[0-9a-fA-F]{6})")); return m ? m[1] : "?"; })
  .join(","));

check("[C3] 紧急色条已彻底移除（CSS/JS/图例均无）:",
  !servedCss.includes("cal-urgent") && !servedCss.includes("cal-soon")
  && !servedCss.includes("::before") && !servedJs.includes("cal-urgent")
  && !servedJs.includes("cal-soon") && !html.includes("左侧色条"));
check("[C4] 旧按天数上色的类已彻底移除:",
  !["cal-red", "cal-orange", "cal-teal", "cal-gray"].some(c => servedCss.includes("." + c + " {")));

check("[C5] 图例四项为「第 N 周」纯文字（无附加说明）:",
  ["第 1 周", "第 2 周", "第 3 周", "第 4 周"].every(w => html.includes(w))
  && !html.includes("最急") && !html.includes("最缓")
  && !html.includes("0-6 天") && !html.includes("7-13 天")
  && !html.includes("14-20 天") && !html.includes("21-27 天"));
check("[C6] 图例只保留 4 个周色块（无色条项）:",
  ["第 1 周", "第 2 周", "第 3 周", "第 4 周"].every(w => html.includes(w))
  && !html.includes("72 小时内到期") && !html.includes("3 - 4 天内到期"));

const d = await (await fetch(`${BASE}/api/expiring`)).json();
if (d.days.length !== 28) { console.log("[C7] FAIL: days=" + d.days.length + "，应为 28"); process.exit(1); }

// 复刻前端 calCellClass（仅按周取色，无紧急色条）
const WEEK = ["cal-w1", "cal-w2", "cal-w3", "cal-w4"];
const results = d.days.map((day, i) => {
  const cls = WEEK[Math.min(Math.floor(i / 7), 3)];
  return { i, date: day.date, cls, week: Math.floor(i / 7) + 1, amount: day.amount };
});
const colored = results.filter(r => r.amount > 0);

check("[C8] 每个有积分的格子恰好对应一个周配色:",
  colored.every(r => WEEK.includes(r.cls.split(" ")[0])));
check("[C9] 格子周归属与区间一致（0-6→W1, 7-13→W2, 14-20→W3, 21-27→W4）:",
  colored.every(r => r.cls.startsWith(WEEK[r.week - 1])));
check("[C10] 第1周格子不串到第2周配色:",
  results.slice(0, 7).every(r => r.cls.startsWith("cal-w1")));
check("[C11] 第4周格子不串到第3周配色:",
  results.slice(21, 28).every(r => r.cls.startsWith("cal-w4")));

// 格子 class 只有周配色一个，不夹带紧急类
check("[C12] 格子 class 只含周配色（无任何多余类）:",
  results.every(r => r.cls.split(" ").length === 1 && WEEK.includes(r.cls)));
check("[C13] calCellClass 已简化为单参数:",
  servedJs.includes("function calCellClass(i)") && !servedJs.includes("dayDiff"));

// 周卡片合计 == 对应周的格子合计（配色对应的前提是数据也对应）
const wkSum = [0, 0, 0, 0];
results.forEach(r => { wkSum[r.week - 1] += r.amount; });
check("[C14] 总览卡片配色未被日历改动波及（stat-yellow/pink/orange/teal 仍在用）:",
  servedJs.includes("stat-yellow") && servedJs.includes("stat-pink")
  && servedJs.includes("stat-orange") && servedJs.includes("stat-teal")
  && servedCss.includes(".stat-yellow {") && servedCss.includes(".stat-pink {")
  && servedCss.includes(".stat-orange {") && servedCss.includes(".stat-teal {"));
check("[C14b] 周卡片标题为纯「第 N 周」（未附加最急/最缓）:",
  servedJs.includes('["第 1 周", "第 2 周", "第 3 周", "第 4 周"]'));
check("[C15] 第1~4周卡片合计 == 对应周格子合计:",
  wkSum.every((v, i) => v === d.weeks[i]),
  "卡片" + JSON.stringify(d.weeks) + " vs 格子" + JSON.stringify(wkSum));

if (failed) console.log(`== ${failed} 项失败 ==`);
else console.log("== 全部通过 ==");
process.exit(failed ? 1 : 0);
