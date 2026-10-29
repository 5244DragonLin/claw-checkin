"""Trae 权威积分查询 · ClawCheckin 隐藏积分源

与 scripts/trae.py（签到）分工：本脚本只读「还剩多少积分」，不领取任何东西、
不打签到链。面板在每次签到后、页面加载时与固定周期都会补跑本脚本，用权威
余额刷新卡片上的「总积分」，并给过期日历提供临期明细。

── 积分口径（2026-10 对着真实响应核对）──────────────────────
POST {ug}/trae/api/v2/pay/ide_user_ent_usage  body `{}`（只读）

响应里两处能算「还剩多少」，实测两者一致：

  · `user_entitlement_pack_list[]` — 每个权益包的
      `entitlement_base_info.quota.credits_limit`（授予额）
      `usage.credits_amount`（已用量）
    SOLO 积分制真正扣的就是这一笔。取 `Σ max(limit - used, 0)`、逐包
    四舍五入（与官方 cashier 同口径）。
  · 顶层 `usage_summary.total_amount / consumed_amount` — 官方自己算的
    同一个池子，不逐包取整。

主口径走逐包求和；只有所有包都没有 `credits_limit` 时才退到官方汇总。
两者都没有时判「余额未知」（`known=False`），卡片留空而不是写 0 ——
参考实现修过的那个真实 bug 就是把「没读到」渲染成「剩 0 积分」。

── 到期日从哪来（不需要本地台账）────────────────────────────
每个包的 `entitlement_base_info.end_time`（秒级 epoch）就是该包失效的时刻，
所以临期明细直接读它：日期用本机时区换成「那一天」，金额用该包剩余。
日历上的合计因此与卡片余额对得上，也不会虚报已消耗掉的份额。
没有 `end_time` 的包（上游没给）不进日历 —— 宁可不显示，不按套餐周期硬算。

── 三处不能「顺手美化」的地方 ───────────────────────────────
  · `credits_limit == -1` 是**不限**，不是耗尽；
  · `credits_amount` 缺失按官方 `?? 0` 处理，但整层 `credits_limit` 缺失
    绝不能当 0（见上）；
  · `product_type == 3`（兑换码活动包）与 `is_hide` / `status == 3` 的包
    不参与聚合 —— 上游自己就不把它们计入。

凭据与请求层与 scripts/trae.py 共用 scripts/_trae_sync.py（同一份登录态、
同一套 ug 画像头）。本脚本不发签到请求，也不改写凭据。

输出契约（末行 JSON）：
  {
    "result": "ALREADY|NO_AUTH|NETWORK|ERROR",
    "report": "Trae 积分 4600（老用户福利 4000；每月登录赠送 500；签到奖励 100），3 笔到期（最早 2026-10-31）",
    "today_credit": 0,
    "streak_days": 0,
    "total_credits": 4600,
    "expiring": [{"date": "2026-10-31", "amount": 500, "source": "每月登录赠送"}]
  }
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _trae_sync as ts  # noqa: E402  同目录共享模块：凭据 / 请求层 / 积分解析


def _out(result: str, report: str, total: int | None = None,
         expiring: list[dict] | None = None) -> dict:
    out = {
        "result": result,
        "report": report,
        "today_credit": 0,
        "streak_days": 0,
        # 读不到余额时给 null：面板据此回退到上一次已知值，而不是把卡片刷成 0
        "total_credits": total,
    }
    if expiring is not None:
        out["expiring"] = expiring
    return out


def _detail(packs: list[dict]) -> str:
    """明细：按包列出还有余额的那几项（同名的合并，最大的在前）。"""
    buckets = ts.credit_buckets(packs)
    if not buckets:
        return ""
    parts = [f"{title} {amount}" for title, amount in
             sorted(buckets.items(), key=lambda kv: -kv[1])]
    return "；".join(parts)


def main() -> int:
    creds = ts.resolve_credentials()
    if creds is None:
        where = "、".join(label for label, _path in ts.credential_sources())
        print(json.dumps(_out(
            "NO_AUTH",
            f"未找到 Trae 登录凭据（{where}、环境变量均无）。"
            "运行一次 `python scripts/trae.py --login` 走网页登录"), ensure_ascii=False))
        return 2

    reading = ts.read_credit(creds)
    if not reading.get("ok"):
        kind = reading.get("kind")
        error = str(reading.get("error") or "")
        if kind == "auth":
            # 1001 是服务端作废了这张 JWT；这里不换发 —— 换发由签到脚本负责，
            # 积分查询只报实情，避免两个脚本同时改写同一份凭据
            print(json.dumps(_out(
                "NO_AUTH",
                f"Trae 登录态已失效（{error}），"
                "运行 `python scripts/trae.py` 签到会自动换发，或重新授权一次"),
                ensure_ascii=False))
            return 2
        if kind == "network":
            print(json.dumps(_out("NETWORK", f"Trae 积分查询失败（网络）：{error}"),
                             ensure_ascii=False))
            return 1
        print(json.dumps(_out("ERROR", f"Trae 积分查询失败：{error}"), ensure_ascii=False))
        return 1

    if not reading["known"]:
        # 上游既没给权益包额度、也没给官方汇总：报「未知」而不是 0
        print(json.dumps(_out(
            "ALREADY",
            "Trae 没有可读的积分额度（上游未返回权益包与官方汇总），"
            "卡片余额暂不更新"), ensure_ascii=False))
        return 0

    if reading["unlimited"]:
        print(json.dumps(_out("ALREADY", "Trae 积分不限量（上游返回 -1），卡片不填数字",
                              total=None,
                              expiring=reading["expiring"]), ensure_ascii=False))
        return 0

    total = int(reading["remain"])
    expiring = sorted(reading["expiring"], key=lambda e: e["date"])
    report = f"Trae 积分 {total}"
    detail = _detail(reading["packs"])
    if detail:
        report += f"（{detail}）"
    if expiring:
        report += f"，{len(expiring)} 笔到期（最早 {expiring[0]['date']}）"

    print(json.dumps(_out("ALREADY", report, total=total, expiring=expiring),
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
