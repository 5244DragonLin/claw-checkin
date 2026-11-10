"""库库AI（百度 GenFlow Pro）权威积分查询 · ClawCheckin 隐藏积分源

与 scripts/kuku.py（签到）分工：本脚本只读「还剩多少积分」，不领取任何
任务、不打 taskComplete。面板在每次签到后与启动时自动补跑本脚本，用权威
余额刷新卡片上的「总积分」，并给过期日历提供临期明细。

── 积分口径 ────────────────────────────────────────────────
GET /api/genflowpro/freepoint/homenew —— 「免费领积分」活动任务面板。
面板除任务列表外带活动积分读数（字段名随活动改版漂移，见
_kuku_sync.parse_total_points 的字段族探测；读不到判「余额未知」，
卡片留空而不是写 0 —— 与 Trae 积分源同一条「缺失 ≠ 0」纪律）。

── 会话续期（与签到同一条链）───────────────────────────────
先 userreport 换三件套再查面板；userreport 失败（Cookie 失效）只报实情
不换发 —— 换发（重新导 Cookie）是人工动作，由签到脚本/面板提示负责。

── 临期明细 ────────────────────────────────────────────────
面板任务上若带 expire 字段族（活动积分有效期），按天聚合进 expiring；
没有就返回空 —— 宁可不显示，不按活动结束日硬算（虚报比不报更糟）。

输出契约（末行 JSON）：
  {
    "result": "ALREADY|NO_AUTH|NETWORK|ERROR",
    "report": "库库积分 150，2 笔到期（最早 2026-11-15）",
    "today_credit": 0,
    "streak_days": 0,
    "total_credits": 150,
    "expiring": [{"date": "2026-11-15", "amount": 50, "source": "每日积分"}]
  }
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _kuku_sync as kd  # noqa: E402  同目录共享模块：凭据 / 请求层 / 任务链


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


def main() -> int:
    creds = kd.resolve_credentials()
    if creds is None:
        _client_creds, client_reason = kd.read_cookies_from_client()
        if client_reason:
            print(json.dumps(_out("NO_AUTH", f"库库登录态不可用：{client_reason}"),
                             ensure_ascii=False))
            return 2
        where = "、".join(label for label, _path in kd.credential_sources())
        print(json.dumps(_out(
            "NO_AUTH",
            f"未找到库库 Cookie（{where}、库库客户端、环境变量均无）。"
            "运行一次 `python scripts/kuku.py --login` 按引导导出即可"),
            ensure_ascii=False))
        return 2

    # ── 主口径：bizapi（「我的积分」面板同款接口，2026-11-09 打通）──────
    # 连签签到发放的 10 积分/天（有效期 7 天）记录在这里；余额与逐笔有效期
    # 都有权威来源，过期日历跟着流水走。
    balance_data, status, err = kd.point_balance(creds)
    if balance_data is not None:
        balance = kd.parse_balance(balance_data)
        if balance is None:
            # 账号从未获得过积分：如实「无数据」，不写 0
            print(json.dumps(_out(
                "ALREADY", "库库尚无积分账户记录（未获得过积分），卡片余额暂无数据"),
                ensure_ascii=False))
            return 0
        rows, _st2, _err2 = kd.point_records(creds)
        expiring = sorted(kd.parse_expiring_records(rows, balance["total"]),
                          key=lambda e: e["date"])
        report = (f"库库积分 {balance['total']}"
                  f"（会员 {balance['vip']}；充值 {balance['charge']}；赠送 {balance['bonus']}）")
        if expiring:
            report += f"，{len(expiring)} 笔到期（最早 {expiring[0]['date']}）"
        print(json.dumps(_out("ALREADY", report, total=balance["total"],
                              expiring=expiring), ensure_ascii=False))
        return 0

    # ── 兜底：freepoint 任务面板（bizapi 不可用时）────────────────────
    trio, status, err = kd.fetch_trio(creds)
    if trio is None:
        if kd.is_auth_reject(status, err):
            print(json.dumps(_out(
                "NO_AUTH",
                "库库登录态已失效（Cookie 过期或账号在别处重新登录），"
                "运行 `python scripts/kuku.py --login` 重新导一次 Cookie"),
                ensure_ascii=False))
            return 2
        if status in (-1, 429, 502, 503, 504):
            print(json.dumps(_out("NETWORK", f"库库积分查询失败（网络）：{err or status}"),
                             ensure_ascii=False))
            return 1
        print(json.dumps(_out("ERROR", f"库库积分查询失败：{err or status}"),
                         ensure_ascii=False))
        return 1

    panel = kd.point_panel(creds, trio)
    if panel is None:
        print(json.dumps(_out("ERROR", "库库积分查询失败（余额与任务面板均不可读）"),
                         ensure_ascii=False))
        return 1

    total = kd.parse_total_points(panel)
    if total is None:
        print(json.dumps(_out(
            "ALREADY",
            "库库无进行中的积分活动（当前是连签活动，奖励为抽奖次数与网盘会员，"
            "不产生积分余额），卡片余额暂无数据"),
            ensure_ascii=False))
        return 0

    expiring = sorted(kd.parse_expiring(panel), key=lambda e: e["date"])
    report = f"库库积分 {total}"
    if expiring:
        report += f"，{len(expiring)} 笔到期（最早 {expiring[0]['date']}）"

    print(json.dumps(_out("ALREADY", report, total=total, expiring=expiring),
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
