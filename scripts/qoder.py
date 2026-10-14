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
    "total_credits": 0
  }

`streak_days` 恒为 0：上游没有可用的连续签到天数口径（legacy status 端点
已全地区停用），不猜、不伪造，交给账本按自然日自行累计。

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


def _out(result: str, report: str, today: int = 0, total: int = 0) -> dict:
    return {
        "result": result,
        "report": report,
        "today_credit": int(today),
        "streak_days": 0,
        "total_credits": int(total),
    }


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


def _no_claim(region: str, claimed: bool, risk_note: str | None, note: str = "") -> dict:
    if claimed:
        return _out("ALREADY", "今日已领取" + (f"（{note}）" if note else ""))
    if region == qd.GLOBAL:
        if risk_note:
            return _out("ALREADY",
                        f"未领取：{risk_note}。国际版签到需要本机安装 Qoder 客户端或 qodercli")
        return _out("ALREADY",
                    "上游暂未给该账号下发每日积分活动（活动窗口每天 10:00 开启，"
                    "若今天已在 Qoder 客户端领过则活动行会消失），明天 10 点后再试")
    return _out("ALREADY", "当前没有可领取的签到活动" + (f"（{note}）" if note else ""))


def _run() -> dict:
    creds = qd.resolve_credentials()
    if creds is None:
        where = "、".join(label for label, _path in qd.credential_sources())
        return _out("NO_AUTH",
                    f"未找到 Qoder 登录凭据（{where}、环境变量均无）。"
                    "运行一次 `python scripts/qoder.py --auth-token <accessToken>` 写入即可")

    region = creds["region"]
    headers = qd.sash_headers(creds["access"])
    payload, status, err = _fetch_campaigns(creds, headers)

    if payload is None and status in (401, 403):
        return _out("NO_AUTH", "登录态被上游拒绝（HTTP %d），请更新 accessToken" % status)
    if payload is None:
        result = "NETWORK" if status in (-1, 429, 502, 503, 504) else "ERROR"
        return _out(result, f"活动列表查询失败：{err or status}")

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
            return _out(result, f"活动列表查询失败（风控重拉）：{err or status}")

    rows = _campaign_rows(payload)
    target, claimed = _pick_target(rows)
    if target is None:
        return _no_claim(region, claimed, risk_note)

    campaign_id = str(target.get("campaignId") or "").strip()
    if not campaign_id:
        return _out("ERROR", "Qoder 签到活动缺少活动标识，无法领取")

    url = f"{qd.open_api(region)}{qd.CAMPAIGNS_PATH}/{campaign_id}/claim"
    body, status, err = qd.api_json("POST", url, headers, {}, timeout=qd.CLAIM_TIMEOUT)

    # 上游并发/重放时返回 409 + {"result":"ALREADY_CLAIMED"}，不是失败
    if body is None and status == 409:
        return _out("ALREADY", "今日已领取")
    if body is not None and str(body.get("result") or "") == RESULT_ALREADY_CLAIMED:
        return _out("ALREADY", "今日已领取")
    if body is None:
        if status in (401, 403):
            return _out("NO_AUTH", f"领取被拒绝（HTTP {status}），请更新 accessToken")
        result = "NETWORK" if status in (-1, 429, 502, 503, 504) else "ERROR"
        return _out(result, f"签到领取失败：{err or status}")

    if str(body.get("status") or "") != qd.STATUS_CLAIMED:
        return _out("ALREADY", f"签到未完成（上游状态 {body.get('status') or '未知'}）")
    if bool(body.get("replayed")):
        return _out("ALREADY", "今日已领取")

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
    expires_at = str(body.get("expiresAt") or "").strip()
    if expires_at:
        try:
            import qoder_credits as qc

            qc.note_grant(reward, expires_at)
        except Exception:
            pass  # 台账失败不影响签到本身

    state = _load_state()
    state["last_date"] = time.strftime("%Y-%m-%d")
    state["last_reward"] = reward
    state["region"] = region
    _save_state(state)

    return _out("CLAIMED", message, today=reward, total=0)


def _status() -> dict:
    """只查活动状态与连续活跃天数，不领取。"""
    creds = qd.resolve_credentials()
    if creds is None:
        where = "、".join(label for label, _path in qd.credential_sources())
        return _out("NO_AUTH", f"未找到 Qoder 登录凭据（{where}、环境变量均无）")

    region = creds["region"]
    headers = qd.sash_headers(creds["access"])
    payload, status, err = _fetch_campaigns(creds, headers)
    if payload is None:
        result = "NETWORK" if status in (-1, 429, 502, 503, 504) else "ERROR"
        return _out(result, f"活动列表查询失败：{err or status}")

    risk_note = None
    if region == qd.GLOBAL:
        uid = str(payload.get("uid") or "").strip() or creds.get("user_id") or ""
        headers, risk_note = qd.augmented_headers(region, creds["access"], uid)
        payload, status, err = _fetch_campaigns(creds, headers)
        if payload is None:
            return _out("ERROR", f"活动列表查询失败（风控重拉）：{err or status}")

    rows = _campaign_rows(payload)
    target, claimed = _pick_target(rows)
    region_label = "中国版" if region == qd.CN else "国际版"

    if target is not None:
        benefit = target.get("benefit") if isinstance(target.get("benefit"), dict) else {}
        amount = benefit.get("amount")
        return _out("ALREADY",
                    f"{region_label}：有可领取的签到活动（{amount or DEFAULT_REWARD:g} credits），"
                    "点签到即可领取")
    if claimed:
        return _out("ALREADY", f"{region_label}：今日已领取")
    if risk_note:
        return _out("ALREADY", f"{region_label}：未领取：{risk_note}")
    return _out("ALREADY", f"{region_label}：当前没有可领取的签到活动")


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
