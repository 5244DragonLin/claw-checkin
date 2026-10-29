"""Trae 每日签到（SOLO 通道）· ClawCheckin 接入脚本

协议参考：https://github.com/aimod-cc/agent2api
（desktop-tauri/.../core/providers/trae/checkin.rs —— 签到状态机的完整实现）

── Trae 的每日签到长什么样 ──────────────────────────────────
Trae（SOLO 通道）每日签到打的是 ug 一族端点，请求体恒为空对象：

  POST https://api.trae.cn/trae/api/v2/ug/checkin_credits/status   body {}
  POST https://api.trae.cn/trae/api/v2/ug/checkin_credits/claim    body {}

一轮 = 状态查询 → 领取 → 同设备号回查，恒 3 发（未签到时）。判「真领到」
**不看** claim 的 code:0 —— 当日已签过的账号对任何 device_id 都回 code:0
（假成功陷阱）—— 只看同 device_id 回查后 checked_in / did_checked_in 有
没有翻转。翻转了才算 Claimed；没翻转报失败，绝不报「签到成功」。

── 设备号纪律（风控红线）────────────────────────────────────
每轮新生成一个 16 位纯数字串，三发之间保持不变；复用旧号、用登录那套
hex32 设备号或 machineId 去打签到，都是官方风控画像里的可疑项。凭据里
没有 deviceId（多半是手工粘贴的）时**硬拦**：这类凭据没有设备绑定，
签到正好落在「绑定不一致」的高危画像上，不如直接提示重新登录。

── 业务码语义（只有这三码有实测依据）─────────────────────────
  0     成功（但见上面的假成功陷阱）
  9074  当前参与用户太多 —— **换一张新设备号重试一轮**。
        本仓三次实测都是换号后通过（同一令牌、同一套画像、同一账号），
        所以按「device 维度限流 / 名额时间窗」处置；仍不过再如实报失败。
        重试前等几秒，避免贴着上一轮的计数连打。换号重试与下面的换发
        重放各最多一次，不在一条失败的链上无限试。
  1001  服务端已作废这张 JWT（本地到期时间看不出来）—— 处置是换发新
        令牌后重放一轮；换发也失败就提示重新登录。
其它非零码换 Bearer 兜底重试一次（只换 Authorization 一个头，其余画像
头逐字一致）；再不行按失败上报。

── 数额口径 ─────────────────────────────────────────────────
上游报出的 credits / extra_credits 是**当日奖励的展示值**，不是到账余额
（实测官方报 200、积分池只长 100，多出来的一半是展示用的额外额度）。
所以今日收益按「签到前后余额差值」记；拿不到差值（余额查询失败）才
回退官方报的当日奖励数额。报告文案据实写「官方报出当日奖励」，不替
实际到账背书。

── 输出契约（末行 JSON，与其余平台一致）──────────────────────
  {
    "result": "CLAIMED|ALREADY|NO_AUTH|NETWORK|ERROR",
    "report": "可读汇报",
    "today_credit": 0,
    "streak_days": 0,
    "total_credits": 0
  }

`streak_days`：上游没有连续签到天数字段，天数在本地累计（与
AutoClaw / Qoder / 小浣熊同口径）：首次从领取台账回填历史，之后每确认
一天 +1、同日不双计。

凭据：按 `auths/trae.json` → `auths/trae_sync.json` → 环境变量的优先级取；
**优先用 `--login` 走网页登录**（会带上设备绑定所需的 deviceId 与设备
密钥），手工 `--auth-token` 写入的凭据缺 deviceId 会被硬拦。
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _trae_sync as ts  # noqa: E402  同目录共享模块：凭据 / 续期 / 组头 / 登录原语
import _syncio  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(PROJECT_ROOT, "data", "trae_state.json")
LEDGER_PATH = os.path.join(PROJECT_ROOT, "data", "ledger.json")

EP_CHECKIN_STATUS = "/trae/api/v2/ug/checkin_credits/status"
EP_CHECKIN_CLAIM = "/trae/api/v2/ug/checkin_credits/claim"

CODE_OK = 0
CODE_REJECTED = 9074
CODE_TOKEN_DEAD = 1001

SCHEME_JWT = "Cloud-IDE-JWT"
SCHEME_BEARER = "Bearer"

REAUTH_TEXTS = ("not able to authenticate", "unable to authenticate")

# 9074 后换新设备号重试前的等待（秒）。本仓三次一手记录都是换号后通过
# （两次分别隔 11 秒、约 1 分钟，一次几乎是紧接着重试就过），5 秒取的是
# 中间值：既不贴着上一轮的计数连打，也不至于让面板点一次签到卡太久。
RETRY_DELAY_SECONDS = 5


# ── 输出与本地状态 ──────────────────────────────────────────

def _out(result: str, report: str, today: int = 0, total: int | None = 0,
         streak: int = 0, signed_today: bool | None = None) -> dict:
    out = {
        "result": result,
        "report": report,
        "today_credit": int(today),
        "streak_days": int(streak),
        # 读不到余额时给 null（面板回退到上一次已知值），不写 0 冒充「没有积分」
        "total_credits": int(total) if total is not None else None,
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
    """本地累计领取天数（上游没有该字段，与 AutoClaw/Qoder/小浣熊同口径）。

    首次从领取台账回填历史：台账里 platform=trae 且当日确有领取的日期，
    去掉「今天」的部分（今天那份由确认逻辑记，避免双计）。之后只增不减，
    存在 state 里；台账被清理也不影响已有计数。
    """
    if "streak_days" in state:
        return int(state["streak_days"] or 0)
    days: set[str] = set()
    try:
        with open(LEDGER_PATH, encoding="utf-8") as handle:
            doc = json.load(handle)
        for row in doc.get("checkin_log") or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("platform") or "") != "trae":
                continue
            if str(row.get("result") or "") != "CLAIMED":
                continue
            day = str(row.get("ts") or "")[:10]
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
    """确认今天确有领取/已签（自己领到、或上游确认今日已签）：累计 +1，每天只记一次。"""
    _streak_days(state)  # 确保历史已回填
    today = time.strftime("%Y-%m-%d")
    if state.get("last_date") != today:
        state["streak_days"] = int(state.get("streak_days") or 0) + 1
        state["last_date"] = today
        _save_state(state)
    return int(state.get("streak_days") or 0)


# ── 状态机（对齐参考实现 checkin.rs）───────────────────────

class Status:
    """一次往返的读数。缺字段一律按 False / 0 收（不猜）。"""

    __slots__ = ("checked_in", "did_checked_in", "enable", "credits",
                 "extra_credits", "code", "message")

    def __init__(self, checked_in=False, did_checked_in=False, enable=False,
                 credits=0, extra_credits=0, code=CODE_OK, message=""):
        self.checked_in = checked_in
        self.did_checked_in = did_checked_in
        self.enable = enable
        self.credits = credits
        self.extra_credits = extra_credits
        self.code = code
        self.message = message

    def done_today(self) -> bool:
        """「今天已算签过」—— 账号维度（checked_in）或设备维度（did_checked_in）任一为真。"""
        return self.checked_in or self.did_checked_in

    def award_total(self) -> int:
        return self.credits + self.extra_credits


def _flag(payload: dict, key: str) -> bool:
    return payload.get(key) is True


def _number(payload: dict, key: str) -> int:
    value = payload.get(key)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _code_of(payload: dict) -> int:
    try:
        return int(payload.get("code"))
    except (TypeError, ValueError):
        return CODE_OK


def status_of(payload: dict) -> Status:
    message = str(payload.get("message") or "").strip()
    return Status(
        checked_in=_flag(payload, "checked_in"),
        did_checked_in=_flag(payload, "did_checked_in"),
        enable=_flag(payload, "enable"),
        credits=_number(payload, "credits"),
        extra_credits=_number(payload, "extra_credits"),
        code=_code_of(payload),
        message=message,
    )


# Outcome 五态：NotEnabled / AlreadyCheckedIn / Claimed / Rejected / Unconfirmed
_NOT_ENABLED = "not_enabled"
_ALREADY = "already"
_CLAIMED = "claimed"
_REJECTED = "rejected"
_UNCONFIRMED = "unconfirmed"


def _decide(before: Status, claim: Status, after: Status | None) -> tuple:
    """状态 → 领取 → 回查的结论（与网络层解耦，对齐参考实现 decide）。"""
    if not before.enable:
        return _NOT_ENABLED, before
    if before.done_today():
        return _ALREADY, before
    if claim.code != CODE_OK:
        return _REJECTED, claim
    if after is not None and after.done_today():
        return _CLAIMED, after
    return _UNCONFIRMED, claim


def needs_reauth(row: dict) -> bool:
    """这一行是不是「服务端把这张 JWT 作废了」（码 1001 或文案两种写法）。"""
    if _code_of(row) == CODE_TOKEN_DEAD:
        return True
    message = str(row.get("msg") or "")
    return any(text in message for text in REAUTH_TEXTS)


def _rejected_text(code: int, message: str, retried: bool = False) -> str:
    """拒绝行的文案（对齐参考实现 complete 的 Rejected 分支）。"""
    reason = message if message else "上游未给文案"
    if code == CODE_REJECTED:
        # 这一码原来只能并列两种读法；本仓后来又撞了两次，两次都是**换一张
        # 新设备号就领到**（一次隔 11 秒、一次隔约 1 分钟），同一令牌、同一
        # 套画像、同一账号。所以按「device 维度限流 / 名额时间窗」处理，
        # 重试一次；仍不过才算当日真的领不到。
        if retried:
            return (f"官方拒绝本次签到（{code}）：{reason}；已换一张新设备号重试一次仍被拒，"
                    "这一码实测是 device 维度限流或名额时间窗（不是凭证或谱系问题），"
                    "当日不再自动重试，过一会儿再点一次签到")
        return (f"官方拒绝本次签到（{code}）：{reason}；这一码实测是 device 维度"
                "限流或名额时间窗（不是凭证或谱系问题），处置是换一张新设备号"
                "重试一次；本次是 HTTP 层先失败、没能走到重试，想再试就再点一次签到")
    if code == CODE_TOKEN_DEAD:
        return (f"官方拒绝本次签到（{code}）：{reason}；这一码判为"
                "「服务端已作废这张登录态」（本地到期时间看不出来），"
                "处置是换发新令牌后重放一轮")
    return (f"官方拒绝本次签到（{code}）：{reason}；"
            "这一码本家没有实测依据，只按失败上报，当日台账不落")


def _complete(outcome: str, reading: Status, device_id: str,
              probe: Status | None = None, retried: bool = False) -> dict:
    """判定 → 结果行（对齐参考实现 complete；只给拒绝行挂状态五格）。"""
    row: dict = {}
    awarded = None
    if outcome == _NOT_ENABLED:
        row["success"] = False
        row["msg"] = "当前没有可领取的签到活动（上游未对该账号开放这一档）"
    elif outcome == _ALREADY:
        row["success"] = False
        row["alreadyCompleted"] = True
        total = reading.award_total()
        row["msg"] = (f"今日已签到（官方报出当日奖励 {total} 积分）" if total > 0
                      else "今日已签到")
        awarded = reading
    elif outcome == _CLAIMED:
        row["success"] = True
        total = reading.award_total()
        row["msg"] = (f"签到成功（官方报出当日奖励 {total} 积分）"
                      if total > 0 else "签到成功（上游未给出奖励数额）")
        awarded = reading
    elif outcome == _REJECTED:
        row["success"] = False
        row["code"] = reading.code
        row["msg"] = _rejected_text(reading.code, reading.message, retried)
    elif outcome == _UNCONFIRMED:
        row["success"] = False
        row["claimUnconfirmed"] = True
        row["msg"] = "签到返回成功但同设备号回查未确认（服务端静默拒签），当日不再自动重试"
    if awarded is not None:
        row["awarded"] = awarded.award_total()
        row["checkinCredits"] = awarded.credits
        row["checkinExtraCredits"] = awarded.extra_credits
    row["checkinDeviceId"] = device_id
    if probe is not None and outcome == _REJECTED:
        # 未知码的含义只能从「状态说什么 × 领取拒不拒」这个组合里夹出来，
        # 所以拒绝行把**第一发状态**的五格原样挂上（成功行不挂：数额走 awarded）。
        row["statusEnable"] = probe.enable
        row["statusCheckedIn"] = probe.checked_in
        row["statusDidCheckedIn"] = probe.did_checked_in
        row["statusCredits"] = probe.credits
        row["statusExtraCredits"] = probe.extra_credits
    return row


def _checkin_device_id() -> str:
    """本轮签到的设备号：16 位纯数字串，逐字节取模丢掉 250-255（分布不歪）。"""
    import secrets

    digits = ""
    while len(digits) < 16:
        for byte in secrets.token_bytes(32):
            if byte < 250:
                digits += str(byte % 10)
                if len(digits) == 16:
                    break
    return digits


def _http_failure(status: int, err: str) -> dict | None:
    """HTTP 层失败 → 平台契约结果行；能归类就归类，归不了留给调用方。"""
    if status in (401, 403):
        return _out("NO_AUTH",
                    f"登录态被上游拒绝（HTTP {status}），请重新登录或更新凭据")
    if status in (-1, 429, 502, 503, 504):
        return _out("NETWORK", f"签到接口请求失败：{err or status}")
    return _out("ERROR", f"签到接口请求失败：{err or status}")


# ── 一轮签到（对齐参考实现 run）────────────────────────────

def _post_probe(url: str, access: str, variant: str, device_id: str,
                scheme: str = SCHEME_JWT) -> tuple[dict | None, int, str]:
    """一发 ug 请求。

    换鉴权方案时**只换 Authorization 一个头**，其余画像头逐字一致
    （混改会撞上「画像对不上」的风控）。

    HTTP >= 400 在这里就地判死（对齐参考实现 post_checkin 的口径）：
    ug 族带 Cloud-IDE-JWT 在生产上是通的，一个 401 的含义是「这条会话
    真的死了」，而不是「该换 Bearer 了」。不放过去的话，401 的 JSON 体
    会被 status_of 按缺字段收成 enable=false —— 把鉴权失败洗成「活动没开」。
    """
    headers = ts.ug_headers(variant, access, device_id)
    for index, (name, _value) in enumerate(headers):
        if name == "Authorization":
            headers[index] = (name, f"{scheme} {access}")
            break
    payload, status, err = ts.api_json("POST", url, headers, {},
                                       timeout=ts.REQUEST_TIMEOUT)
    if payload is None:
        return None, status, err
    if status >= 400:
        head = str(payload.get("message") or payload.get("msg")
                   or payload.get("error") or "").strip()
        return None, status, head or f"HTTP {status}"
    return payload, status, ""


def _round_trip(url: str, access: str, variant: str, device_id: str):
    """一发「方案 × 探测体」的往返（体序列只有一条：恒 {}）。

    分派：code:0 采纳；9074 体用尽即终点（换方案没用）；1001 也是终点
    （真正的处置在驱动层：换发新令牌后重放）；其它非零码换 Bearer 再打一发。
    HTTP >= 400 原样抛给调用方。
    """
    payload, status, err = _post_probe(url, access, variant, device_id)
    if payload is None:
        return None, status, err
    last = payload
    code = _code_of(payload)
    if code in (CODE_OK, CODE_REJECTED, CODE_TOKEN_DEAD):
        return payload, status, ""
    payload, status, err = _post_probe(url, access, variant, device_id,
                                       scheme=SCHEME_BEARER)
    if payload is None:
        return None, status, err
    return payload, status, ""


def _claim_round(creds: dict, retried: bool = False) -> tuple[dict, tuple[int, str] | None]:
    """一轮签到（凭证 → 结果行）。返回 (结果行, HTTP 层错误)。

    `retried=True` 表示这是 9074 之后换号重试的那一轮，只影响拒绝文案
    （说明「已重试过一次」，不再说「本轮只发了一发」）。
    """
    variant = creds["variant"]
    access = creds["access"]
    device_id = _checkin_device_id()
    base = ts.UG_HOST

    before_payload, status, err = _round_trip(base + EP_CHECKIN_STATUS,
                                              access, variant, device_id)
    if before_payload is None:
        return {}, (status, err)
    before = status_of(before_payload)
    # 第一发自己回非零码时原样报拒绝，不许退化成「活动没开」。
    if before.code != CODE_OK:
        return _complete(_REJECTED, before, device_id, retried=retried), None
    if not before.enable or before.done_today():
        # 这条分支根本没打领取，claim 用空响应占位。
        return _complete(_decide(before, Status(), None)[0], before, device_id), None

    claim_payload, status, err = _round_trip(base + EP_CHECKIN_CLAIM,
                                             access, variant, device_id)
    if claim_payload is None:
        return {}, (status, err)
    claim = status_of(claim_payload)
    if claim.code != CODE_OK:
        return _complete(_REJECTED, claim, device_id, probe=before,
                         retried=retried), None
    # 同设备号回查。回查本身失败按「未确认」处理 —— 一发没被确认的 claim
    # 报成成功，就是给用户一个假成功。
    after_payload, _status2, _err2 = _round_trip(base + EP_CHECKIN_STATUS,
                                                 access, variant, device_id)
    after = status_of(after_payload) if after_payload is not None else None
    outcome, reading = _decide(before, claim, after)
    return _complete(outcome, reading, device_id), None


def _credit_snapshot(creds: dict, state: dict,
                     before: int | None = None) -> int | None:
    """查一次真实积分余额（只读 ent_usage），填卡片上的「当前累计积分」。

    签到接口只回当日奖励数额、没有余额，所以余额得单独读一次。失败时退回
    state 里上一次读到的值 —— 一次网络抖动不该把卡片刷成 0；连历史值都没有
    才给 None（面板按「没读到」处理）。

    `before` 是签到前读到的余额（差值口径用）：本次读到的余额作为最新值
    落进 state；把 `state["credit_before"]` 保留成 `before`（上一轮的
    `credit_balance`），供之后做「签到前后余额差值」核对。签到那次差分
    在此闭环：fresh=最新的"已含今日"，before=读它之前的值。

    临期明细不在这里报：过期日历由隐藏积分源（trae_credits.py）负责，两处
    都报同一批权益包会让日历双计。
    """
    reading = ts.read_credit(creds)
    fresh: int | None = None
    if reading.get("ok") and reading.get("known") and not reading.get("unlimited"):
        fresh = int(reading["remain"])
        state["credit_balance"] = fresh
    if before is not None:
        state["credit_before"] = int(before)
    _save_state(state)
    if fresh is not None:
        return fresh
    last = state.get("credit_balance")
    try:
        return int(last) if last is not None else None
    except (TypeError, ValueError):
        return None


def _credit_diff(before: int | None, after: int | None) -> int | None:
    """签到前后的余额差值 = 实际到账（官方报的奖励是展示值，一半是 extra）。

    只在两边都是有限数时给差：任一边没读到就算不出真实收益，退回 None。
    """
    if before is None or after is None:
        return None
    diff = int(after) - int(before)
    return diff if diff > 0 else None


def _map_row(row: dict, state: dict, balance: int | None = None,
             diff: int | None = None) -> dict:
    """结果行 → 平台契约输出。

    `balance` 是卡片上的当前积分；`diff` 是签到前后余额差值（实际到账）。
    CLAIMED 时今日收益用 `diff`（官方报的奖励是展示值，实测只有一半真入池）；
    拿不到差值才回退官方报的当日奖励数额。
    """
    if row.get("success"):
        streak = _confirm_day(state)
        today = diff if diff is not None else int(row.get("awarded") or 0)
        return _out("CLAIMED", str(row.get("msg") or ""),
                    today=today, total=balance, streak=streak)
    if row.get("alreadyCompleted"):
        streak = _confirm_day(state)
        return _out("ALREADY", str(row.get("msg") or ""), total=balance, streak=streak)
    streak = _streak_days(state)
    if row.get("claimUnconfirmed"):
        return _out("ERROR", str(row.get("msg") or ""), total=balance, streak=streak)
    if "code" in row:
        return _out("ERROR", str(row.get("msg") or ""), total=balance, streak=streak)
    # NotEnabled：中性，不算失败也不算成功
    return _out("ALREADY", str(row.get("msg") or ""), total=balance, streak=streak)


def _renew_if_due(creds: dict) -> dict:
    """临期（24h 窗口）先换发；换发成功就回写凭据（工作副本 + 镜像）。"""
    if ts.needs_refresh(creds):
        renewed = ts.refresh_once(creds, ts.refresh_candidates(creds["api_host"]))
        if renewed is not None:
            ts.save_credentials(renewed)
            return renewed
    return creds


def _require_checkin_credentials(creds: dict, state: dict):
    """公共前置检查：谱系 / accessToken / deviceId。返回契约行或 None。"""
    if creds["variant"] in ts.INTL_VARIANTS:
        # 国际版上游没有签到通道（无此活动）。如实说明，不打注定失败的请求。
        return _out("ALREADY", "国际版 Trae 没有签到通道（上游无此活动）",
                    streak=_streak_days(state))
    if not (creds.get("access") or "").strip():
        return _out("NO_AUTH", "该 Trae 账号没有可用凭证（accessToken 为空），无法签到")
    if not (creds.get("device_id") or "").strip():
        # 缺 deviceId 的凭据多半是手工粘贴的，服务端侧没有设备绑定，
        # 签到正好落在「绑定不一致」的高危画像上。硬拦，不打这一发。
        return _out("ERROR",
                    "该账号没有 deviceId（多半是手工粘贴的凭据），官方按设备画像风控，"
                    "请先重新登录再签到")
    return None


def _rate_limited(row: dict) -> bool:
    """这一行是不是 9074（device 维度限流 / 名额时间窗），值得换号重试一次。"""
    return _code_of(row) == CODE_REJECTED


def _retry_after_rate_limit(creds: dict, row: dict) -> dict:
    """9074 时换一张新设备号重打一轮，返回新的结果行。

    本仓三次一手记录都是**换张新设备号就领到**（同一令牌、同一套画像、
    同一账号），所以不再把这一码当当日终局 —— 否则定时任务会白失败一天。
    换号那轮本身失败（HTTP 层）时保留原结果行，至少能报出上游真实文案。
    """
    if not _rate_limited(row):
        return row
    time.sleep(RETRY_DELAY_SECONDS)
    retry_row, retry_err = _claim_round(creds, retried=True)
    return row if retry_err is not None else retry_row


def _run() -> dict:
    creds = ts.resolve_credentials()
    if creds is None:
        where = "、".join(label for label, _path in ts.credential_sources())
        return _out("NO_AUTH",
                    f"未找到 Trae 登录凭据（{where}、环境变量均无）。"
                    "运行一次 `python scripts/trae.py --login` 走网页登录，"
                    "或 `--auth-token <accessToken>` 手工写入")

    state = _load_state()
    streak = _streak_days(state)
    _save_state(state)  # 首次回填的天数落盘，之后只增不减

    blocked = _require_checkin_credentials(creds, state)
    if blocked is not None:
        return blocked

    creds = _renew_if_due(creds)

    row, http_err = _claim_round(creds)
    if http_err is not None:
        out = _http_failure(http_err[0], http_err[1])
        out["streak_days"] = streak
        return out
    row = _retry_after_rate_limit(creds, row)
    if not needs_reauth(row):
        # 签到前先读一次余额（快照旧值），签到后立刻再读（已含今日这份），
        # 两者差值就是实际到账；第二次读失败时退回上次已知值。
        before = _credit_snapshot(creds, state)          # 签到前的余额
        after = _credit_snapshot(creds, state, before)   # 签到后的余额（此刻已含今日）
        return _map_row(row, state, after, _credit_diff(before, after))

    # 1001：本地到期表看不出来，只能强制换发后重放一轮。换号重试与换发
    # 重放各最多一次，避免在一条失败的链上无限试。
    renewed, reason = ts.refresh_once_ex(creds,
                                         ts.refresh_candidates(creds["api_host"]))
    if renewed is None:
        head = str(row.get("msg") or "")
        return _out("ERROR",
                    f"{head}；换发新令牌也失败（{reason}），请重新登录",
                    streak=streak)
    ts.save_credentials(renewed)
    row2, http_err2 = _claim_round(renewed)
    if http_err2 is not None:
        out = _http_failure(http_err2[0], http_err2[1])
        out["streak_days"] = streak
        return out
    final_row = _retry_after_rate_limit(renewed, row2)
    # 与上面的快照链同一个差值口径：换发成功后先读签到前余额、再读签到后余额。
    before = _credit_snapshot(renewed, state)
    after = _credit_snapshot(renewed, state, before)
    return _map_row(final_row, state, after, _credit_diff(before, after))


def _status() -> dict:
    """只读查询今日签到状态，不领取。仅打一次 status。

    `signed_today` 的口径：上游确认今日已签（done_today）才置 true；
    明确没签置 false；活动没开（无法判断）不置该字段，留给面板按
    「未签到」显示，不猜。
    """
    creds = ts.resolve_credentials()
    if creds is None:
        where = "、".join(label for label, _path in ts.credential_sources())
        return _out("NO_AUTH", f"未找到 Trae 登录凭据（{where}、环境变量均无）")

    state = _load_state()
    streak = _streak_days(state)

    blocked = _require_checkin_credentials(creds, state)
    if blocked is not None:
        return blocked

    # 只读核查不换发令牌：--status 由体检与面板状态核查调用，写入凭据会让
    # 它与计划任务并发时互相把 refreshToken 一次一换的链打断。令牌真过期就
    # 如实报 NO_AUTH，下一次正式签到自己会先续期。
    device_id = _checkin_device_id()
    payload, status, err = _round_trip(ts.UG_HOST + EP_CHECKIN_STATUS,
                                       creds["access"], creds["variant"], device_id)
    if payload is None:
        out = _http_failure(status, err)
        out["streak_days"] = streak
        return out

    before = status_of(payload)
    if before.code != CODE_OK:
        return _out("ERROR", _rejected_text(before.code, before.message),
                    streak=streak)
    if not before.enable:
        return _out("ALREADY", "当前没有可领取的签到活动（上游未对该账号开放这一档）",
                    streak=streak)
    if before.done_today():
        streak = _confirm_day(state)
        total = before.award_total()
        text = (f"今日已签到（官方报出当日奖励 {total} 积分）" if total > 0
                else "今日已签到")
        return _out("ALREADY", text, streak=streak, signed_today=True)
    total = before.award_total()
    return _out("ALREADY",
                f"有可领取的签到活动（官方报出当日奖励 {total} 积分），点签到即可领取",
                streak=streak, signed_today=False)


# ── --login：网页登录（PKCE + loopback 回调 + 授权码换证）────

class _CallbackHandler:
    """回调监听的处理函数外壳（由 _CallbackServer 填 holder）。"""

    def __init__(self, holder: dict):
        self.holder = holder

    def handle(self, path: str, query: str) -> tuple[int, str]:
        if path != ts.CALLBACK_PATH:
            return 404, "not found"
        callback = ts.parse_callback(query)
        if ts.callback_resolves(callback):
            self.holder["query"] = query
            return 200, "登录成功，可以关闭此页面了"
        return 200, "等待授权中…"


def _login(argv: list[str]) -> int:
    """--login：起 loopback 回调 → 打印授权地址 → 等回调 → 换证 → 落盘凭据。

    回调地址形状是死的：官方授权页前端把 auth_callback_url 硬校验成
    ^http://127.0.0.1:<port>/authorize$ —— 端口必须先 bind 成功再拼。
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlsplit

    variant = ts.SOLO
    wait_seconds = 300
    index = 0
    while index < len(argv):
        if argv[index] == "--variant" and index + 1 < len(argv):
            variant = argv[index + 1].strip().lower()
            if variant not in (ts.SOLO, ts.CN, "intl", "solo-intl"):
                print(json.dumps(_out("ERROR", f"不认识的 --variant：{variant}"),
                                 ensure_ascii=False))
                return 1
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

    holder: dict = {}
    handler_box = _CallbackHandler(holder)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            parsed = urlsplit(self.path)
            status, body = handler_box.handle(parsed.path, parsed.query)
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    context = ts.new_login_context(variant, port)
    login_host = ts.request_login_guidance()
    auth_url = ts.verification_uri(login_host, context)

    serve_thread = threading.Thread(target=server.serve_forever, daemon=True)
    serve_thread.start()

    print("请用浏览器打开下面的地址，登录并授权（脚本会在后台等待回调）：")
    print(auth_url)
    print()
    print(f"等待授权中（最多 {wait_seconds} 秒）…", flush=True)

    deadline = time.time() + wait_seconds
    while time.time() < deadline and "query" not in holder:
        time.sleep(1)
    server.shutdown()
    serve_thread.join(timeout=5)

    if "query" not in holder:
        print(json.dumps(_out("NETWORK", "等待授权超时，请重新运行 --login"),
                             ensure_ascii=False))
        return 1

    query = holder["query"]
    callback = ts.parse_callback(query)
    if callback["error"]:
        print(json.dumps(_out("ERROR", callback["error"]), ensure_ascii=False))
        return 1
    if not callback["auth_code"] and not callback["refresh_token"]:
        print(json.dumps(_out("ERROR", "回调里没有授权码也没有续期串"),
                         ensure_ascii=False))
        return 1

    effective_host = callback["login_host"] or login_host
    if callback["refresh_token"]:
        # 续期串分支：直接走续期链；换不动也把这条串存下来（参考实现同一条：
        # 存一条可能可用的凭据，好过丢掉一次已经花掉的授权）。
        seed = {
            "variant": variant, "access": "", "refresh": callback["refresh_token"],
            "expires_at": 0, "api_host": ts.DEFAULT_LOGIN_HOST,
        }
        renewed, _reason = ts.refresh_once_ex(seed,
                                              ts.refresh_candidates(seed["api_host"]))
        if renewed is not None:
            tokens = {"access": renewed["access"], "refresh": renewed["refresh"],
                      "expires_at": renewed["expires_at"],
                      "client_id": renewed["auth_client_id"]}
        else:
            tokens = {"access": callback["refresh_token"],
                      "refresh": callback["refresh_token"], "expires_at": 0,
                      "client_id": ""}
    else:
        exchanged, reason = ts.exchange_auth_code(context, callback["auth_code"],
                                                  effective_host)
        if exchanged is None:
            print(json.dumps(_out("ERROR", f"授权码换证失败：{reason}"),
                             ensure_ascii=False))
            return 1
        tokens = exchanged

    if not tokens["access"] and not tokens["refresh"]:
        print(json.dumps(_out("ERROR", "换证成功但响应里没有任何令牌，请重试"),
                         ensure_ascii=False))
        return 1

    creds = {
        "variant": variant,
        "access": tokens["access"] or tokens["refresh"],
        "refresh": tokens["refresh"],
        "expires_at": tokens["expires_at"],
        "machine_id": context["machine_id"],
        "device_id": context["device_id"],
        "device_public_key": context["device_public_pem"],
        "device_private_key": context["device_private_pem"],
        "auth_client_id": tokens["client_id"],
        "domain": "trae.cn",
        "api_host": ts.DEFAULT_LOGIN_HOST,
        # 身份是读数不是门槛：GetUserInfo 挂了就用回调回显，两个都没有就落
        # 一个固定 unknown 名（重复登录覆盖同一条记录，而不是新造账号）。
        "uid": "", "nickname": "",
    }
    authoritative = ts.get_user_info(creds)
    echo = ts.callback_identity(query)
    creds["uid"] = (authoritative.get("uid") or echo.get("uid")
                    or ts.unknown_uid_fallback(variant))
    creds["nickname"] = (authoritative.get("nickname") or echo.get("nickname") or "")

    if not ts.save_credentials(creds):
        print(json.dumps(_out("ERROR", "凭据写入 auths/ 失败，请检查目录权限"),
                         ensure_ascii=False))
        return 1
    name = creds["nickname"] or creds["uid"]
    print(json.dumps(_out("ALREADY",
                          f"Trae 登录成功，凭据已写入 auths/trae.json"
                          + (f"（账号 {name}）" if name else "")),
                     ensure_ascii=False))
    return 0


