"""小浣熊（商汤）每日积分临期口径 · 共享模块

官方规则（2026-10 核实，官方客户端内置文案）：
  「每日积分每天 00:00 发放，默认当日 24:00 清零，当天登录后有效期延长至 7 天。」

账单接口（GET /api/web/points/v1/bills）的 daily_grant 每笔都带权威 expire_at：
  - 无登录日：daily_makeup 补发，expire_at = 当日 23:59:59（次日有等额清零负项）；
  - 有登录日：发放记录写入于当天首次登录时刻，expire_at = 发放日 +6 天的 23:59:59。
历史实现把每日积分硬编码为「当日 24 点清零」，登录日会提前 6 天误报到期；
本模块改为逐笔读取 expire_at，24 小时 / 7 天自适应，两种模式统一到账单口径。

来源标签必须跨天稳定：账本快照按 (date, amount, source) 去重，同一笔积分
从发放日到到期日标签若随运行日期漂移（登录延期的笔目到期当天被改标成
「当日 24:00 清零」），到期日那格会新旧两行同现、作废统计双计。现改为按
「发放日（created_at）vs 到期日」判定模式，与运行日期无关。

expire_at 形如 2026-10-14T23:59:59+08:00：去掉 +08:00 后缀按本地时区解析
（国内机器与服务端同时区），两种模式都以下午 24:00 收尾，展示为「24:00 过期」。
"""

from __future__ import annotations

import time


def _daily_grant_items(bills) -> list[dict]:
    """提取账单中的每日积分发放条目（biz_type == daily_grant 且 points > 0）。"""
    items = bills.get("items") if isinstance(bills, dict) else None
    rows = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("biz_type") or "") != "daily_grant":
            continue
        try:
            points = int(float(item.get("points")))
        except (TypeError, ValueError):
            continue
        if points <= 0:
            continue
        rows.append(item)
    return rows


