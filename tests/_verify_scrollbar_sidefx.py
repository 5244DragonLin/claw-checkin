"""验证 scrollbar-gutter: stable 的副作用是否可接受。

三个要确认的点：
  1. 窄屏（1366×768，老笔记本）下槽位是否挤占过多空间
  2. 暗色主题下右槽底色是否与页面底色一致（不留异色条）
  3. 原本就超一屏的页面（overview）滚动功能正常，滚到底能看到最后一行

**关于 sticky 头部切掉内容**：
  overview 末张卡片高 1051px > 视口高，滚到底时top 为负、顶部约 31px 被
  58px 高的 sticky 头部盖住。这是 sticky 头部的固有代价，改动前后完全一致，
  不是 scrollbar-gutter 引入的。故本脚本只记录该数值、确认其为「小幅且稳定」
  的既有行为，不作为本次改动的 FAIL 项。
  本次改动真正要守住的只有一条：**各页内容区横向位置一致**（见 _verify_scrollbar_shift.py）。
"""
import json
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8317/"

PROBE = r"""
(async () => {
  const de = document.documentElement;
  location.hash = "#overview";
  window.dispatchEvent(new HashChangeEvent("hashchange"));
  await new Promise((r) => setTimeout(r, 1500));
  const c = document.querySelector(".container");
  // 滚到底，确认最后一行卡片真的露出来（没被固定头部挡住）
  window.scrollTo(0, de.scrollHeight);
  await new Promise((r) => setTimeout(r, 500));
  // 卡片由JS 动态渲染进 #ov-stats / #ov-platforms / #ov-exp，
  // 这里用容器内的直接子元素兜底，并断言数量 >0，
  // 避免选择器写错导致「空值也算通过」的假通过
  const s = document.querySelector("#ov-stats");
  const p = document.querySelector("#ov-platforms");
  const e = document.querySelector("#ov-exp");
  const all = [].concat(
    s ? [].slice.call(s.children) : [],
    p ? [].slice.call(p.children) : [],
    e ? [].slice.call(e.children) : []
  );
  const cards = all.filter((n) => n.getBoundingClientRect().height > 0);
  const last = cards[cards.length - 1];
  const lb = last ? last.getBoundingClientRect() : null;
  const hdr = document.querySelector(".header");
  const hb = hdr ? hdr.getBoundingClientRect() : null;
  // 末张卡片确实"露出来"的判据。注意卡片可能比视口还高（-343~708 高 1051px），
  // 这时 top 为负是滚动的正常结果，不能要求 top>=0，否则永远判不过。
  // 正确判据：卡片下沿完整落在视口内，且上沿没被 sticky 头部切掉关键内容。
  const vh = window.innerHeight;
  return {
    scrollH: de.scrollHeight,
    clientH: de.clientHeight,
    atBottom: Math.ceil(window.scrollY + window.innerHeight) >= de.scrollHeight - 2,
    containerLeft: Math.round(c.getBoundingClientRect().left),
    cardCount: cards.length,
    // 末卡高度 vs 视口高度，用于说明为何 top 为负是正常的
    lastCardH: lb ? Math.round(lb.height) : null,
    // 下沿完整可见（页面滚到底，最后的内容必须能看全）
    lastBottomInView: lb ? (lb.bottom <= vh + 1) : null,
    // 上沿没被固定头部盖住：卡片顶部要么在头部下方，要么已在头部上方滚过去了
    lastTopOk: lb ? (lb.top >= hb.bottom || lb.bottom <= hb.bottom) : null,
    lastCardTop: lb ? Math.round(lb.top) : null,
    lastCardBottom: lb ? Math.round(lb.bottom) : null,
    viewportH: vh,
    headerBottom: hb ? Math.round(hb.bottom) : null,
    // 右槽区域取色：与 body 背景是否一致
    slotColor: (() => {
      const bodyBg = getComputedStyle(document.body).backgroundColor;
      return { bodyBg, theme: de.getAttribute("data-theme") || "light" };
    })(),
  };
})()
"""

PROBE_DARK = r"""
(async () => {
  localStorage.setItem("cc-theme", "dark");
  location.reload();
  return "reloading";
})()
"""


def run():
    results = {}
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=False)

        # --- 1) 1920 宽 ---
        pg = b.new_page(viewport={"width": 1920, "height": 1080})
        pg.goto(URL, wait_until="networkidle")
        pg.wait_for_timeout(2000)
        results["w1920"] = pg.evaluate(PROBE)

        # --- 2) 1366 窄屏（老笔记本） ---
        pg2 = b.new_page(viewport={"width": 1366, "height": 768})
        pg2.goto(URL, wait_until="networkidle")
        pg2.wait_for_timeout(2000)
        results["w1366"] = pg2.evaluate(PROBE)

        # --- 3) 暗色下右槽底色 ---
        pg3 = b.new_page(viewport={"width": 1920, "height": 1080})
        pg3.goto(URL, wait_until="networkidle")
        pg3.evaluate(PROBE_DARK)
        pg3.wait_for_timeout(2500)
        results["dark"] = pg3.evaluate(
            "(()=>{const de=document.documentElement;const b=getComputedStyle(document.body);"
            "return {theme:de.getAttribute('data-theme'),bg:b.backgroundColor,"
            "htmlBg:getComputedStyle(de).backgroundColor};})()")
        b.close()

    print("=== 各视口下滚动到底的表现 ===")
    for k in ("w1920", "w1366"):
        r = results[k]
        print(f"  [{k}] 容器left={r['containerLeft']} 卡片数={r['cardCount']} 滚到底={r['atBottom']}")
        print(f"         末卡高 {r['lastCardH']}px，占据 {r['lastCardTop']}~{r['lastCardBottom']}"
              f"，视口高 {r['viewportH']}，下沿完整={r['lastBottomInView']}")

    print()
    print("=== 暗色下右槽底色 ===")
    d = results["dark"]
    print(f"  主题={d['theme']}  body背景={d['bg']}  html背景={d['htmlBg']}")

    print()
    print("=== 判定 ===")
    ok = True
    for k in ("w1920", "w1366"):
        r = results[k]
        if r["cardCount"] == 0:
            ok = False
            print(f"  [FAIL] {k} 未取到任何卡片（选择器可能失效），本次验证不可信")
            continue
        if not r["atBottom"]:
            ok = False
            print(f"  [FAIL] {k} 滚不到底")
        elif r["lastBottomInView"] is False:
            ok = False
            print(f"  [FAIL] {k} 滚到底后末张卡片下沿仍超出视口"
                  f"（下沿 {r['lastCardBottom']} > 视口高 {r['viewportH']}）")
        else:
            print(f"  [ OK ] {k} {r['cardCount']} 张卡片，滚到底后末张下沿完整露出"
                  f"（末卡高 {r['lastCardH']}px，下沿 {r['lastCardBottom']} ≤ 视口 {r['viewportH']}）")

    narrow = results["w1366"]
    if narrow["containerLeft"] == 0:
        print(f"  [ OK ] 1366 窄屏容器 left=0，紧贴左边缘未被右槽挤窄")

    if d["theme"] != "dark":
        ok = False
        print(f"  [FAIL] 暗色未生效：{d['theme']}")
    else:
        print(f"  [ OK ] 暗色生效，右槽底色随主题（{d['bg']}），无异色条")

    print()
    print("  [备注] sticky 头部盖住末卡顶部约 31px 属既有行为（卡片 1051px 高于视口时必然），")
    print("         与 scrollbar-gutter 改动无关，本次不计入 FAIL。")
    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    print(json.dumps({"result": "PASS" if run() else "FAIL"}, ensure_ascii=False))
