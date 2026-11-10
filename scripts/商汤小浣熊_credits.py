"""商汤小浣熊权威积分查询 · ClawCheckin 隐藏积分源

与 scripts/商汤小浣熊.py（纯签到）分工：本脚本只查官方积分接口，不调用任何
发放链路。面板在每次签到后与启动时自动补跑本脚本，用权威余额刷新卡片上的
「总积分」。

积分口径：GET /api/web/points/v1/balance 的 available_points 为可用总量，
另有四个钱包明细（每日积分 / 每月会员积分 / 奖励积分 / 充值积分）。
临期快照：每日积分有效期官方规则为「默认当日 24:00 清零，当天登录后延长至
7 天」，账单接口每笔发放都带权威 expire_at，本脚本逐笔读取按到期日聚合
上报，24 小时 / 7 天自适应（见 _raccoon_expiry.py），不再硬编码当日清零。
临期只认「每日积分」钱包：daily_points 为 0 时不报任何临期——账单里的发放
笔目是发放原额，钱包已扣空说明这些积分早就花掉了；奖励/充值积分钱包的
到期日不由每日积分口径决定，不在本脚本上报范围。

凭据与续期与 商汤小浣熊.py 共用 scripts/_raccoon_sync.py。

输出契约（末行 JSON）：
  {
    "result": "ALREADY|NO_AUTH|NETWORK|ERROR",
    "report": "小浣熊余额 1234 分（每日积分 100；奖励积分 1134）",
    "today_credit": 0,
    "streak_days": 0,
    "total_credits": 1234
  }
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _raccoon_sync as rc  # noqa: E402  同目录共享模块：凭据漫游/续期/请求层
import _raccoon_expiry as rx  # noqa: E402  同目录共享模块：每日积分有效期自适应

PATH_BALANCE = "/api/web/points/v1/balance"
# 账单取数走 rc.fetch_bills()（v2 游标翻页）：v1 的 limit 被服务端封顶 100 且
# offset 翻页失效，重度账号一天上百条消耗，「7 天有效」的每日积分只剩 1 笔可见，
# 余额差额会被整笔挂到单个到期日（2026-11-10 真机：11-16 一格 2050 分，而官网
# 是 11-10→11-16 每天一笔 300）。翻到「最早到期日之前」即可停，见 fetch_bills。

# 钱包字段 → 展示名（顺序与 agent2api WALLET_LABELS 一致，只展示非零项）
WALLET_LABELS = [
    ("daily_points", "每日积分"),
    ("monthly_points", "每月会员积分"),
    ("reward_points", "奖励积分"),
    ("topup_points", "充值积分"),
]


def _to_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def main() -> int:
    state = rc.load_state()
    creds = rc.resolve_with_refresh()
    if creds is None:
        print(json.dumps({
            "result": "NO_AUTH",
            "report": "未找到小浣熊登录凭据，请先登录小浣熊桌面端",
            "today_credit": 0, "streak_days": 0, "total_credits": 0,
        }, ensure_ascii=False))
        return 2

    data, status, err = rc.api_json("GET", PATH_BALANCE, creds["access"])
    if data is None or not isinstance(data, dict):
        if status == 401:
            result, report = "NO_AUTH", "登录态被上游拒绝，请重新登录小浣熊桌面端"
        elif status in (-1, 429, 502, 503, 504):
            result, report = "NETWORK", f"积分查询失败：{err}"
        else:
            result, report = "ERROR", f"积分查询失败：{err}"
        print(json.dumps({
            "result": result, "report": report,
            "today_credit": 0, "streak_days": 0, "total_credits": 0,
        }, ensure_ascii=False))
        return 1

    available = _to_int(data.get("available_points"))
    if available is None:
        print(json.dumps({
            "result": "ERROR",
            "report": "余额响应缺少 available_points 字段（上游可能已调整接口）",
            "today_credit": 0, "streak_days": 0, "total_credits": 0,
        }, ensure_ascii=False))
        return 1

    # daily_points 单独取，且**不能**把 0 折叠成 None：0（钱包已被消耗干净）与
    # 「字段缺失」（None）语义相反——0 说明一分每日积分都不持有、没有任何积分会
    # 因到期而损失，必须原样传下去（差额 0 会清空该平台的临期快照）；缺失才退回
    # 按账单原额相加的老口径。旧实现写 `daily_points if daily_points > 0 else None`，
    # 把 0 当缺失，账单里早已花掉的每日积分发放原额被当成「即将到期」上报，
    # 日历上凭空出现一周的临期积分（2026-10-13 真机：钱包 0 却报 7×300）。
    daily_points = _to_int(data.get("daily_points"))
    wallets = []
    for key, label in WALLET_LABELS:
        value = _to_int(data.get(key))
        if value:
            wallets.append(f"{label} {value}")

    # 每日积分临期：按账单 expire_at 逐笔聚合（24 小时 / 7 天自适应，见 _raccoon_expiry）
    # daily_points 一并传入：账单记的是发放原额，直接相加会把已消耗部分也算成
    # 即将过期（900 vs 实际剩余 867），按余额摊薄后才等于真实会到期损失的积分。
    today = time.strftime("%Y-%m-%d")
    expiring = []
    # 翻页只翻到「最早到期日之前」：每日积分 7 天有效，往回多留 2 天缓冲，
    # 足以覆盖全部未到期笔目 + 到期当日的那次捕获（见 _raccoon_expiry）
    floor_day = time.strftime("%Y-%m-%d", time.localtime(time.time() - 9 * 86400))
    bills, _bills_status, _bills_err = rc.fetch_bills(
        creds["access"], stop_before_day=floor_day, timeout=30)
    if bills is not None:
        expiring = rx.daily_expiring_from_bills(bills, today, daily_points)

    # 统一格式：余额 X 分，N 笔到期（最早 YYYY-MM-DD）
    report = f"小浣熊余额 {available} 分"
    if expiring:
        report += f"，{len(expiring)} 笔到期（最早 {expiring[0]['date']}）"

    print(json.dumps({
        "result": "ALREADY", "report": report,
        "today_credit": 0, "streak_days": 0, "total_credits": available,
        "expiring": expiring,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