def _expire_date(item: dict) -> str:
    """单笔发放的到期本地日期（YYYY-MM-DD）；expire_at 缺失/解析失败返回空。"""
    expire_raw = str(item.get("expire_at") or "")
    try:
        expire_ts = time.mktime(time.strptime(expire_raw[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(expire_ts))


def _grant_source(item: dict, date: str, today: str) -> str:
    """单笔发放的来源标签：按「发放日 vs 到期日」判定，与运行日期无关。

    官方两种模式（见模块头）：无登录日 daily_makeup 补发当日 24:00 清零，
    发放日 == 到期日；有登录日发放延至 7 天，到期日 = 发放日 +6 天。
    created_at 缺失时退回按运行日判断的老口径（仅影响无法追溯的笔目）。
    """
    created = str(item.get("created_at") or "")[:10]
    if created:
        return ("每日积分（当日 24:00 清零）" if created == date
                else "每日积分（当天登录，7 天有效）")
    return "每日积分（当日 24:00 清零）" if date == today else "每日积分（当天登录，7 天有效）"


def daily_expiring_from_bills(bills, today: str, daily_balance=None) -> list[dict]:
    """从账单生成每日积分临期快照：按 expire_at 逐笔聚合，有效期自适应。

    返回契约的 expiring 数组（date/amount/source，按日期升序），只含今天及
    未来的到期日：
      - 标签按「发放日 vs 到期日」判定（发放日== 到期日 →「当日 24:00 清零」，
        否则「当天登录，7 天有效」），与运行日期无关——同一笔积分从发放日到
        到期日标签不变，账本按 (date, amount, source) 去重才不会在到期日双计；
        发放日与到期日不同的笔目各自成行，同日不同模式不混算；
      - 过去日期一律不上报：已作废的历史笔目由到期当日那次运行捕获进快照，
        反复重报会重复计入作废统计；
      - expire_at 解析失败的笔目跳过（宁缺勿错，不回退到旧的硬编码口径）；
      - daily_balance 给定时按余额接口的 daily_points 摊薄（见摊薄说明）。

    摊薄说明（2026-10-09 修正）：
      账单里每笔 daily_grant 记的是**发放时原额**（如300×3=900），而余额接口
      `daily_points` 是每日积分钱包的**实际剩余**（如 867）。用户消耗积分后
      账单原额不变，直接相加会把已花掉的部分也算成「即将过期」，虚高。
      积分池扣减不绑定具体笔目，故按积分池实际的先进先出行为摊薄：**从最早到期的
      笔目起逐笔扣减**，花掉的积分落在快过期的那笔上，剩余积分完整保留在较晚到期
      的笔目上。三笔 300（10-13/14/15 到期）花 33 分、余额 867 时，结果为
      267/300/300。这样「14 天内将过期」= 真实会因到期而损失的积分，且合计恒等
      于 daily_points。

    daily_balance 为 None 时退回原额相加（接口没给该字段时的兜底）。

    余额为 0（2026-10-13 修正）：daily_points == 0 表示每日积分钱包已被消耗干净，
    真实的「会因到期损失」的积分为 0，直接返回空数组（旧实现返回等量 0 额行，
    日历会凭空多出「0 分」格、卡片仍标「有临期」）。注意 0 与 None 语义相反：
    调用方必须如实把 0 传进来，不可写成 `x if x > 0 else None`，否则退回原额
    相加，把账单里早已花掉的发放原额当成临期积分上报。

    余额 > 笔目合计（2026-10-12 修正）：账单窗口没覆盖全部未到期笔目时（拉取
    limit 不足 / 上游截断），笔目合计会小于 daily_points，此时差额是真实存在于
    每日积分钱包、且即将到期的积分。旧实现「余额覆盖即原额不变」把差额静默丢弃
    （实测 limit=40 只拿到未来 2 笔 600、余额 1024，日历漏报 424），现改为把差额
    挂到最早到期日，合计恒等于 daily_points。账单一个 daily_grant 都没有时仍返回
    空——上游没给数据时不凭空捏造到期日。
    """
    agg: dict[tuple[str, str], int] = {}
    for item in _daily_grant_items(bills):
        date = _expire_date(item)
        if not date or date < today:
            continue
        try:
            points = int(float(item.get("points")))
        except (TypeError, ValueError):
            continue
        key = (date, _grant_source(item, date, today))
        agg[key] = agg.get(key, 0) + points

    rows = [{"date": d, "amount": pts, "source": src}
            for (d, src), pts in sorted(agg.items())]
    return _cap_to_daily_balance(rows, daily_balance)


def _cap_to_daily_balance(rows: list[dict], daily_balance) -> list[dict]:
    """把逐笔发放原额按每日积分钱包实际余额摊薄。

    rows 按到期日升序（调用方已排序）。

    摊薄方向：**从最早到期的一笔开始扣减**。积分池先进先出——花掉的积分优先
    扣最早过期那笔（快过期的先花掉，避免作废），所以剩余积分集中在**较晚到期**
    的笔目上。例：三笔 300（10-13/14/15 到期）已花 33 分、余额 867时，
    正确结果是 267/300/300 —— 10-13 被扣 33，两笔较晚到期的原额完整保留。

    反过来（从最晚到期开始扣）会把消耗全甩给快过期的笔目，与积分池实际行为
    相反，日历上表现为「越早到期剩得越多」，明显不合常理。
    """
    if daily_balance is None:
        return rows
    try:
        remaining = int(daily_balance)
    except (TypeError, ValueError):
        return rows

    total = sum(r["amount"] for r in rows)
    if remaining >= total:
        # 余额 ≥ 笔目合计：账单拉全时两者应相等。若余额仍大于合计，说明账单窗口
        # 没覆盖到全部未到期笔目（limit 不够 / 上游截断），差额是真实存在于每日
        # 积分钱包、且即将到期的积分，绝不能静默丢弃——挂到最早到期日，如实进
        # 日历（宁可到期日早报一天，也不要让用户看不到这部分会作废的分）。
        #
        # 仅在有笔目时兜底：账单里一个 daily_grant 都没有说明上游没给数据，此时
        # 凭空造一条等于捏造积分与到期日，仍返回空（缺失即该平台不参与统计）。
        gap = remaining - total
        if gap > 0 and rows:
            first = dict(rows[0])
            first["amount"] += gap
            rows = [first] + list(rows[1:])
        return rows
    if remaining <= 0:
        # 余额接口明确为 0：每日积分钱包一分为空，没有任何一笔会因到期而损失，
        # 返回空数组（清空该平台的临期快照）。
        #
        # 旧实现返回等量「0 额行」，想法是「空数组会残留旧快照」，但账本并不这样
        # 工作（ledger._update_snapshots_locked：result=ALREADY 的正常回报照常
        # 覆盖——未来行按平台整体重建、今天行打 live=0），残留不会发生；而 0 额行
        # 会真的进日历占格（显示「x 分 · 0」）、平台卡片仍标「有临期」，把「钱包
        # 已空」误报成「有临期积分」，正是 2026-10-13 真机上「总积分全是明年才过期
        # 的奖励分，本周却天天有小浣熊临期」的来源之一。
        return []

    # 从最早到期的一笔开始扣：消耗掉的积分落在最早的笔目上
    consumed = total - remaining          # 已被花掉的积分
    out = []
    for r in rows:
        if consumed <= 0:
            out.append(r)
            continue
        amount = r["amount"]
        if amount <= consumed:
            # 这笔被扣光了，剩余积分全部落在更晚到期的笔目上
            consumed -= amount
            continue
        out.append({"date": r["date"], "amount": amount - consumed, "source": r["source"]})
        consumed = 0
    return out


def daily_grant_expiry(bills, today: str) -> str:
    """今天发放的每日积分的到期日期（YYYY-MM-DD）；今天没有发放记录返回空。

    供签到回报用：同一服务端口径下两种模式的到期时刻都是 23:59:59，
    展示层统一写成「{date} 24:00 过期」。
    """
    best = ""
    for item in _daily_grant_items(bills):
        created = str(item.get("created_at") or "")
        if created[:10] != today:
            continue
        date = _expire_date(item)
        if date and date > best:
            best = date
    return best
