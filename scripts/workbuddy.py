"""WorkBuddy 每日签到 + 成长中心 · ClawCheckin 接入脚本

参考：https://github.com/88lin/workbuddy-auto-signin（signin.py）

本脚本作为 WorkBuddy signin.py 的 ClawCheckin 包装层：
  - 自动查找 signin.py（优先级从高到低）：环境变量 WORKBUDDY_SIGNIN_DIR
    > 本地克隆 _ref_workbuddy/ > 内置副本 vendor/workbuddy/ > scripts/signin.py
  - 调用 `python signin.py auto` 执行签到 + 成长中心全套
  - 解析输出并映射为 ClawCheckin 输出契约

内置副本来自上游 MIT 仓库的原样文件（来源与同步方式见 vendor/workbuddy/README.md）；
也可通过环境变量 WORKBUDDY_SIGNIN_DIR 指向包含 signin.py 的目录。

输出契约（末行 JSON）：
  {
    "result": "CLAIMED|ALREADY|NO_SESSION|NO_AUTH|NETWORK|ERROR",
    "report": "成功领取 100 积分（连续 7 天，累计 700 积分）",
    "today_credit": 100,
    "streak_days": 7,
    "total_credits": 700,
    "expiring": [],   // 可选
    "signed_today": true  // 可选：平台侧确认今天已签（仅 --status 只读查询会带上）
  }

`--status`：只读查询今天签没签（调 signin.py 的 status 命令，读
`today_checked_in`），**不领取任何积分**。面板在打开/切回时用它承认「你在
客户端已经签过」的到，免得卡片一直显示未签到。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _wbsync  # noqa: E402  同目录共享模块：WorkBuddy 凭据跨机同步镜像

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIGNIN_CANDIDATES = [
    # 本项目的 _ref_workbuddy 引用克隆（用户有意克隆，通常更新，优先）
    os.path.join(PROJECT_ROOT, "_ref_workbuddy", "signin.py"),
    # 仓库内置副本（vendor/workbuddy，见该目录 README）
    os.path.join(PROJECT_ROOT, "vendor", "workbuddy", "signin.py"),
    # 用户自己 clone 到脚本同级目录
    os.path.join(PROJECT_ROOT, "scripts", "signin.py"),
]

SIGNIN_TIMEOUT = 120  # 签到 + 成长中心预算
STATUS_TIMEOUT = 60   # 只读查询（含客户端解密）


def _find_signin() -> str | None:
    """查找 signin.py 可执行路径。"""
    override = os.environ.get("WORKBUDDY_SIGNIN_DIR")
    if override:
        path = os.path.join(override, "signin.py")
        if os.path.isfile(path):
            return path

    for candidate in SIGNIN_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate

    return None


def _refresh_check() -> dict:
    """纯凭证刷新：校验本地/同步镜像凭据是否就绪，不执行签到。"""
    path, source = _wbsync.pick()
    where = {"local": "本机凭据", "mirror": "同步镜像凭据（auths/）", "none": ""}[source]
    if not path:
        return {
            "result": "NO_AUTH",
            "report": "未找到 WorkBuddy 登录凭据（本机与同步镜像均无），请先登录 WorkBuddy 桌面端",
            "today_credit": 0, "streak_days": 0, "total_credits": 0,
        }
    session = _wbsync.load_session_file(path)
    token = _wbsync.token_of(session)
    if token is None:
        return {
            "result": "NO_AUTH",
            "report": f"{where}读取失败或缺少 accessToken，请重新登录 WorkBuddy 桌面端",
            "today_credit": 0, "streak_days": 0, "total_credits": 0,
        }
    exp = _wbsync.session_expiry(session)
    if exp and exp <= time.time():
        return {
            "result": "NO_AUTH",
            "report": f"{where}已过期，请重新登录 WorkBuddy 桌面端",
            "today_credit": 0, "streak_days": 0, "total_credits": 0,
        }
    # 新版加密信封（$wbEncrypted/sym-v1）：签到时由本机 WorkBuddy.exe 运行时解密；
    # 同步镜像里存的是明文 token，无需客户端参与
    if isinstance(token, dict) and token.get("$wbEncrypted") == 1:
        return {
            "result": "ALREADY",
            "report": f"Token 已就绪（{where}，加密信封，签到时自动解密）",
            "today_credit": 0, "streak_days": 0, "total_credits": 0,
        }
    if isinstance(token, str) and token:
        return {
            "result": "ALREADY",
            "report": f"Token 已刷新，凭据有效（{where}）",
            "today_credit": 0, "streak_days": 0, "total_credits": 0,
        }
    return {
        "result": "NO_AUTH",
        "report": "凭据中未找到有效 accessToken，请重新登录 WorkBuddy 桌面端",
        "today_credit": 0, "streak_days": 0, "total_credits": 0,
    }


def _parse_signin_output(stdout: str) -> dict | None:
    """解析 signin.py 的最后一行 JSON 输出。"""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        idx = line.find("{")
        if idx < 0:
            continue
        try:
            return json.loads(line[idx:])
        except json.JSONDecodeError:
            continue
    return None


def _signin_env(signin_path: str, auth_file: str | None = None) -> dict:
    """构造 signin.py 子进程环境：显式指定凭据文件 + 日志位置。

    auth_file 为空时按本地优先规则现选一份。凭据一律显式传给内核，不再依赖它
    自己猜——回退重试时要能指定「另一份」。
    """
    env = os.environ.copy()
    try:
        _wbsync.update_from_local(signin_path)
    except Exception:
        pass
    target = auth_file if auth_file is not None else _wbsync.pick()[0]
    if target:
        env["WORKBUDDY_AUTH_FILE"] = target
    if "WORKBUDDY_SIGNIN_LOG" not in env:
        data_dir = os.path.join(PROJECT_ROOT, "data")
        os.makedirs(data_dir, exist_ok=True)
        env["WORKBUDDY_SIGNIN_LOG"] = os.path.join(data_dir, "workbuddy_signin.log")
    return env


# 这些结论等同于「凭据没被服务端接受」，值得换另一份凭据重试
AUTH_PROBLEM_RESULTS = {"NO_AUTH", "NO_SESSION", "AUTH_REJECTED", "AUTH_ERROR",
                        "FORBIDDEN", "INVALID_RUNTIME_PATH", "KEY_MISMATCH",
                        "DECRYPT_FAILED", "HELPER_PROTOCOL"}
_LAST_STDERR = ""   # 最近一次 signin.py 的 stderr 尾巴，仅用于失败时的报告文案


def _is_auth_problem(data: dict | None) -> bool:
    if not isinstance(data, dict):
        return False
    if str(data.get("http") or "") == "401":
        return True
    return str(data.get("result") or "").upper() in AUTH_PROBLEM_RESULTS


def _run_signin(signin_path: str, mode: str, auth_file: str | None) -> tuple[dict | None, str]:
    """跑一次 signin.py（mode = auto / status），返回 (解析结果, 失败类别)。"""
    global _LAST_STDERR
    env = _signin_env(signin_path, auth_file)
    try:
        proc = subprocess.run(
            ["python", str(signin_path), mode],
            capture_output=True, text=True,
            timeout=SIGNIN_TIMEOUT if mode == "auto" else STATUS_TIMEOUT,
            encoding="utf-8", errors="replace",
            cwd=os.path.dirname(signin_path), env=env,
        )
    except FileNotFoundError:
        return None, "no-python"
    except subprocess.TimeoutExpired:
        return None, "timeout"
    _LAST_STDERR = (proc.stderr or "")[-300:]
    data = _parse_signin_output(proc.stdout or "")
    return data, ("" if data is not None else "bad-output")


def _run_with_fallback(signin_path: str, mode: str) -> tuple[dict | None, str, str | None]:
    """先按本地优先选一份凭据跑；被服务端拒绝就换另一份重试一次。

    返回 (解析结果, 失败类别, 真正拿来用的凭据路径)。重试成功的那份会被回写进
    跨机镜像——它刚被服务端接受过，是当下唯一有实证可用的凭据。
    """
    primary, _source = _wbsync.pick()
    data, err = _run_signin(signin_path, mode, primary)
    if not _is_auth_problem(data):
        return data, err, primary

    alt = _wbsync.alternate(primary)
    if not alt:
        return data, err, primary
    retry, retry_err = _run_signin(signin_path, mode, alt)
    if retry is not None and not _is_auth_problem(retry):
        try:
            _wbsync.heal_mirror(alt, signin_path)
        except Exception:
            pass
        return retry, retry_err, alt
    # 两份都不认：优先报告换过之后的结果（信息更具体）
    return (retry, retry_err, alt) if retry is not None else (data, err, primary)


def _dig(obj, key):
    """在可能被 data/result 包裹的响应里找字段（与 signin.py 同款容错）。"""
    if isinstance(obj, dict):
        if obj.get(key) is not None:
            return obj[key]
        for k in ("data", "result", "resp", "response"):
            if isinstance(obj.get(k), dict):
                got = _dig(obj[k], key)
                if got is not None:
                    return got
    return None


def _status_probe() -> dict:
    """只读查询「今天签过没有」：跑 signin.py 的 status 命令，绝不触发领取。

    status 只打 `checkin-activity-status`（GET 语义的查询接口），不做任何写入。
    只有平台明确回报 today_checked_in 为真时才置 signed_today=true——拿不准就
    不填，宁可显示未签到，也不谎报已签。
    """
    signin_path = _find_signin()
    if not signin_path:
        return {"result": "NO_AUTH", "report": "未找到 WorkBuddy 签到内核 signin.py",
                "today_credit": 0, "streak_days": 0, "total_credits": 0}
    data, err, _used = _run_with_fallback(signin_path, "status")
    if data is None:
        if err == "no-python":
            return {"result": "NETWORK", "report": "找不到 python 解释器，请确认 Python 已安装且加入 PATH",
                    "today_credit": 0, "streak_days": 0, "total_credits": 0}
        if err == "timeout":
            return {"result": "NETWORK", "report": f"状态查询超时（>{STATUS_TIMEOUT}s）",
                    "today_credit": 0, "streak_days": 0, "total_credits": 0}
        return {"result": "ERROR", "report": "状态查询未返回可解析结果",
                "today_credit": 0, "streak_days": 0, "total_credits": 0}
    if _is_auth_problem(data):
        return {"result": "NO_AUTH", "report": str(data.get("report") or "登录凭据未就绪"),
                "today_credit": 0, "streak_days": 0, "total_credits": 0}

    http = data.get("http") or 0
    if not (200 <= int(http) < 300):
        return {"result": "NETWORK" if int(http) in (0, 429, 502, 503, 504) else "ERROR",
                "report": f"状态查询失败（HTTP {http}）",
                "today_credit": 0, "streak_days": 0, "total_credits": 0}

    body = data.get("body") if isinstance(data.get("body"), dict) else {}
    signed = _dig(body, "today_checked_in") in (True, 1)
    active = _dig(body, "active")
    out = {
        "result": "ALREADY" if signed else "ERROR",
        "report": "",
        "today_credit": 0,
        "streak_days": int(_dig(body, "streak_days") or 0),
        "total_credits": int(_dig(body, "total_credits") or 0),
        "signed_today": signed,
    }
    if signed:
        streak = out["streak_days"]
        out["report"] = "平台确认今日已签到" + (f"（连续 {streak} 天）" if streak else "")
    elif active is False:
        out["result"] = "ALREADY"
        out["report"] = "签到活动未开启"
    else:
        out["result"] = "ALREADY"
        out["report"] = "平台显示今天尚未签到"
    return out


def _remap(result: dict) -> dict:
    """将 signin.py 的输出字段映射为 ClawCheckin 契约格式。

    signin.py vs ClawCheckin 字段对照：
      CLAIMED: credit  → today_credit
      ALREADY: today_credit → today_credit (直接透传)
      共有:    streak_days, total_credits
    """
    remapped = dict(result)

    # CLAIMED/ALREADY 归一化
    if "credit" in remapped and "today_credit" not in remapped:
        remapped["today_credit"] = remapped.pop("credit")

    # 将 None/缺失 统一为 0（ClawCheckin 契约要求数字）
    for key in ("today_credit", "streak_days", "total_credits"):
        val = remapped.get(key)
        remapped[key] = int(val) if isinstance(val, (int, float)) else 0

    # 保留 expiring（可选）
    exp = remapped.get("expiring")
    if not isinstance(exp, list):
        remapped.pop("expiring", None)

    # 去除不用的辅助字段
    for key in ("credit", "needs_attention", "config_warning",
                "is_streak_day", "next_streak_day", "active",
                "growth_result", "growth"):
        remapped.pop(key, None)

    return remapped


def _ensure_runtime() -> None:
    """把 WorkBuddy 可执行文件路径写进本进程环境变量。

    必须先于一切解密操作——signin.py 子进程靠它解密凭据，跨机镜像的明文提取
    （_wbsync）同样靠它。
    """
    if "WORKBUDDY_EXE" in os.environ:
        return
    for candidate in (
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe"),
        r"D:\WorkBuddy\WorkBuddy.exe",
        r"D:\Program Files\WorkBuddy\WorkBuddy.exe",
    ):
        if os.path.isfile(candidate):
            os.environ["WORKBUDDY_EXE"] = candidate
            return


def main():
    _ensure_runtime()

    # --refresh：纯凭证刷新，只校验凭据不执行签到
    if "--refresh" in sys.argv:
        # 刷新顺带把本机最新凭据同步进跨机镜像（另一台电脑不登录也能用）
        sync_note = ""
        signin_path = _find_signin()
        if signin_path:
            try:
                if _wbsync.update_from_local(signin_path) == "synced":
                    sync_note = "；跨机镜像已更新"
            except Exception:
                pass
        out = _refresh_check()
        if sync_note:
            out["report"] += sync_note
        print(json.dumps(out, ensure_ascii=False))
        return 0 if out["result"] == "ALREADY" else 1

    # --status：只读查询今天签没签（不领取），供面板承认在别处签的到
    if "--status" in sys.argv:
        out = _status_probe()
        print(json.dumps(out, ensure_ascii=False))
        return 0 if out["result"] in ("CLAIMED", "ALREADY") else 1

    signin_path = _find_signin()
    if not signin_path:
        print(json.dumps({
            "result": "NO_AUTH",
            "report": "未找到 WorkBuddy 签到内核 signin.py（内置副本 vendor/workbuddy/ 缺失）。"
                      "请恢复 vendor/workbuddy/signin.py，或 git clone https://github.com/88lin/workbuddy-auto-signin.git "
                      "到项目目录 _ref_workbuddy/，或设置环境变量 WORKBUDDY_SIGNIN_DIR 指向其路径",
            "today_credit": 0,
            "streak_days": 0,
            "total_credits": 0,
        }, ensure_ascii=False))
        return 2

    data, err, _used = _run_with_fallback(signin_path, "auto")
    if data is None:
        if err == "no-python":
            print(json.dumps({
                "result": "ERROR",
                "report": "找不到 python 解释器，请确认 Python 已安装且加入 PATH",
                "today_credit": 0,
                "streak_days": 0,
                "total_credits": 0,
            }, ensure_ascii=False))
            return 1
        if err == "timeout":
            print(json.dumps({
                "result": "ERROR",
                "report": f"签名脚本执行超时（>{SIGNIN_TIMEOUT}s），下次自动重试",
                "today_credit": 0,
                "streak_days": 0,
                "total_credits": 0,
            }, ensure_ascii=False))
            return 1
        # 解析失败时附上 signin.py 的 stderr 尾巴
        print(json.dumps({
            "result": "ERROR",
            "report": f"signin.py stdout 末行非 JSON，请检查客户端登录状态：{_LAST_STDERR}",
            "today_credit": 0,
            "streak_days": 0,
            "total_credits": 0,
        }, ensure_ascii=False))
        return 1

    out = _remap(data)

    # 结果映射
    signin_result = out.get("result", "").upper()
    result_map = {
        "CLAIMED": "CLAIMED",
        "ALREADY": "ALREADY",
        "INACTIVE": "NO_AUTH",      # 活动未开启
        "NETWORK": "NETWORK",
        "TIMEOUT": "NETWORK",
        "NO_SESSION": "NO_AUTH",
        "AUTH_REJECTED": "NO_AUTH",
        "FORBIDDEN": "NO_AUTH",
        "AUTH_ERROR": "NO_AUTH",
        "UNKNOWN": "ERROR",
    }
    out["result"] = result_map.get(signin_result, signin_result if signin_result in (
        "CLAIMED", "ALREADY", "NO_SESSION", "NO_AUTH", "NETWORK", "ERROR") else "ERROR")

    # 已签到场景：保留今日积分数值（取自 signin.py 原始输出，避免显示 +0）
    if out["result"] == "ALREADY":
        actual = int(data.get("today_credit") or data.get("credit") or 0)
        out["report"] = f"今日已签到（+{actual} 积分）"
        out["today_credit"] = 0

    # 成长中心信息（旅行名额/能量等）在已签到场景也保留
    growth_report = data.get("growth", "")
    if growth_report and growth_report not in (out.get("report", ""), ""):
        out["report"] = f"{out.get('report', '')}；{growth_report}"

    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["result"] in ("CLAIMED", "ALREADY") else 1


if __name__ == "__main__":
    sys.exit(main())