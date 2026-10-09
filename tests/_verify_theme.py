# 暗色模式验证：对比度 + 主题切换行为
# 暗色模式最常见的翻车是「字太暗看不清」。这里用 WCAG 相对亮度公式
# 量化判断，不靠肉眼。配对表写在 JS 里，避免 Python->JS 跨语言传参被丢成 None。
import sys

from playwright.sync_api import sync_playwright

# WCAG 相对亮度与对比度
def lum(rgb):
    def ch(v):
        v = v / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = [ch(x) for x in rgb]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(c1, c2):
    l1, l2 = lum(c1), lum(c2)
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def parse(s):
    return [float(x.strip()) for x in s[s.find("(") + 1:s.find(")")].split(",")[:3]]


# 在页面里跑：量每对「前景 / 实际不透明背景」的对比度
PROBE = """() => {
  var pairs = [
    ["body", "正文 / 页面底"],
    [".page-sub", "页面副标题 / 页面底"],
    [".stat-card .label", "统计卡标签 / 卡底"],
    [".stat-card .value", "统计卡数值 / 卡底"],
    [".stat-yellow", "黄卡 / 卡底"],
    [".stat-pink", "粉卡 / 卡底"],
    [".stat-orange", "橙卡 / 卡底"],
    [".stat-teal", "青卡 / 卡底"],
    [".stat-gray", "灰卡 / 卡底"],
    [".week-card .label", "周卡标签 / 卡底"],
    [".week-card .value", "周卡数值 / 卡底"],
    [".cal-w1 .v", "第1周数值 / 红底"],
    [".cal-w2 .v", "第2周数值 / 橙底"],
    [".cal-w3 .v", "第3周数值 / 黄底"],
    [".cal-w4 .v", "第4周数值 / 青底"],
    [".cal-w1 .p", "第1周平台名 / 红底"],
    [".cal-w4 .p", "第4周平台名 / 青底"],
    [".cal-cell.empty .d", "空格日期 / 空格底"],
    [".pc-name", "平台名 / 卡底"],
    [".pc-meta", "平台说明 / 卡底"],
    [".badge.ok", "成功徽标 / 绿底"],
    [".badge.fail", "失败徽标 / 红底"],
    [".badge.pending", "待签徽标 / 黄底"],
    [".badge.gray", "灰徽标 / 灰底"],
    [".nav a.active", "导航当前项 / 头部"],
    [".nav a:not(.active)", "导航未选中 / 头部"],
    [".brand small", "品牌副标题 / 头部"],
    [".btn-dark", "主按钮文字 / 底"],
    [".btn-outline", "次按钮文字 / 底"],
    [".legend", "图例文字 / 页面底"],
    [".table-foot", "表脚文字 / 表脚底"],
    [".pager-btn", "分页按钮 / 底"],
    [".chip.active", "选中筛选 / 底"],
    [".result-tag.CLAIMED", "成功状态字 / 卡底"],
    [".result-tag.ERROR", "错误状态字 / 卡底"],
    [".urgent-red", "紧急红字 / 页面底"],
    [".tip-banner", "提示条 / 底"],
    ["th", "表头文字 / 表头底"],
    ["td", "表格正文 / 卡底"],
    [".exec-tag", "执行标签 / 底"],
    [".pager-info", "分页信息 / 页面底"]
  ];
  var out = [];
  pairs.forEach(function(p) {
    var el = document.querySelector(p[0]);
    if (!el) { out.push({sel: p[0], name: p[1], miss: true}); return; }
    var cs = getComputedStyle(el);
    function bgOf(node) {
      var n = node;
      while (n && n !== document.documentElement) {
        var c = getComputedStyle(n).backgroundColor;
        if (c && c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent') return c;
        n = n.parentElement;
      }
      return getComputedStyle(document.body).backgroundColor;
    }
    out.push({sel: p[0], name: p[1], miss: false, fg: cs.color, bg: bgOf(el)});
  });
  return out;
}"""

# 主题行为检查：切换是否生效、是否持久化、刷新后是否保持
BEHAVIOR = """() => {
  var html = document.documentElement;
  var btn = document.getElementById('btn-theme');
  return {
    attr: html.getAttribute('data-theme'),
    scheme: getComputedStyle(html).colorScheme,
    bodyBg: getComputedStyle(document.body).backgroundColor,
    bodyFg: getComputedStyle(document.body).color,
    btnIcon: btn ? btn.textContent : null,
    saved: localStorage.getItem('cc-theme')
  };
}"""

NAVS = ["签到总览", "过期日历", "平台接入", "运行日志"]
collect = {"light": [], "dark": []}
behavior = []

