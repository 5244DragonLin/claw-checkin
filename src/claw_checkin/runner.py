"""脚本执行 + 输出契约校验 + 接入体检

契约：脚本 stdout 末行输出一行 JSON：
{
  "result": "CLAIMED",            # CLAIMED/ALREADY/NO_SESSION/NO_AUTH/NETWORK/ERROR
  "report": "成功领取 100 积分",
  "today_credit": 100,
  "streak_days": 7,
  "total_credits": 700,
  "expiring": [ { "date": "2026-09-25", "amount": 320, "source": "每日签到" } ],  # 可选
  "signed_today": true            # 可选：平台侧确认「今天确实已签」
}

signed_today 的意义：面板默认只承认自己触发的签到动作。用户在平台客户端（或
别的签到工具）先签了，面板无从得知，只能显示「未签到」。脚本在**确有证据**
（平台返回今日已签/今日已领取）时置 true，面板据此把卡片标成已签到；
拿不准就不填——填错会让面板谎报，比显示未签到更糟。

三态口径（2026-11-10 起）：
  · true  = 确证今天已签 → 卡片标已签到；
  · false = 确证今天**没**签（例如「现在还没到开窗时刻，读到的是昨天的成绩」）
            → 卡片维持未签到，即使这次运行的 trigger 是手动签到；
  · 缺省  = 说不准（查不到/接口没这个信息）→ 不参与判断。
false 必须能压过「manual + ALREADY 就算今天签过」的默认规则：Qoder 的窗口
10:00 才开，10 点前签到脚本读到的是昨天那一窗的 CLAIMED，若沿用默认规则，
面板会把卡片标成「今日已完成」并把按钮置灰，而今天 100 credits 其实一分没到。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field

from claw_checkin.scanner import ScriptInfo, build_command

RESULT_ENUM = ["CLAIMED", "ALREADY", "NO_SESSION", "NO_AUTH", "NETWORK", "ERROR"]
REQUIRED_FIELDS = ["result", "today_credit", "streak_days", "total_credits"]
OK_RESULTS = {"CLAIMED", "ALREADY"}


@dataclass
class RunResult:
    platform: str
    script: str
    returncode: int
    result: str = "ERROR"           # 六枚举之一；执行失败归 ERROR
    report: str = ""
    today_credit: int | None = None
    streak_days: int | None = None
    total_credits: int | None = None
    expiring: list = field(default_factory=list)
    signed_today: bool = False       # 平台侧确认今日已签（见模块说明）
    signed_today_known: bool = False  # 脚本是否就「今天签没签」明确表态（true/false 都算）
    raw: str = ""                    # stdout 原文
    parse_ok: bool = False
    error: str = ""                  # 执行/解析失败原因

    @property
    def ok(self) -> bool:
        return self.result in OK_RESULTS

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "script": self.script,
            "returncode": self.returncode,
            "result": self.result,
            "report": self.report,
            "today_credit": self.today_credit,
            "streak_days": self.streak_days,
            "total_credits": self.total_credits,
            "expiring": self.expiring,
            "signed_today": self.signed_today,
            "signed_today_known": self.signed_today_known,
            "raw": self.raw,
            "parse_ok": self.parse_ok,
            "error": self.error,
            "ok": self.ok,
        }


def parse_output(stdout: str) -> dict | None:
    """取 stdout 最后一个非空行解析为 JSON。"""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        # 容错：剥离可能的日志前缀，找第一个 { 开始解析
        idx = line.find("{")
        if idx < 0:
            return None
        try:
            data = json.loads(line[idx:])
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def run_script(platform: str, path, command: list[str], timeout: int = 120) -> RunResult:
    """执行单个签到脚本并按契约解析。任何执行层失败都归一为 ERROR，绝不抛异常。"""
    rr = RunResult(platform=platform, script=path.name, returncode=-1)
    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            cwd=str(path.parent),
        )
        rr.returncode = proc.returncode
        rr.raw = (proc.stdout or "")[-4000:]
        stderr_tail = (proc.stderr or "")[-500:]
        data = parse_output(proc.stdout or "")
        if data is None:
            rr.error = stderr_tail.strip() or "stdout 末行不是可解析的 JSON"
            rr.report = rr.error
            return rr
        rr.parse_ok = True
        rr.result = str(data.get("result", "")).upper() or "ERROR"
        rr.report = str(data.get("report", ""))
        for k in ("today_credit", "streak_days", "total_credits"):
            v = data.get(k)
            rr.__dict__[k] = int(v) if isinstance(v, (int, float)) else None
        exp = data.get("expiring")
        if isinstance(exp, list):
            rr.expiring = [e for e in exp if isinstance(e, dict) and e.get("date")]
        # 只认明确的布尔真值：字符串 "false"、0、缺失都按「没有证据」处理，
        # 不能让脏字段把平台谎报成已签到。但布尔 false 是脚本的**明确表态**
        # （「今天确实没签」），单独记在 signed_today_known 里，供面板否决
        # 「手动签到 + ALREADY 就算今天签过」的默认规则
        value = data.get("signed_today")
        rr.signed_today = value is True
        rr.signed_today_known = isinstance(value, bool)
        if rr.result not in RESULT_ENUM:
            rr.result = "ERROR"
            rr.error = f"result 取值越界：{rr.result!r}（六枚举之外）"
        if rr.result == "ERROR" and not rr.report:
            rr.report = rr.error or "脚本返回 ERROR"
        return rr
    except subprocess.TimeoutExpired:
        rr.error = f"执行超时（>{timeout}s）"
        rr.report = rr.error
        return rr
    except FileNotFoundError:
        rr.error = f"执行器不存在：{command[0]}"
        rr.report = rr.error
        return rr
    except Exception as e:  # 兜底，面板不允许崩
        rr.error = f"执行异常：{e}"
        rr.report = rr.error
        return rr


def health_check(info: ScriptInfo, timeout: int = 120) -> dict:
    """接入体检 5 项：可执行性 / 输出合法 / 字段齐全 / 枚举合规 / 幂等双跑。

    新脚本先跑体检，通过后才纳入调度。连跑两次：第二次应返回 ALREADY 且积分不变。
    """
    cmd = build_command(info)
    first = run_script(info.name, info.path, cmd, timeout)

    # 可执行性只看「跑得动、吐得出合法契约」，不要求这次真的签到成功。
    # 脚本对未成功的结果一律返回非 0 退出码（1/2），那属于业务结论，由
    # 「枚举合规」体现；否则凭据没就绪时会被误判成「脚本跑不动」。
    contract_ok = first.parse_ok and first.result in RESULT_ENUM
    if first.returncode == 0:
        exec_detail = f"退出码 {first.returncode}"
    elif contract_ok:
        exec_detail = f"退出码 {first.returncode}（{first.result}，契约合法）"
    else:
        exec_detail = f"退出码 {first.returncode}"

    checks = [
        {"name": "可执行性", "desc": "脚本跑得动、能吐出符合契约的结果",
         "status": "pass" if (first.returncode == 0 or contract_ok) else "fail",
         "detail": exec_detail},
        {"name": "输出合法", "desc": "stdout 末行是可解析的单行 JSON",
         "status": "pass" if first.parse_ok else "fail",
         "detail": "已解析" if first.parse_ok else "末行非 JSON"},
        {"name": "字段齐全", "desc": "result / today_credit / streak_days / total_credits 都在",
         "status": None, "detail": ""},
        {"name": "枚举合规", "desc": "result 取值落在约定的六种状态之内",
         "status": None, "detail": ""},
        {"name": "幂等双跑", "desc": "连跑两次，第二次返回 ALREADY 且积分不变——防重复刷分，第一次真实签到后自动补验",
         "status": "skip", "detail": "待验证"},
    ]

    if first.parse_ok:
        # 「字段齐全」看的是契约字段**存在与否**，不是值是否为 None——null 是
        # 合法值（读不到余额时输出 null，面板回退上一次已知值），把 null 当
        # 缺失会让「余额未知的正常回报」过不了体检（2026-11-09 库库踩中）
        missing = [f for f in REQUIRED_FIELDS if f not in first.__dict__]
        checks[2]["status"] = "pass" if not missing else "fail"
        checks[2]["detail"] = "字段齐全" if not missing else f"缺少 {', '.join(missing)}"
        checks[3]["status"] = "pass" if first.result in RESULT_ENUM and first.result != "ERROR" else "fail"
        checks[3]["detail"] = first.result

        # 双跑验证：只有首次确实领到或确认已领（CLAIMED/ALREADY）才谈得上幂等，
        # 与退出码无关（脚本的退出码表达业务结论，不表达「能不能跑」）
        if not missing and first.result in OK_RESULTS:
            import time
            time.sleep(0.5)
            second = run_script(info.name, info.path, cmd, timeout)
            idempotent = (
                second.result == "ALREADY"
                and second.total_credits == first.total_credits
            )
            checks[4]["status"] = "pass" if idempotent else "fail"
            checks[4]["detail"] = (
                f"第二次 {second.result}，积分不变"
                if idempotent
                else f"第二次返回 {second.result or '无输出'}"
                + ("" if second.total_credits == first.total_credits else "，积分发生变化")
            )
        elif not missing:
            checks[4]["status"] = "skip"
            checks[4]["detail"] = f"首次返回 {first.result}，待首次真实签到后补验"
        else:
            checks[4]["status"] = "skip"
            checks[4]["detail"] = "前置项未通过，跳过"

    # 总体：全 pass = 已校验；有 fail = 未通过；含 skip 无 fail = 待验证
    statuses = [c["status"] for c in checks]
    if "fail" in statuses:
        overall = "未通过"
    elif "skip" in statuses:
        overall = "待验证"
    else:
        overall = "已校验"
    return {
        "platform": info.name,
        "overall": overall,
        "checks": checks,
        "first_run": first.to_dict(),
    }