def _write_token(argv: list[str]) -> int:
    """--auth-token 写入凭据（可选 --variant）。缺 deviceId，签到会被硬拦。"""
    token = ""
    variant = ts.SOLO
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--auth-token" and index + 1 < len(argv):
            token = argv[index + 1].strip()
            index += 2
            continue
        if item == "--variant" and index + 1 < len(argv):
            variant = argv[index + 1].strip().lower()
            index += 2
            continue
        index += 1

    if not token:
        print(json.dumps(_out("ERROR", "--auth-token 缺少 accessToken 值"),
                         ensure_ascii=False))
        return 1

    creds = {"variant": variant, "access": token, "refresh": "",
             "expires_at": 0, "uid": "", "nickname": ""}
    if not ts.save_credentials(creds):
        print(json.dumps(_out("ERROR", "凭据写入 auths/ 失败，请检查目录权限"),
                         ensure_ascii=False))
        return 1
    print(json.dumps(_out("ALREADY",
                          "Trae accessToken 已写入 auths/trae.json"
                          "（缺 deviceId，签到会被硬拦；建议改用 --login）"),
                     ensure_ascii=False))
    return 0


def main() -> int:
    argv = sys.argv[1:]
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    if "--login" in argv:
        return _login(argv)
    if "--auth-token" in argv:
        return _write_token(argv)

    out = _status() if "--status" in argv else _run()
    print(json.dumps(out, ensure_ascii=False))
    if out["result"] in ("CLAIMED", "ALREADY"):
        return 0
    return 2 if out["result"] == "NO_AUTH" else 1


if __name__ == "__main__":
    sys.exit(main())