with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome")
    pg = b.new_page(viewport={"width": 1400, "height": 1050})
    pg.goto("http://127.0.0.1:8317/", wait_until="networkidle")
    pg.wait_for_timeout(2500)

    for theme in ("light", "dark"):
        # 走真实路径：只写 localStorage 后 reload，让页面自己的首屏脚本决定主题，
        # 不能直接设data-theme——那会绕过 applyTheme，按钮图标就不会跟着更新
        pg.evaluate("(t)=>{localStorage.setItem('cc-theme',t);}", theme)
        pg.reload(wait_until="networkidle")
        pg.wait_for_timeout(2200)
        for nav in NAVS:
            pg.click(".nav a:has-text('%s')" % nav)
            pg.wait_for_timeout(2000)
            collect[theme].extend(pg.evaluate(PROBE))
        behavior.append((theme, pg.evaluate(BEHAVIOR)))

    # 点按钮切换，验证状态翻转
    pg.click(".nav a:has-text('签到总览')")
    pg.wait_for_timeout(800)
    before = pg.evaluate("()=>document.documentElement.getAttribute('data-theme')")
    pg.click("#btn-theme")
    pg.wait_for_timeout(500)
    after = pg.evaluate(BEHAVIOR)
    # 刷新后是否保持
    pg.reload(wait_until="networkidle")
    pg.wait_for_timeout(2000)
    reloaded = pg.evaluate(BEHAVIOR)
    b.close()

print("=" * 72)
print("对比度检查（WCAG AA 正文门槛 4.5:1）")
print("=" * 72)
fail = 0
# 关键判断标准：暗色不得比亮色更差。
# 浅色下本来就偏低的那几项（如 --muted 灰字 3.0:1）是改动前就有的，
# 不该算在暗色模式头上，所以逐元素对比两套主题的数值。
def collect_map(theme):
    m = {}
    for r in collect[theme]:
        if r.get("miss") or r["sel"] in m:
            continue
        m[r["sel"]] = (ratio(parse(r["fg"]), parse(r["bg"])), r["fg"], r["bg"])
    return m


lm = collect_map("light")
dm = collect_map("dark")
regress = 0
print()
print("%-24s %-9s %-9s %s" % ("元素", "浅色", "暗色", "判定"))
print("-" * 72)
for sel in sorted(set(lm) | set(dm)):
    l = lm.get(sel)
    d = dm.get(sel)
    lv = l[0] if l else None
    dv = d[0] if d else None
    # 判定分两档：
    # 1) 绝对可用性——暗色下必须 >= 4.5:1（这才是暗色模式的责任）
    # 2) 不得比亮色显著更差——容差 3.0，因为深底浅字的对比度天花板本就低于
    #    白底黑字（16:1 -> 14:1 属正常代价，不该误报）
    if dv is not None and dv < 4.5:
        verdict = "!! 暗色低于 4.5:1"
        regress += 1
        print("%-24s %-9s %-9s %s" % (sel, "%.2f:1" % lv, "%.2f:1" % dv, verdict))
        print("      暗色 %s / 底 %s" % (d[1], d[2]))
    elif lv is not None and dv is not None and dv < lv - 3.0:
        verdict = "!! 暗色明显差于浅色"
        regress += 1
        print("%-24s %-9s %-9s %s" % (sel, "%.2f:1" % lv, "%.2f:1" % dv, verdict))
    elif lv is not None and lv < 4.5:
        print("%-24s %-9s %-9s %s" % (sel, "%.2f:1" % lv, "%.2f:1" % dv, "亮色原有问题"))
    else:
        print("%-24s %-9s %-9s %s" % (sel, "%.2f:1" % lv, "%.2f:1" % dv, "ok"))

print("\n" + "=" * 72)
print("主题行为")
print("=" * 72)
for theme, s in behavior:
    print("  设为%-5s -> attr=%-5s scheme=%-5s 底色=%-18s 按钮=%s localStorage=%s"
          % (theme, s["attr"], s["scheme"], s["bodyBg"], s["btnIcon"], s["saved"]))
print("  点击切换：%s -> %s，按钮=%s，已存=%s" % (before, after["attr"], after["btnIcon"], after["saved"]))
print("  刷新后保持：attr=%s 底色=%s localStorage=%s" % (reloaded["attr"], reloaded["bodyBg"], reloaded["saved"]))

b_ok = 1
if after["attr"] == before:
    print("  FAIL 点击切换无效")
    b_ok = 0
if reloaded["attr"] != after["attr"]:
    print("  FAIL 刷新后未保持主题")
    b_ok = 0
if regress or not b_ok:
    print("\n== 暗色引入 %d 项对比度问题，行为%s ==" % (regress, "正常" if b_ok else "异常"))
    sys.exit(1)
print("\n== 暗色未引入对比度问题，主题行为正常 ==")
