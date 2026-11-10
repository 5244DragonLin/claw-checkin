"""Qoder 每日签到（活动领取链路）· ClawCheckin 接入脚本

协议参考：https://github.com/aimod-cc/agent2api
（core/providers/qoder/checkin.rs —— Qoder 两版签到链路的完整实现）

── Qoder 的每日权益长什么样 ─────────────────────────────────
Qoder **没有**传统的「一天一次签到接口」。每日权益以**活动（campaign）**
形式下发，走 sash 一族接口：

  GET  {openapi}/sash/api/v1/me/campaigns?forceRefresh=true
   → {uid, showCampaign, claimable, campaigns:[{campaignId, actionType,
        claimStatus, startAt, endAt, benefit:{kind:"CREDITS", amount:100,
        validity:{mode:"RELATIVE_DAYS", days:30}}}]}
  POST {openapi}/sash/api/v1/me/campaigns/{campaignId}/claim   body {}
   → {status:"CLAIMED", replayed, benefit:{...}, claimedAt, grantedAt, expiresAt}

实测口径（agent2api 在真实账号上验证过）：
  · 活动窗口是中国时间**当天 10:00 → 次日 09:59**（官方「每天 10:00 刷新」）；
    窗口边界以活动行自带的 `startAt` / `endAt` 为准（本地小时数只是兜底），
    因为「开窗前读到的 CLAIMED 属于昨天那一窗」这个判断全靠它；
  · 领取的钱落在 `/api/v2/quota/usage` 的 `addOnQuota` 桶里（余额面板「资源包」
    那一行），不是普通 Credits 余额，所以本脚本不去猜余额，只报领到的额度；
  · **领取后活动行不消失**，`claimStatus` 从 `CLAIMABLE` 变 `CLAIMED` ——
    「今天领没领」直接读活动列表即可，无需本地记账；
  · 顶层 `claimable` 不能当判据（领完后可能仍为 true，那是另一条促销活动），
    必须按 `actionType == "CLAIM_BENEFIT"` 逐行筛。

两个地区的差别：
  · 中国版（openapi.qoder.com.cn）：直接就能领；
  · 国际版（openapi.qoder.sh）：服务端**只对带设备风控头**的请求下发
    「每天领 100 Credits」活动，风控三件套由本机 Qoder 客户端 / qodercli 的
    UMID 组件生成（见 `_qoder_sync.qoder_risk`）。缺组件时给中性说明而不是报错。
  · 两个地区的 legacy `daily-check-in` 路径都已停用，本脚本一次都不碰。

「没活动」不是错误：中国版 Free 套餐实测没有活动下发（Pro / 试用才有），
国际版缺 UMID 组件时也看不到积分活动。这两种情况都返回 ALREADY + 说明，
不要把「今天没得领」记成失败或已签到。

输出契约（末行 JSON，与其余平台一致）：
  {
    "result": "CLAIMED|ALREADY|NETWORK|ERROR|NO_AUTH",
    "report": "可读汇报",
    "today_credit": 100,
    "streak_days": 0,
    "total_credits": 0,
    "signed_today": true        // 仅当「今天这一窗确实已领」时置 true；
                                // 开窗前查到昨天那窗的 CLAIMED 时显式置 false
  }

`signed_today` 三态（与 runner 契约一致）：true = 今天已领；false = 今天确实
没领（例如 10:00 前窗口还没刷新）；字段缺省 = 说不准。开窗前必须显式给 false：
旧版本这里只报「今日已领取」，面板据此把卡片标成已签到、按钮变「今日已完成」，
用户以为今天的分已经到手，其实一分没进账（2026-11-10 实测的误导来源）。

`streak_days`：上游没有连续签到天数字段（legacy status 端点已全地区停用，
实测恒回 DISABLED/0），活动列表也只吐当天一个窗口、没有历史。因此天数在
本地累计（与 AutoClaw / 小浣熊同口径）：首次从领取台账回填，之后每确认一天
领取 +1、同日不双计。口径是「本地有记录的领取天数」——在 Qoder 客户端里
领的日子，只有脚本当天读到 CLAIMED 状态才会计入。

凭据：客户端登录态是 Electron safeStorage 加密的，脚本不解密客户端密文，
按 `auths/qoder.json` → `auths/qoder_sync.json` → 环境变量的优先级取；接入时
用 `--auth-token <accessToken>` 写一次即可（详见脚本内 `--help` 说明）。
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _qoder_sync as qd  # noqa: E402  同目录共享模块：凭据 / 请求层 / 风控身份
import _syncio  # noqa: E402

STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "qoder_state.json")

DEFAULT_REWARD = 100.0
RESULT_ALREADY_CLAIMED = "ALREADY_CLAIMED"
# 活动窗口：当天 10:00 开窗 → 次日 09:59 关窗。10 点前读到的「已领取」是
# 昨天那一窗的成绩，不能算成今天已签（面板会因此把今天误报成已完成）。
# 这个小时数只是兜底：活动行自带 startAt/endAt，能读到就以它为准（见
# _window_open），以免官方改了刷新时刻后本地判断失真。
WINDOW_OPEN_HOUR = 10


def _start_ts(row: dict) -> float | None:
    """活动行的窗口开始时刻（秒级时间戳）；没有可用时间戳返回 None。"""
    try:
        start = float(row.get("startAt"))
    except (TypeError, ValueError):
        return None
    return start / 1000.0 if start > 1e11 else start        # 兼容毫秒


def _row_starts_today(row: dict) -> bool | None:
    """活动行的整段窗口是否落在今天（True 今天的、False 更早的、None 无时间戳）。"""
    start = _start_ts(row)
    if start is None:
        return None
    today = time.strftime("%Y-%m-%d")
    try:
        return time.strftime("%Y-%m-%d", time.localtime(start)) == today
    except (OSError, OverflowError, ValueError):
        return None


def _window_open(rows: list[dict]) -> bool:
    """今天这一窗是否已经开过（决定上游的「已领取」算不算今天的成绩）。

    口径按可信度排序：
      1. 有 CLAIM_BENEFIT 行且带 startAt：只要存在 startAt 落在今天的行，就看
         此刻是否已过它；一条都没有今天的，说明上游还没下发今天这一窗 → False
         （此时列表里的 CLAIMED 必然是昨天那一窗的）。
      2. 行里没有可用时间戳（上游改结构）：退回本地小时数 ≥ 10。
    宁可保守判「没开窗」，也不要谎报已签——谎报会让面板把今天标成已完成。
    """
    saw_stamp = False
    for row in rows:
        if str(row.get("actionType") or "") != qd.ACTION_CLAIM_BENEFIT:
            continue
        today = _row_starts_today(row)
        if today is None:
            continue
        saw_stamp = True
        if today:
            return time.time() >= _start_ts(row)
    if saw_stamp:
        return False                # 有行但都属于更早的窗口：今天还没开窗
    return time.localtime().tm_hour >= WINDOW_OPEN_HOUR


def _out(result: str, report: str, today: int = 0, total: int = 0,
         streak: int = 0, signed_today: bool | None = None) -> dict:
    out = {
        "result": result,
        "report": report,
        "today_credit": int(today),
        "streak_days": int(streak),
        "total_credits": int(total),
    }
    if signed_today is not None:
        out["signed_today"] = bool(signed_today)
    return out


def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as handle:
            doc = json.load(handle)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        _syncio.atomic_write_json(STATE_PATH, state)
    except Exception:
        pass


def _streak_days(state: dict) -> int:
    """本地累计领取天数（上游没有该字段，与 AutoClaw/小浣熊同口径在本地计）。

    首次从领取台账回填历史：台账里的日期去掉「今天」的部分，再加上
    「今天是否已确认领取」（last_date），避免明天领的时候把今天双计。
    之后只增不减，存在 state 里；台账被清理也不影响已有计数。
    """
    if "streak_days" in state:
        return int(state["streak_days"] or 0)
    days: set[str] = set()
    try:
        import qoder_credits as qc

        for row in qc.load_grants():
            day = str(row.get("date") or "")
            if len(day) == 10 and day[4] == "-" and day[7] == "-":
                days.add(day)
    except Exception:
        pass  # 台账读不到就从 0 起数，不影响签到本身
    streak = len(days)
    if time.strftime("%Y-%m-%d") in days:
        streak -= 1  # 今天那份由下面的确认逻辑记，不在这里预支
    if state.get("last_date") == time.strftime("%Y-%m-%d"):
        streak += 1
    state["streak_days"] = streak
    return streak


def _confirm_day(state: dict) -> int:
    """确认今天确有领取（自己领到 / 上游确认今天窗口已领）：累计 +1，每天只记一次。"""
    _streak_days(state)  # 确保历史已回填
    today = time.strftime("%Y-%m-%d")
    if state.get("last_date") != today:
        state["streak_days"] = int(state.get("streak_days") or 0) + 1
        state["last_date"] = today
        _save_state(state)
    return int(state.get("streak_days") or 0)


def _campaign_rows(payload: dict) -> list[dict]:
    rows = payload.get("campaigns")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _fetch_campaigns(creds: dict, headers: list[tuple[str, str]]):
    url = qd.open_api(creds["region"]) + qd.CAMPAIGNS_PATH + qd.CAMPAIGNS_QUERY
    return qd.api_json("GET", url, headers)


def _pick_target(rows: list[dict]) -> tuple[dict | None, bool]:
    """返回 (可领取的活动行, 是否今天已领过)。只认 CLAIM_BENEFIT。"""
    claimable = None
    claimed = False
    for row in rows:
        if str(row.get("actionType") or "") != qd.ACTION_CLAIM_BENEFIT:
            continue
        status = str(row.get("claimStatus") or "")
        if status == qd.STATUS_CLAIMABLE and claimable is None:
            claimable = row
        elif status == qd.STATUS_CLAIMED:
            claimed = True
    return claimable, claimed


def _no_claim(region: str, claimed: bool, risk_note: str | None,
              streak: int, note: str = "", window_open: bool = True) -> dict:
    if claimed:
        if window_open:
            return _out("ALREADY", f"今日已领取（连续 {streak} 天）"
                        + (f"（{note}）" if note else ""),
                        streak=streak, signed_today=True)
        # 开窗前：上游那条 CLAIMED 是昨天那一窗的成绩，今天还没开始领。
        # 必须显式 signed_today=False，否则面板会把今天标成已完成，
        # 而实际上今天这 100 credits 根本还没到账（2026-11-10 的误导）
        return _out("ALREADY", f"今天还没开窗（10:00 后可再领）；"
                    f"刚读到的是昨天已领取（连续 {streak} 天）",
                    streak=streak, signed_today=False)
    if region == qd.GLOBAL:
        # 「没有活动行」不等于「今天没领」：国际版缺 UMID 组件时本来就不下发，
        # 中国版也可能已在客户端领过而列表不再给可领行。所以这两种情况不置
        # signed_today，留给面板按「未签到」显示，不猜
        if risk_note:
            return _out("ALREADY",
                        f"未领取：{risk_note}。国际版签到需要本机安装 Qoder 客户端或 qodercli",
                        streak=streak)
        return _out("ALREADY",
                    "上游暂未给该账号下发每日积分活动（活动窗口每天 10:00 开启，"
                    "若今天已在 Qoder 客户端领过则活动行会消失），明天 10 点后再试",
                    streak=streak)
    return _out("ALREADY", "当前没有可领取的签到活动" + (f"（{note}）" if note else ""),
                streak=streak)


def _claimed_already(state: dict, streak: int, carry_over: bool) -> dict:
    """上游回报「这条活动已经领过了」。

    `carry_over` 表示这条活动属于上一窗（开窗前读到/领到的），那不是今天的成绩：
    不记今天的连续天数，并显式 signed_today=False，面板才不会把今天标成已完成。
    """
    streak = streak if carry_over else _confirm_day(state)
    return _out("ALREADY",
                (f"昨天那一窗已领取（连续 {streak} 天）" if carry_over
                 else f"今日已领取（连续 {streak} 天）"),
                streak=streak, signed_today=not carry_over)


def _note_grant_from(body: dict) -> None:
    """把一次 claim 响应里的权威到期时刻记进领取台账。

    `expiresAt` 只出现在 claim 响应里（活动列表与额度接口都不给逐笔到期时间，
    上游也没有领取历史接口），所以过期日历全靠这个台账。记失败不影响签到本身。
    """
    if not isinstance(body, dict):
        return
    expires_at = str(body.get("expiresAt") or "").strip()
    if not expires_at:
        return
    benefit = body.get("benefit") if isinstance(body.get("benefit"), dict) else {}
    try:
        reward = float(benefit.get("amount"))
    except (TypeError, ValueError):
        reward = DEFAULT_REWARD
    try:
        import qoder_credits as qc

        qc.note_grant(reward, expires_at)
    except Exception:
        pass


def _grant_known(today: str) -> bool:
    """台账里今天是否已有一笔领取记录（用于避免重复补记）。

    台账只存 {date, amount, expiresAt}，没有 campaignId，所以按「发放日=今天」
    判断。同一天最多领一笔（活动窗口一天一开），这个粒度足够。
    """
    try:
        import qoder_credits as qc

        return any(str(r.get("date") or "") == today for r in qc.load_grants())
    except Exception:
        return True     # 台账读不到时按「已有」处理，不打扰上游


def _backfill_grant(creds: dict, headers: list[tuple[str, str]], rows: list[dict]) -> None:
    """台账缺了「今天这一窗」的笔目时，重放一次 claim 把它补回来。

    背景（2026-11-10 真机）：脚本读到活动已是 CLAIMED 就直接报「今日已领取」、
    不再调 claim —— 而 `expiresAt` 只在 claim 响应里，于是那些日子在台账里
    一笔都没有。真机后果：资源包 3000 分的台账只有 17 笔（1700 分），过期日历
    靠「余额填最早笔目」硬凑，把 100 分报成 11-11 到期（实际是 12-10）。

    上游对已领活动重放是幂等的（回 `replayed: true`，不重复加分），响应里带
    权威的 `expiresAt`，所以补一次 claim 既安全又能拿到到期时刻。只在台账里
    确实没有今天这一笔时才补，避免每次运行都白打一发。
    """
    today = time.strftime("%Y-%m-%d")
    if _grant_known(today):
        return
    if not any(str(r.get("actionType") or "") == qd.ACTION_CLAIM_BENEFIT
               and str(r.get("claimStatus") or "") == qd.STATUS_CLAIMED
               and _row_starts_today(r) is not False
               for r in rows):
        return              # 没有「今天这一窗且已领」的活动行：没什么可补的
    for row in rows:
        if str(row.get("actionType") or "") != qd.ACTION_CLAIM_BENEFIT:
            continue
        if str(row.get("claimStatus") or "") != qd.STATUS_CLAIMED:
            continue
        if _row_starts_today(row) is False:
            continue        # 昨天那一窗的，不补（今天开窗后会领到新的）
        campaign_id = str(row.get("campaignId") or "").strip()
        if not campaign_id:
            continue
        url = f"{qd.open_api(creds['region'])}{qd.CAMPAIGNS_PATH}/{campaign_id}/claim"
        body, _status, _err = qd.api_json("POST", url, headers, {},
                                         timeout=qd.CLAIM_TIMEOUT)
        _note_grant_from(body or {})
        return


def _run() -> dict:
    creds = qd.resolve_credentials()
    if creds is None:
        where = "、".join(label for label, _path in qd.credential_sources())
        return _out("NO_AUTH",
                    f"未找到 Qoder 登录凭据（{where}、环境变量均无）。"
                    "运行一次 `python scripts/qoder.py --auth-token <accessToken>` 写入即可")

    region = creds["region"]
    state = _load_state()
    streak = _streak_days(state)
    _save_state(state)  # 首次回填的天数落盘，之后只增不减（台账有 40 条上限会裁剪）
    headers = qd.sash_headers(creds["access"])
    payload, status, err = _fetch_campaigns(creds, headers)

    if payload is None and status in (401, 403):
        return _out("NO_AUTH", "登录态被上游拒绝（HTTP %d），请更新 accessToken" % status,
                    streak=streak)
    if payload is None:
        result = "NETWORK" if status in (-1, 429, 502, 503, 504) else "ERROR"
        return _out(result, f"活动列表查询失败：{err or status}", streak=streak)

    # 国际版：先拿顶层 uid，叠风控头重拉一次（中国版不需要）
    risk_note = None
    if region == qd.GLOBAL:
        uid = str(payload.get("uid") or "").strip()
        if not uid:
            uid = creds.get("user_id") or ""
        headers, risk_note = qd.augmented_headers(region, creds["access"], uid)
        payload, status, err = _fetch_campaigns(creds, headers)
        if payload is None:
            result = "NETWORK" if status in (-1, 429, 502, 503, 504) else "ERROR"
            return _out(result, f"活动列表查询失败（风控重拉）：{err or status}", streak=streak)

    rows = _campaign_rows(payload)
    window_open = _window_open(rows)
    target, claimed = _pick_target(rows)
    if target is None:
        # 上游确认今天窗口已领（可能是在 Qoder 客户端里领的）：开窗后的
        # 「已领取」才是今天那一窗，计入本地天数；开窗前读到的是昨天的成绩
        if claimed and window_open:
            streak = _confirm_day(state)
            # 台账缺今天这一笔就补记：`expiresAt` 只在 claim 响应里，而这条
            # 路径从不调 claim——不补的话过期日历会缺笔目、把余额错挂到旧日期
            _backfill_grant(creds, headers, rows)
        return _no_claim(region, claimed, risk_note, streak, window_open=window_open)

    campaign_id = str(target.get("campaignId") or "").strip()
    if not campaign_id:
        return _out("ERROR", "Qoder 签到活动缺少活动标识，无法领取")

    # 可领取的那一行本身是否属于今天这一窗。开窗前能领到的行属于「昨天那一窗」
    # （窗口要到 09:59 才关）：真能领到就是真收益，照领不误，但文案要说清这不是
    # 今天的份，免得用户以为今天已经领完、后面不用再跑。行里没时间戳时退回
    # 「今天还没开窗 ⇒ 算上一窗」，宁可多提醒一句也不谎报今日已签。
    row_today = _row_starts_today(target)
    carry_over = (not row_today) if row_today is not None else (not window_open)

    url = f"{qd.open_api(region)}{qd.CAMPAIGNS_PATH}/{campaign_id}/claim"
    body, status, err = qd.api_json("POST", url, headers, {}, timeout=qd.CLAIM_TIMEOUT)

    # 上游并发/重放时返回 409 + {"result":"ALREADY_CLAIMED"}，不是失败。
    # carry_over（上一窗）同一个道理：那是昨天的成绩，不记今天的连续天数
    if body is None and status == 409:
        return _claimed_already(state, streak, carry_over)
    if body is not None and str(body.get("result") or "") == RESULT_ALREADY_CLAIMED:
        return _claimed_already(state, streak, carry_over)
    if body is None:
        if status in (401, 403):
            return _out("NO_AUTH", f"领取被拒绝（HTTP {status}），请更新 accessToken",
                        streak=streak)
        result = "NETWORK" if status in (-1, 429, 502, 503, 504) else "ERROR"
        return _out(result, f"签到领取失败：{err or status}", streak=streak)

    if str(body.get("status") or "") != qd.STATUS_CLAIMED:
        return _out("ALREADY", f"签到未完成（上游状态 {body.get('status') or '未知'}）",
                    streak=streak)
    if bool(body.get("replayed")):
        # 重放（今天已领过）同样带回权威 expiresAt：顺手补一笔台账，避免这条
        # 路径漏记（它常年是「已领」时唯一能拿到到期时刻的地方）
        _note_grant_from(body)
        return _claimed_already(state, streak, carry_over)

    benefit = body.get("benefit") if isinstance(body.get("benefit"), dict) else {}
    try:
        reward = float(benefit.get("amount"))
    except (TypeError, ValueError):
        reward = DEFAULT_REWARD
    validity = benefit.get("validity") if isinstance(benefit.get("validity"), dict) else {}
    try:
        days = float(validity.get("days"))
    except (TypeError, ValueError):
        days = 0.0

    message = f"获得 {reward:g} credits" + (f"，{days:g} 天有效" if days > 0 else "")

    # 记一笔领取台账（到期时刻 → 过期日历）。
    # `expiresAt` 是上游返回的权威到期时刻 —— addOnQuota 接口不暴露逐笔到期时间，
    # 上游也没有领取历史接口，所以这个时刻只能在这里顺手记下来。
    # 交给 qoder_credits 读（本脚本只负责记，不参与总积分口径）。
    _note_grant_from(body)

    # 开窗前领到的是「昨天那一窗」的补领：积分真到账，但那是昨天的成绩，
    # 不能算成今天已签，也不该把本地连续天数按今天 +1（否则明天再领会双计）
    streak = streak if carry_over else _confirm_day(state)
    state["last_reward"] = reward
    state["region"] = region
    _save_state(state)

    # 开窗前领到的是昨天那一窗的补领，不是今天的份额：照报 CLAIMED（真到账），
    # 但 signed_today=False，别让面板把今天标成已完成
    if carry_over:
        return _out("CLAIMED", f"补领昨天那一窗：{message}（连续 {streak} 天）；"
                    "今天的份额 10:00 开窗后可再领",
                    today=reward, total=0, streak=streak, signed_today=False)
    return _out("CLAIMED", f"{message}（连续 {streak} 天）", today=reward, total=0,
                streak=streak, signed_today=True)


def _status() -> dict:
    """只读查询活动状态与连续活跃天数，不领取。

    `--status` 也用于面板的状态核查：claimed=True 即「平台侧今天已领」（置
    signed_today），target 存在即「明确还没领」（置 false）。两种都没有时（今天
    没下发活动）无法判断，不置该字段——留给面板按「未签到」显示，不猜。
    """
    creds = qd.resolve_credentials()
    if creds is None:
        where = "、".join(label for label, _path in qd.credential_sources())
        return _out("NO_AUTH", f"未找到 Qoder 登录凭据（{where}、环境变量均无）")

    region = creds["region"]
    state = _load_state()
    streak = _streak_days(state)
    headers = qd.sash_headers(creds["access"])
    payload, status, err = _fetch_campaigns(creds, headers)
    if payload is None:
        result = "NETWORK" if status in (-1, 429, 502, 503, 504) else "ERROR"
        return _out(result, f"活动列表查询失败：{err or status}", streak=streak)

    risk_note = None
    if region == qd.GLOBAL:
        uid = str(payload.get("uid") or "").strip() or creds.get("user_id") or ""
        headers, risk_note = qd.augmented_headers(region, creds["access"], uid)
        payload, status, err = _fetch_campaigns(creds, headers)
        if payload is None:
            return _out("ERROR", f"活动列表查询失败（风控重拉）：{err or status}",
                        streak=streak)

    rows = _campaign_rows(payload)
    window_open = _window_open(rows)
    target, claimed = _pick_target(rows)
    region_label = "中国版" if region == qd.CN else "国际版"

    if target is not None:
        benefit = target.get("benefit") if isinstance(target.get("benefit"), dict) else {}
        amount = benefit.get("amount")
        return _out("ALREADY",
                    f"{region_label}：有可领取的签到活动（{amount or DEFAULT_REWARD:g} credits），"
                    "点签到即可领取",
                    streak=streak, signed_today=False)
    if claimed:
        # 开窗前（10:00 前）窗口还没刷新：此刻的「已领取」是昨天那一窗的，今天
        # 还没开始。必须显式 signed_today=False——旧版这里不置该字段，面板虽按
        # 「不猜」显示未签到，但签到路径却报「今日已领取」，两条口径打架
        if window_open:
            return _out("ALREADY", f"{region_label}：今日已领取（连续 {streak} 天）",
                        streak=streak, signed_today=True)
        return _out("ALREADY",
                    f"{region_label}：昨日已领取，今天 10:00 开窗后可再领",
                    streak=streak, signed_today=False)
    if risk_note:
        return _out("ALREADY", f"{region_label}：未领取：{risk_note}", streak=streak)
    return _out("ALREADY", f"{region_label}：当前没有可领取的签到活动", streak=streak)


def _device_login(argv: list[str]) -> int:
    """--login：走 Qoder 官方设备授权，拿 accessToken 写进 auths/qoder.json。

    流程与 agent2api 的 PKCE 设备授权同构：打印授权页地址 → 用户在浏览器点
    授权 → 脚本轮询拿 token。全程不碰客户端加密凭据。
    """
    region = qd.CN
    wait_seconds = 300
    index = 0
    while index < len(argv):
        if argv[index] == "--region" and index + 1 < len(argv):
            region = qd.GLOBAL if argv[index + 1].strip().lower() in ("global", "intl") else qd.CN
            index += 2
            continue
        if argv[index] == "--wait" and index + 1 < len(argv):
            try:
                wait_seconds = max(30, int(argv[index + 1]))
            except ValueError:
                pass
            index += 2
            continue
        index += 1

    url, nonce, verifier = qd.device_login_url(region)
    print("请用浏览器打开下面的地址，登录并授权（脚本会在后台等待）：")
    print(url)
    print()
    print(f"等待授权中（最多 {wait_seconds} 秒）…", flush=True)

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        creds = qd.poll_device_token(region, nonce, verifier)
        if creds:
            if qd.save_credentials(creds):
                label = "中国版" if region == qd.CN else "国际版"
                print(json.dumps(_out("ALREADY",
                                      f"Qoder {label} 登录成功，凭据已写入 auths/qoder.json"
                                      + (f"（账号 {creds['name']}）" if creds.get("name") else "")),
                                 ensure_ascii=False))
                return 0
            print(json.dumps(_out("ERROR", "凭据写入 auths/ 失败，请检查目录权限"),
                             ensure_ascii=False))
            return 1
        time.sleep(3)

    print(json.dumps(_out("NETWORK", "等待授权超时，请重新运行 --login"), ensure_ascii=False))
    return 1


def _write_token(argv: list[str]) -> int:
    """--auth-token 写入凭据（可选 --region / --user-id）。"""
    token = ""
    region = qd.CN
    user_id = ""
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--auth-token" and index + 1 < len(argv):
            token = argv[index + 1].strip()
            index += 2
            continue
        if item == "--region" and index + 1 < len(argv):
            region = qd.GLOBAL if argv[index + 1].strip().lower() in ("global", "intl") else qd.CN
            index += 2
            continue
        if item == "--user-id" and index + 1 < len(argv):
            user_id = argv[index + 1].strip()
            index += 2
            continue
        index += 1

    if not token:
        print(json.dumps(_out("ERROR", "--auth-token 缺少 accessToken 值"), ensure_ascii=False))
        return 1

    creds = {"region": region, "access": token, "refresh": "", "user_id": user_id}
    saved = qd.save_credentials(creds)
    if not saved:
        print(json.dumps(_out("ERROR", "凭据写入 auths/ 失败，请检查目录权限"), ensure_ascii=False))
        return 1
    label = "中国版" if region == qd.CN else "国际版"
    print(json.dumps(_out("ALREADY", f"Qoder {label} accessToken 已写入 auths/qoder.json"),
                     ensure_ascii=False))
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    if "--login" in argv:
        return _device_login(argv)
    if "--auth-token" in argv:
        return _write_token(argv)

    out = _status() if "--status" in argv else _run()
    print(json.dumps(out, ensure_ascii=False))
    if out["result"] in ("CLAIMED", "ALREADY"):
        return 0
    return 2 if out["result"] == "NO_AUTH" else 1


if __name__ == "__main__":
    sys.exit(main())
