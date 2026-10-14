"""Qoder 权威积分查询 · ClawCheckin 隐藏积分源

与 scripts/qoder.py（签到）分工：本脚本只查官方额度接口，不领取任何活动。
面板在每次签到后与启动时自动补跑本脚本，用权威余额刷新卡片上的「总积分」。

── 积分口径（2026-10 实测）──────────────────────────────────
GET {openapi}/api/v2/quota/usage  → {userQuota, addOnQuota, expiresAt}
  · userQuota  = 套餐额度（Pro 试用 300 credits）
  · addOnQuota = **签到领的那 100 Credits 落在这里**（官方面板「资源包」那一行）

总积分口径 = userQuota.remaining + addOnQuota.remaining。
只算 remaining（已消耗的不计入持有量，与本项目其余平台一致）。

── 两个桶的到期语义不同（都要进日历，所以分开标注来源）────────
后端字段与客户端渲染都确认过（`buildUsageItemGroups`）：

  · userQuota  → 客户端的 `renewsOn: formatRenewal(expiresAt)`，界面显示
    「将于 X 刷新」；官方错误提示也写「将在下个订阅周期自动重置」。
    即它是**周期额度**，到刷新日回满额，而不是作废。
  · addOnQuota → 客户端不给它渲染任何时间字段；它的到期时刻只出现在**领取
    响应**的 `expiresAt` 里（活动 validity 为领取后 30 天）。

本脚本把两者都按「该日起这笔额度不再可用」计入过期日历，但用 `source` 区分：
  · 资源包份额 → source「每日签到」（真正会过期的签到收益）
  · 套餐额度   → source「套餐额度（周期刷新）」（到期语义是换血，不是损失）

── 到期时间：为什么必须本地记账 ──────────────────────────────
`addOnQuota` **不暴露逐笔到期时间**，上游也没有「领取历史」接口可查（实测
campaigns/history、credits/grants 等路径全是 404）。唯一权威的到期时刻出现在
**领取响应**里：

  POST .../campaigns/{id}/claim → {..., "expiresAt": "<ISO 8601>"}

所以本脚本与 qoder.py 共用 data/qoder_grants.json 台账：
  · qoder.py 每次成功领取后追加一条 {date, amount, expiresAt}；
  · 本脚本读台账，把未过期的份额按到期日聚合，作为 expiring 上报给过期日历。

台账里没有记录时才退化为「只报余额、不报临期」——宁可不显示，也不按
validity.days 硬算（那是相对天数，基准时刻不同会算错到期日，虚报比不报更糟）。
套餐额度不需要台账：刷新日由顶层 `expiresAt` 直接给出。

凭据与请求层与 scripts/qoder.py 共用 scripts/_qoder_sync.py。

输出契约（末行 JSON）：
  {
    "result": "ALREADY|NO_AUTH|NETWORK|ERROR",
    "report": "Qoder 余额 400 credits（套餐 300；资源包 100），1 笔到期（最早 2026-11-11）",
    "today_credit": 0,
    "streak_days": 0,
    "total_credits": 400,
    "expiring": [{"date": "2026-11-11", "amount": 100, "source": "每日签到"}]
  }
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _qoder_sync as qd  # noqa: E402  同目录共享模块：凭据 / 请求层
import _syncio  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRANTS_PATH = os.path.join(PROJECT_ROOT, "data", "qoder_grants.json")
PATH_USAGE = "/api/v2/quota/usage"

# 过期日历只关心未来 28 天，台账里超过这个天数的份额不入册（列表会越来越长）
KEEP_DAYS = 40


def _to_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def load_grants() -> list[dict]:
    """读领取台账：[{date, amount, expiresAt}]。"""
    try:
        with open(GRANTS_PATH, encoding="utf-8") as handle:
            doc = json.load(handle)
        rows = doc.get("grants") if isinstance(doc, dict) else doc
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    except (OSError, ValueError):
        return []


def save_grants(rows: list[dict]) -> None:
    try:
        os.makedirs(os.path.dirname(GRANTS_PATH), exist_ok=True)
        _syncio.atomic_write_json(GRANTS_PATH, {"grants": rows[-KEEP_DAYS:]})
    except Exception:
        pass


def note_grant(amount: float, expires_at: str) -> None:
    """记一笔领取（由 qoder.py 领取成功后调用）。同一天同一到期日只记一次。"""
    if not expires_at:
        return
    rows = load_grants()
    date = time.strftime("%Y-%m-%d")
    for row in rows:
        if row.get("date") == date and row.get("expiresAt") == expires_at:
            return
    rows.append({"date": date, "amount": amount, "expiresAt": expires_at})
    save_grants(rows)


def _plan_refresh_date(expires_at) -> str | None:
    """把顶层的 `expiresAt`（毫秒时间戳或 ISO 串）换算成本地日期 YYYY-MM-DD。

    客户端用同一个字段渲染套餐额度的「将于 X 刷新」（见 buildUsageItemGroups），
    所以它就是套餐额度的周期刷新日。
    """
    if expires_at is None:
        return None
    try:
        if isinstance(expires_at, (int, float)) or (
            isinstance(expires_at, str) and expires_at.strip().isdigit()
        ):
            seconds = float(expires_at) / 1000.0        # 上游给的是毫秒
            stamp = time.localtime(seconds)
            return time.strftime("%Y-%m-%d", stamp)
        text = str(expires_at).strip()
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            return text[:10]
    except (OSError, OverflowError, ValueError):
        return None
    return None


def _expiring_from_grants(rows: list[dict], quota_remaining: int) -> list[dict]:
    """把台账按到期日聚合，只保留今天及以后的份额。

    `quota_remaining` 是 addOnQuota 的真实剩余：台账里的原始发放额可能已被
    消耗掉一部分，逐笔照搬会虚高。按「剩余 ≤ 台账合计」的比例等比摊薄，
    让日历上的到期总额等于真实会到期的量（与小浣熊积分源同一思路）。
    """
    today = time.strftime("%Y-%m-%d")
    buckets: dict[str, float] = {}
    for row in rows:
        expires_at = str(row.get("expiresAt") or "")
        day = expires_at[:10]
        if not day or day < today:      # 已过期的不进日历
            continue
        try:
            amount = float(row.get("amount"))
        except (TypeError, ValueError):
            continue
        if amount <= 0:
            continue
        buckets[day] = buckets.get(day, 0.0) + amount

    if not buckets:
        return []

    total = sum(buckets.values())
    scale = 1.0 if total <= 0 or quota_remaining <= 0 else min(1.0, quota_remaining / total)

    out = []
    for day in sorted(buckets):
        amount = buckets[day] * scale
        if amount >= 1:
            out.append({"date": day, "amount": int(round(amount)), "source": "每日签到"})
    return out


def main() -> int:
    creds = qd.resolve_credentials()
    if creds is None:
        print(json.dumps({
            "result": "NO_AUTH",
            "report": "未找到 Qoder 登录凭据（auths/qoder.json 为空），"
                      "运行 `python scripts/qoder.py --login` 授权一次即可",
            "today_credit": 0, "streak_days": 0, "total_credits": 0,
        }, ensure_ascii=False))
        return 2

    url = qd.open_api(creds["region"]) + PATH_USAGE
    payload, status, err = qd.api_json("GET", url, qd.sash_headers(creds["access"]))

    if payload is None or not isinstance(payload, dict):
        if status in (401, 403):
            result, report = "NO_AUTH", "Qoder 登录态被上游拒绝，请重新授权"
        elif status in (-1, 429, 502, 503, 504):
            result, report = "NETWORK", f"额度查询失败：{err or status}"
        else:
            result, report = "ERROR", f"额度查询失败：{err or status}"
        print(json.dumps({
            "result": result, "report": report,
            "today_credit": 0, "streak_days": 0, "total_credits": 0,
        }, ensure_ascii=False))
        return 1

    plan = payload.get("userQuota") if isinstance(payload.get("userQuota"), dict) else {}
    addon = payload.get("addOnQuota") if isinstance(payload.get("addOnQuota"), dict) else {}
    plan_remaining = _to_int(plan.get("remaining")) or 0
    addon_remaining = _to_int(addon.get("remaining")) or 0
    total = plan_remaining + addon_remaining

    if plan_remaining == 0 and addon_remaining == 0 and not plan and not addon:
        print(json.dumps({
            "result": "ERROR",
            "report": "额度响应缺少 userQuota / addOnQuota 字段（上游可能已调整接口）",
            "today_credit": 0, "streak_days": 0, "total_credits": 0,
        }, ensure_ascii=False))
        return 1

    # 明细：套餐 / 资源包（资源包即签到额度），只展示非零项
    parts = []
    if plan_remaining:
        parts.append(f"套餐 {plan_remaining}")
    if addon_remaining:
        parts.append(f"资源包 {addon_remaining}")

    expiring = _expiring_from_grants(load_grants(), addon_remaining)

    # 套餐额度（userQuota）：按「周期刷新日」进日历。
    #
    # 与资源包的性质不同 —— 它是**周期额度**，到刷新日回满额而不是作废（客户端
    # 界面同样把它渲染成「将于 X 刷新」，与资源包的「有效期至」区分开）。但既然
    # 要一并显示，就统一按「该日这笔额度不再可用」记账，让日历如实反映哪天额度
    # 换血；source 单独标注为「套餐额度（周期刷新）」，避免与签到收益混淆。
    plan_expiry = _plan_refresh_date(payload.get("expiresAt"))
    if plan_remaining > 0 and plan_expiry:
        expiring.append({
            "date": plan_expiry,
            "amount": plan_remaining,
            "source": "套餐额度（周期刷新）",
        })
        expiring.sort(key=lambda e: e["date"])

    report = f"Qoder 余额 {total} credits"
    if parts:
        report += "（" + "；".join(parts) + "）"
    if expiring:
        report += f"，{len(expiring)} 笔到期（最早 {expiring[0]['date']}）"

    print(json.dumps({
        "result": "ALREADY", "report": report,
        "today_credit": 0, "streak_days": 0, "total_credits": total,
        "expiring": expiring,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
