"""验证：刷新页面后不再出现「刷新中…」按钮态，且总览面板不会被清空重画。

注意：add_init_script 在 DOM 构建之前运行，此时 #ov-body / #btn-refresh-credits
都还不存在。探针必须等 DOM 就绪后再挂监听，否则 observer 挂到 null 上、
判定会假通过。这里用 DOMContentLoaded + 轮询兜底两次，确保元素到手再开始记录。
"""
import json
import time
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8317/"

PROBE = r"""
(() => {
  window.__log = [];
  window.__refetchCount = 0;
  const t0 = performance.now();
  const stamp = () => ((performance.now() - t0) / 1000).toFixed(2);
  const push = (kind, detail) => window.__log.push({ t: stamp(), kind, detail });

  // fetch 拦截不依赖 DOM，先挂。响应回来后记 skipped / ran，
  // 用来确认本轮到底走没走到 else 分支（真正会触发重画的那条路）
  const of = window.fetch;
  window.fetch = function (...a) {
    const u = String(a[0] || "");
    if (u.indexOf("/api/refresh_credits") < 0) return of.apply(this, a);
    window.__refetchCount += 1;
    const n = window.__refetchCount;
    push("refresh_call", "第 " + n + " 次");
    return of.apply(this, a).then((resp) => resp.clone().json().then((j) => {
      push("res", "skipped=" + !!j.skipped + " ran=" + (j.ran === undefined ? "-" : j.ran)
        + " ok=" + (j.ok === undefined ? "-" : j.ok) + " (" + n + ")");
      return resp;
    }).catch(() => resp));
  };

  let armed = false;
  function arm() {
    const btn = document.querySelector("#btn-refresh-credits");
    const ovBody = document.querySelector("#ov-body");
    const ovSub = document.querySelector("#ov-sub");
    if (!btn || !ovBody || !ovSub) return false;
    if (armed) return true;
    armed = true;
    push("armed", "探针已挂载，按钮文本=" + btn.textContent);

    let lastBtn = null;
    setInterval(() => {
      const cur = btn.disabled + "|" + btn.textContent;
      if (cur !== lastBtn) {
        lastBtn = cur;
        push("btn", "disabled=" + btn.disabled + " 文案=" + btn.textContent);
      }
    }, 25);

    new MutationObserver(() => {
      const d = ovBody.style.display;
      push("ovbody", "display=" + (d === "" ? "(空=可见)" : d));
    }).observe(ovBody, { attributes: true, attributeFilter: ["style"] });

    new MutationObserver(() => {
      push("ovsub", ovSub.textContent);
    }).observe(ovSub, { childList: true, characterData: true, subtree: true });

    return true;
  }

  if (!arm()) {
    document.addEventListener("DOMContentLoaded", arm);
    const iv = setInterval(() => { if (arm()) clearInterval(iv); }, 20);
  }
  return "probe-installed";
})()
"""


def run(wait_debounce: int = 0):
    if wait_debounce:
        # 必须等过服务端 page_load_debounce（默认 30 秒），否则 refresh_credits
        # 被服务端拦成 skipped，走 if 分支，else 里那段重画逻辑根本不会执行——
        # 那样测出来的「无重画」是假通过。
        print(f"等待 {wait_debounce}s 让服务端防抖窗口过期…")
        time.sleep(wait_debounce)

    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome")
        pg = b.new_page()
        pg.add_init_script(PROBE)
        pg.goto(URL, wait_until="networkidle")
        pg.wait_for_timeout(9000)
        log = pg.evaluate("window.__log")
        cnt = pg.evaluate("window.__refetchCount")
        b.close()

    armed = [e for e in log if e["kind"] == "armed"]
    print("=== 时间线 ===")
    for e in log:
        print(f"  [{e['t']:>6}s] {e['kind']:<12} {e['detail']}")

    print()
    print("=== 判定 ===")
    ok = True
    if not armed:
        ok = False
        print("  [FAIL] 探针未成功挂载，本次结果不可信")
    else:
        print("  [ OK ] 探针已挂载，结果可信")

    btn_busy = [e for e in log if e["kind"] == "btn" and ("disabled=true" in e["detail"] or "刷新中" in e["detail"])]
    if btn_busy:
        ok = False
        print(f"  [FAIL] 刷新页面后按钮进入「刷新中…」{len(btn_busy)} 次：{btn_busy}")
    else:
        print("  [ OK ] 按钮全程正常态，无「刷新中…」")

    blank = [e for e in log if e["kind"] == "ovbody" and "display=none" in e["detail"]]
    if blank:
        ok = False
        print(f"  [FAIL] 总览面板被隐藏（清空重画）{len(blank)} 次：{blank}")
    else:
        print("  [ OK ] 总览面板未被隐藏，无清空重画")

    loading = [e for e in log if e["kind"] == "ovsub" and "加载中" in e["detail"]]
    if loading:
        ok = False
        print(f"  [FAIL] 副标题二次回退「加载中…」{len(loading)} 次：{loading}")
    else:
        print("  [ OK ] 副标题无二次「加载中…」")

    # skipped = 走 if 分支（没跑脚本），ran = 走 else 分支（真跑了脚本）
    # 注意要匹配 skipped=true，写 "skipped" 会把 skipped=false 也误判成被拦下
    skipped = [e for e in log if e["kind"] == "res" and "skipped=true" in e["detail"]]
    ran = [e for e in log if e["kind"] == "res" and "skipped=false" in e["detail"]]
    if skipped:
        print(f"  [WARN] 本次被服务端防抖拦下（skipped），未覆盖真实刷新路径——结果不足以判定")
        ok = False
    if ran:
        print(f"  [ OK ] 本次真实跑了积分源（{ran[0]['detail']}），覆盖到重画路径")
    else:
        print("  [WARN] 未观测到真实刷新，else 分支未被覆盖")

    print(f"  [-- ] refresh_credits 被调用 {cnt} 次")
    print()
    print("RESULT:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    # 默认等 35 秒跨过 30 秒防抖，覆盖「真实跑脚本」这条路径
    print(json.dumps({"result": "PASS" if run(35) else "FAIL"}, ensure_ascii=False))
