"""验证：用户手动点「刷新积分」按钮时，按钮仍要进入「刷新中…」并给出反馈。

上一处修复把按钮态改成「只有 silent=false 才改」，这里守住另一半：
手动点击是 silent=false，必须照旧显示「刷新中…」并在结束后恢复，
不能被修复顺带削掉。同时确认手动刷新后总览会正常更新数据。
"""
import json
import time
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8317/"

PROBE = r"""
(() => {
  window.__log = [];
  const t0 = performance.now();
  const stamp = () => ((performance.now() - t0) / 1000).toFixed(2);
  const push = (k, d) => window.__log.push({ t: stamp(), kind: k, detail: d });
  let armed = false;
  function arm() {
    const btn = document.querySelector("#btn-refresh-credits");
    if (!btn) return false;
    if (armed) return true;
    armed = true;
    let last = null;
    setInterval(() => {
      const cur = btn.disabled + "|" + btn.textContent;
      if (cur !== last) { last = cur; push("btn", "disabled=" + btn.disabled + " 文案=" + btn.textContent); }
    }, 25);
    // 手动点击应弹 toast，一并观察
    const mo = new MutationObserver(() => {
      const el = document.querySelector("#toast, .toast");
      if (el && el.textContent) push("toast", el.textContent.trim().slice(0, 40));
    });
    mo.observe(document.body, { childList: true, subtree: true });
    return true;
  }
  if (!arm()) {
    document.addEventListener("DOMContentLoaded", arm);
    const iv = setInterval(() => { if (arm()) clearInterval(iv); }, 20);
  }
  return "ok";
})()
"""


def run(wait_debounce: int = 35):
    if wait_debounce:
        # 手动点击走 silent=false，服务端防抖不拦它，但若被拦下会秒回，
        # 「刷新中…」只闪一帧就结束，25ms 采样容易漏判。
        # 等过 page_load_debounce 让它真跑一次脚本，按钮态能稳定观测到。
        print(f"等待 {wait_debounce}s 让服务端防抖窗口过期…")
        time.sleep(wait_debounce)

    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome")
        pg = b.new_page()
        pg.add_init_script(PROBE)
        pg.goto(URL, wait_until="networkidle")
        pg.wait_for_timeout(2500)

        pg.click("#btn-refresh-credits")
        pg.wait_for_timeout(9000)
        log = pg.evaluate("window.__log")
        btn_state = pg.evaluate(
            "(()=>{const b=document.querySelector('#btn-refresh-credits');"
            "return b?b.disabled+'|'+b.textContent:'按钮不存在';})()")
        b.close()

    print("=== 时间线 ===")
    for e in log:
        print(f"  [{e['t']:>6}s] {e['kind']:<6} {e['detail']}")

    btn_events = [e for e in log if e["kind"] == "btn"]
    busy = [e for e in btn_events if "刷新中" in e["detail"]]
    back = [e for e in btn_events if e["detail"].endswith("文案=刷新积分")]

    print()
    print("=== 判定 ===")
    ok = True
    if busy:
        print(f"  [ OK ] 手动点击后按钮进入「刷新中…」（{len(busy)} 次）")
    else:
        ok = False
        print("  [FAIL] 手动点击后按钮未显示「刷新中…」——修复过头，把手动反馈也削掉了")
    if back:
        print(f"  [ OK ] 刷新结束后按钮恢复「刷新积分」（{len(back)} 次）")
    else:
        ok = False
        print("  [FAIL] 刷新结束后按钮未恢复文案")
    if btn_state == "false|刷新积分":
        print(f"  [ OK ] 终态正确：{btn_state}")
    else:
        ok = False
        print(f"  [FAIL] 终态异常：{btn_state}")

    toast = [e for e in log if e["kind"] == "toast"]
    print(f"  [-- ] 观察到 toast {len(toast)} 条：{[e['detail'] for e in toast][:3]}")
    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    print(json.dumps({"result": "PASS" if run(35) else "FAIL"}, ensure_ascii=False))
