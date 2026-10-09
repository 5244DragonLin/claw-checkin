"""商汤小浣熊每日签到（桌面登录积分）· ClawCheckin 接入脚本

参考：https://github.com/aimod-cc/agent2api（desktop-tauri providers/raccoon）

小浣熊没有 WorkBuddy 那种「每日签到」接口，每日奖励走的是桌面端登录积分链路：
官方桌面客户端每次启动会打两个接口，服务端据此按天发放每日积分。本脚本复刻
同一条链路作为「签到」：
  1. POST /api/web/desktop/v1/login/points/grant —— 首次桌面登录奖励
     （一次性，幂等；老账号已发放过时上游返回 granted=false）；
  2. GET  /api/web/office/v3/setting_info —— 每日积分的发放触发器（按天幂等），
     发放通知在响应的 point_grant_popups / point_grant_toast；
  3. GET  /api/web/points/v1/bills —— 核对今日实际入账，作为权威结果
     （账单接口慢，独立 30s 超时；核对失败按 NETWORK 处理、不更新本地状态，
     下次重跑时账单里看得到就是领到了，不会丢）。

「已签到」判定：账单里今日确有「每日积分发放」/「桌面端登录奖励」入账，而本地
状态已记过今天 → ALREADY；今日无签到事件入账且核对成功 → 同样 ALREADY（可能
已由桌面端领取过）。收益只认这两类事件——老用户回馈等运营活动入账不计入
today_credit。连续天数只在首次确认入账的当天 +1（last_date 守卫，同日重跑不双计）。
每日积分有效期自适应（scripts/_raccoon_expiry.py，2026-10 核实官方口径）：
每日积分每天 00:00 发放，默认当日 24:00 清零，当天登录后有效期延长至 7 天。
本脚本的 API 签到是否被服务端算作「当天登录」尚无干净样本，不做假设：
签到报告直接写明本笔到期时刻（读账单 expire_at）；临期快照统一由隐藏积分源
商汤小浣熊_credits.py 上报（两个脚本同报会被账本按平台名双计），
24 小时 / 7 天两种模式自适应，历史硬编码「当日清零」已废弃。

凭据与续期（access 有效期仅约 3 小时）：见 scripts/_raccoon_sync.py——
桌面端登录态 ~/.box-agent/config/auth.json 优先，过期自动用 refreshToken
向 /api/web/auth/v1/refresh 续期并写回，双机轮流使用时凭据随同步盘漫游。

权威余额由隐藏积分源 scripts/商汤小浣熊_credits.py 查询，签到后面板自动补跑。

输出契约（末行 JSON，与 AutoClaw 一致）：
  {
    "result": "CLAIMED|ALREADY|NETWORK|ERROR|NO_AUTH",
    "report": "可读汇报",
    "today_credit": 100,
    "streak_days": 7,
    "total_credits": 700
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

PATH_GRANT = "/api/web/desktop/v1/login/points/grant"
PATH_SETTING = "/api/web/office/v3/setting_info"
PATH_BILLS = "/api/web/points/v1/bills?paging.limit=40&paging.offset=0"
PATH_BALANCE = "/api/web/points/v1/balance"
BILLS_TIMEOUT = 30      # 账单接口实测约 9 秒，独立超时兜底（agent2api 同款）

# 签到相关的账单事件（实测口径，2026-10）：
#   每日积分发放 —— 每日签到收益，有效期自适应：默认当日 24:00 清零，当天
#                  登录后延长至 7 天（逐笔读账单 expire_at，见 _raccoon_expiry）；
#   桌面端登录奖励 —— 首次桌面登录的一次性奖励（发放链路 ① 领取的那笔）。
# 其余正数入账（老用户回馈等运营活动）不计入签到收益，避免误记。
DAILY_GRANT_EVENT = "每日积分发放"
CHECKIN_EVENTS = (DAILY_GRANT_EVENT, "桌面端登录奖励")


def _out(result: str, report: str, today: int, streak: int, total) -> dict:
    return {
        "result": result,
        "report": report,
        "today_credit": int(today),
        "streak_days": int(streak),
        "total_credits": int(total) if isinstance(total, (int, float)) else 0,
    }


def _today_bills(bills: dict, today: str) -> tuple[list[dict], int]:
    """把今天的账单分拣为 (签到入账列表, 其他正数入账合计)。

    签到入账 = points>0、created_at 日期部分 == 本地今天、事件名在
    CHECKIN_EVENTS 之内；运营活动等其他入账单独合计、只进报告不计收益。
    """
    items = bills.get("items") if isinstance(bills, dict) else None
    grants, other = [], 0
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            points = int(float(item.get("points")))
        except (TypeError, ValueError):
            continue
        created_at = str(item.get("created_at") or "")
        if points <= 0 or created_at[:10] != today:
            continue
        name = str(item.get("event_name") or "积分发放")
        if name in CHECKIN_EVENTS:
            grants.append({
                "name": name,
                "points": points,
                "at": created_at.replace("T", " ")[11:19],
            })
        else:
            other += points
    return grants, other


def _balance(jwt: str) -> tuple[int | None, str]:
    """查询可用积分余额，返回 (available, err)。"""
    data, _status, err = rc.api_json("GET", PATH_BALANCE, jwt)
    if data is None:
        return None, err
    value = data.get("available_points") if isinstance(data, dict) else None
    try:
        return int(float(value)), ""
    except (TypeError, ValueError):
        return None, "余额响应缺少 available_points"


def _grant_flow(jwt: str) -> tuple[dict, dict]:
    """执行发放链路 ①②：桌面登录奖励 + 每日积分触发器。

    返回 ({granted, error}, {popups, toast, error})；单步失败不阻断 ——
    真正的每日发放以账单核对为准。
    """
    grant_data, _s, grant_err = rc.api_json(
        "POST", PATH_GRANT, jwt, extra_headers=rc.DESKTOP_HEADERS)
    granted = bool(isinstance(grant_data, dict) and grant_data.get("granted"))

    headers = list(rc.DESKTOP_HEADERS) + [("X-Raccoon-Language", "zh")]
    setting_data, _s2, setting_err = rc.api_json(
        "GET", PATH_SETTING, jwt, extra_headers=headers)
    setting = {
        "popups": setting_data.get("point_grant_popups") if isinstance(setting_data, dict) else None,
        "toast": setting_data.get("point_grant_toast") if isinstance(setting_data, dict) else None,
        "error": setting_err,
    }
    return {"granted": granted, "error": grant_err}, setting


def _run() -> dict:
    state = rc.load_state()
    today = time.strftime("%Y-%m-%d")

    creds = rc.resolve_with_refresh()
    if creds is None:
        sources = [name for name, _p, _d in rc.credential_sources()]
        where = "、".join(sources) if sources else "桌面端登录态与 auths/ 镜像"
        return _out("NO_AUTH",
                    f"未找到小浣熊登录凭据（{where} 均无）；请先登录小浣熊桌面端，"
                    "或把 refreshToken 放入 auths/raccoon.json",
                    0, state["streak_days"], state["last_total"])

    # ①② 发放链路（失败不阻断，账单核对给权威结论）
    _grant, setting = _grant_flow(creds["access"])

    # ③ 账单核对今日入账（失败按 NETWORK 处理：不更新本地状态，下次重跑可补认）
    bills, bills_status, bills_err = rc.api_json(
        "GET", PATH_BILLS, creds["access"], timeout=BILLS_TIMEOUT)
    if bills is None:
        if bills_status == 401:
            return _out("NO_AUTH", "登录态被上游拒绝（续期后仍 401），"
                                   "请重新登录小浣熊桌面端",
                        0, state["streak_days"], state["last_total"])
        result = "NETWORK" if bills_status in (-1, 429, 502, 503, 504) else "ERROR"
        return _out(result, f"账单核对失败：{bills_err}",
                    0, state["streak_days"], state["last_total"])
    grants, other = _today_bills(bills, today)
    granted_today = sum(g["points"] for g in grants)

    # ④ 余额（权威总量；查询失败沿用本地最近一次记录）
    total, _total_err = _balance(creds["access"])
    if total is not None:
        if total != state.get("last_total"):
            state["last_total"] = total
            rc.save_state(state)
    else:
        total = state["last_total"]

    # 结果判定：今日确有签到入账且本地尚未记过今天 → CLAIMED（连续天数 +1）
    first_today = state.get("last_date") != today
    if granted_today > 0 and first_today:
        state["streak_days"] = state.get("streak_days", 0) + 1
        state["last_date"] = today
        rc.save_state(state)
        daily = sum(g["points"] for g in grants if g["name"] == DAILY_GRANT_EVENT)
        bonus = granted_today - daily
        report = "签到成功"
        parts = []
        if daily:
            parts.append(f"+{daily} 积分")
        if bonus:
            parts.append(f"首次桌面登录奖励 +{bonus}")
        if parts:
            report += "，" + "，".join(parts)
        expiry_date = rx.daily_grant_expiry(bills, today)
        if expiry_date:
            report += f"，本笔 {expiry_date} 24:00 过期"
        if total:
            report += f"，余额 {total} 积分"
        out = _out("CLAIMED", report, granted_today,
                   state["streak_days"], total)
        return out

    if granted_today > 0:
        # 同日重跑：账单里还是同一批入账，不能重复计
        daily = sum(g["points"] for g in grants if g["name"] == DAILY_GRANT_EVENT)
        return _out("ALREADY", f"今日已签到（+{daily} 积分）",
                   0, state["streak_days"], total)

    # 今日无签到入账：不动本地状态 —— 当天晚些时候真入账了仍可被下一次运行记为 CLAIMED
    report = "今日未见签到积分入账（可能已领取）"
    if other > 0:
        report += f"；今日另有 {other} 分非签到入账（运营活动等）"
    if setting.get("error"):
        report += f"；发放触发器异常：{setting['error']}"
    if total:
        report += f"，余额 {total} 积分"
    return _out("ALREADY", report, 0, state["streak_days"], total)


def _refresh_only() -> dict:
    """纯凭据刷新：校验/续期登录态，不执行签到。"""
    state = rc.load_state()
    creds = rc.resolve_with_refresh()
    if creds is None:
        return _out("NO_AUTH", "小浣熊凭据未就绪（无登录态且续期失败），"
                               "请登录小浣熊桌面端或更新 auths/raccoon.json",
                    0, state["streak_days"], state["last_total"])
    where = {"desktop": "桌面端登录态", "env": "环境变量指定凭据",
             "work": "auths/raccoon.json", "mirror": "跨机漫游镜像"}.get(
        creds["source"], creds["source"])
    exp = time.strftime("%Y-%m-%d %H:%M", time.localtime(creds["exp"]))
    return _out("ALREADY", f"Token 已续期，凭据有效至 {exp}（{where}）",
                0, state["streak_days"], state["last_total"])


def main() -> int:
    out = _refresh_only() if "--refresh" in sys.argv else _run()
    print(json.dumps(out, ensure_ascii=False))
    if out["result"] in ("CLAIMED", "ALREADY"):
        return 0
    return 2 if out["result"] == "NO_AUTH" else 1


if __name__ == "__main__":
    sys.exit(main())
