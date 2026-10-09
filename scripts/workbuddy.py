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
    "expiring": []   // 可选
  }
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


def main():
    # WorkBuddy 可执行文件路径：写进本进程环境变量，且必须先于一切解密操作——
    # signin.py 子进程靠它解密凭据，跨机镜像的明文提取（_wbsync）同样靠它
    if "WORKBUDDY_EXE" not in os.environ:
        for candidate in (
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe"),
            r"D:\WorkBuddy\WorkBuddy.exe",
            r"D:\Program Files\WorkBuddy\WorkBuddy.exe",
        ):
            if os.path.isfile(candidate):
                os.environ["WORKBUDDY_EXE"] = candidate
                break

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

    env = os.environ.copy()

    # 尽力把本机最新凭据同步进跨机镜像（能解密的一侧负责产出明文镜像），
    # 再决定 signin.py 读本地凭据还是同步镜像（双机轮流使用，见 _wbsync）
    try:
        _wbsync.update_from_local(signin_path)
    except Exception:
        pass
    _auth_path, _auth_source = _wbsync.pick()
    if _auth_source == "mirror":
        env["WORKBUDDY_AUTH_FILE"] = _wbsync.MIRROR_PATH

    # signin.py 默认把日志写到自身同目录；重定向到项目 data/，保持 vendor/ 干净
    if "WORKBUDDY_SIGNIN_LOG" not in env:
        data_dir = os.path.join(PROJECT_ROOT, "data")
        os.makedirs(data_dir, exist_ok=True)
        env["WORKBUDDY_SIGNIN_LOG"] = os.path.join(data_dir, "workbuddy_signin.log")

    try:
        proc = subprocess.run(
            ["python", str(signin_path), "auto"],
            capture_output=True,
            text=True,
            timeout=SIGNIN_TIMEOUT,
            encoding="utf-8",
            errors="replace",
            cwd=os.path.dirname(signin_path),
            env=env,
        )
    except FileNotFoundError:
        print(json.dumps({
            "result": "ERROR",
            "report": "找不到 python 解释器，请确认 Python 已安装且加入 PATH",
            "today_credit": 0,
            "streak_days": 0,
            "total_credits": 0,
        }, ensure_ascii=False))
        return 1
    except subprocess.TimeoutExpired:
        print(json.dumps({
            "result": "ERROR",
            "report": f"签名脚本执行超时（>{SIGNIN_TIMEOUT}s），下次自动重试",
            "today_credit": 0,
            "streak_days": 0,
            "total_credits": 0,
        }, ensure_ascii=False))
        return 1

    data = _parse_signin_output(proc.stdout or "")
    if data is None:
        # 解析失败时检查 stderr
        stderr_tail = (proc.stderr or "")[-300:]
        print(json.dumps({
            "result": "ERROR",
            "report": f"signin.py stdout 末行非 JSON，请检查客户端登录状态：{stderr_tail}",
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