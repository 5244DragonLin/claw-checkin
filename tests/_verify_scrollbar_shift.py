"""定位：切换页面时右侧滚动条出现/消失导致内容区横向跳动。

两个关键坑（都踩过）：
1. **Playwright 无头Chrome 用的是「悬浮滚动条」（overlay），不占布局宽度**，
   window.innerWidth - documentElement.clientWidth 恒为 0。必须用有头模式
   驱动系统 Chrome 才能量到经典滚动条的真实占位。
2. **不能用容器宽度判定**。1920 视口下容器受 max-width:1500px 限制，
   即便滚动条占 17px，可用宽 1905 仍大于 1500，容器宽度不变 1500——
   但它的**居中位置**会左移约 8px。这才是用户看到的「视觉偏差」。

所以本脚本测容器 offsetLeft（有滚动条页 vs 无滚动条页之差），
修复后各页 offsetLeft 应完全一致。
"""
import json
import sys
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8317/"

PROBE = r"""
(async () => {
  const PAGES = ["overview", "platforms", "calendar", "logs"];
  const out = [];
  for (const p of PAGES) {
    location.hash = "#" + p;
    window.dispatchEvent(new HashChangeEvent("hashchange"));
    await new Promise((r) => setTimeout(r, 1500));
    const de = document.documentElement;
    const c = document.querySelector(".container");
    out.push({
      page: p,
      scrollH: de.scrollHeight,
      clientH: de.clientHeight,
      innerW: window.innerWidth,
      clientW: de.clientWidth,
      // 真实滚动条占位宽度（经典模式约 15~17，悬浮模式恒为 0）
      sbW: window.innerWidth - de.clientWidth,
      hasVScrollbar: de.scrollHeight > de.clientHeight,
      gutter: getComputedStyle(de).scrollbarGutter,
      containerLeft: c ? Math.round(c.getBoundingClientRect().left) : null,
      containerW: c ? Math.round(c.getBoundingClientRect().width) : null,
    });
  }
  return out;
})()
"""


def run(headless=False):
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=headless)
        pg = b.new_page(viewport={"width": 1920, "height": 1080})
        pg.goto(URL, wait_until="networkidle")
        pg.wait_for_timeout(2500)
        rows = pg.evaluate(PROBE)
        b.close()

    print(f"=== 各页实测（1920×1080, headless={headless}） ===")
    print(f"{'页面':<11}{'内容高':>7}{'视口高':>7}{'滚动条占位':>11}{'容器左':>8}{'容器宽':>8}  有滚动条")
    for r in rows:
        print(f"{r['page']:<11}{r['scrollH']:>7}{r['clientH']:>7}{r['sbW']:>11}"
              f"{str(r['containerLeft']):>8}{str(r['containerW']):>8}  {'有' if r['hasVScrollbar'] else '无'}")

    print()
    print(f"scrollbar-gutter: {rows[0]['gutter']}")
    print()

    print("=== 判定 ===")
    ok = True
    sb = {r["page"]: r["sbW"] for r in rows}
    lefts = {r["page"]: r["containerLeft"] for r in rows}

    if max(sb.values()) == 0:
        print("  [ !! ] 滚动条占位全部为 0 —— 当前是无头/悬浮滚动条环境，本轮测不到真实差异")
        print("         （真实 Windows Chrome 经典滚动条约 15~17px）")
    else:
        print(f"  [-- ] 真实滚动条占位宽度: {sb}")

    uniq = set(lefts.values())
    if len(uniq) > 1:
        ok = False
        mn, mx = min(lefts.values()), max(lefts.values())
        print(f"  [FAIL] 各页容器居中位置不一致 → 切换时内容整体横向位移：")
        for p, v in lefts.items():
            print(f"         {p:<11}left={v}px")
        print(f"         最大偏差 {mx - mn}px（正是用户看到的视觉偏差）")
    else:
        print(f"  [ OK ] 各页容器居中位置一致（left={lefts}px），切换无横向位移")

    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    headless = "--headless" in sys.argv
    print(json.dumps({"result": "PASS" if run(headless) else "FAIL"}, ensure_ascii=False))
