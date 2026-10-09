"""WorkBuddy 登录凭据的跨机同步镜像（auths/workbuddy-desktop.info）。

背景：桌面端登录态在 %LOCALAPPDATA% 的 workbuddy-desktop.info，其中 accessToken
是 $wbEncrypted 信封，须由本机安装的 WorkBuddy.exe 解密；解密密钥在客户端安装
目录里，凭据文件原样拷到另一台电脑会 KEY_MISMATCH。

镜像做法：在能解密的一侧把 accessToken 解成明文，连同会话其余字段原样存进项目
auths/ 目录（随同步盘漫游）。accessToken 有效期长达 55 天（auth.expiresIn=
4752000 秒），另一台电脑即使不登录客户端也能直接用。

新旧比较用 auth.expiresAt（毫秒时间戳，凭据文件里明文可读），无须解密即可判断
本地与镜像哪份更新。镜像文件与桌面端凭据同构，signin.py 通过 WORKBUDDY_AUTH_FILE
直接消费。
"""

from __future__ import annotations

import copy
import json
import os
import time

import _syncio

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIRROR_PATH = os.path.join(PROJECT_ROOT, "auths", "workbuddy-desktop.info")

# 镜像视为仍然有效的最短剩余寿命：低于 1 小时就尝试用本地凭据换新
MIRROR_MIN_TTL_SECONDS = 3600


def local_auth_file() -> str | None:
    """本机桌面端凭据文件（与 signin.py 的 Windows 探测口径一致）。"""
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    candidates = [
        os.path.join(local, "CodeBuddyExtension", "Data", "Public", "auth",
                     "workbuddy-desktop.info"),
        os.path.join(home, ".workbuddy", "auth", "workbuddy-desktop.info"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def load_session_file(path: str | None) -> dict | None:
    if not path:
        return None
    doc = _syncio.load_json(path)
    return doc if isinstance(doc, dict) else None


def _normalize_epoch(value) -> float:
    """expiresAt/refreshExpiresAt 为毫秒时间戳，统一换算成秒。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return v / 1000.0 if v > 1e12 else v


def session_expiry(session: dict | None) -> float:
    """会话的 accessToken 过期时刻（秒）；缺失/无法解析返回 0。"""
    if not isinstance(session, dict):
        return 0.0
    return _normalize_epoch((session.get("auth") or {}).get("expiresAt"))


def token_of(session: dict | None):
    """取出 accessToken：明文返回 str，加密信封返回 dict，缺失返回 None。"""
    if not isinstance(session, dict):
        return None
    return (session.get("auth") or {}).get("accessToken")


def pick() -> tuple[str | None, str]:
    """选择 signin.py / 积分脚本应使用的凭据文件，返回 (path, source)。

    source ∈ local / mirror / none。本地与镜像都在时取 expiresAt 较新的一份；
    同为 0（无时间戳可比）时本地优先——本机存在凭据文件本身就代表本机登录过。
    """
    local = local_auth_file()
    mirror_exists = os.path.isfile(MIRROR_PATH)
    local_exp = session_expiry(load_session_file(local)) if local else 0.0
    mirror_exp = session_expiry(load_session_file(MIRROR_PATH)) if mirror_exists else 0.0
    if local and (not mirror_exists or local_exp >= mirror_exp):
        return local, "local"
    if mirror_exists:
        return MIRROR_PATH, "mirror"
    return None, "none"


def sync_mirror(session: dict, plaintext_token: str) -> bool:
    """把已解密的完整会话写入镜像；镜像已明显更新则跳过（单调守卫）。"""
    if not isinstance(session, dict) or not plaintext_token:
        return False
    mirror = load_session_file(MIRROR_PATH)
    if mirror and session_expiry(mirror) > session_expiry(session) + 60:
        return False
    out = copy.deepcopy(session)
    out.setdefault("auth", {})["accessToken"] = plaintext_token
    return _syncio.atomic_write_json(MIRROR_PATH, out)


def maybe_sync_mirror(path: str | None, plaintext_token: str) -> bool:
    """积分脚本在本地凭据解密成功后调用：path 为镜像自身时跳过。"""
    if not path or os.path.abspath(path) == os.path.abspath(MIRROR_PATH):
        return False
    session = load_session_file(path)
    if not session:
        return False
    try:
        return sync_mirror(session, plaintext_token)
    except Exception:
        return False


def update_from_local(signin_path: str) -> str:
    """签到包装层在调 signin.py 前调用：尽力把本地凭据同步进镜像。

    返回状态串（供日志/排查）：synced / mirror-ok / mirror-newer /
    local-plaintext / no-local / resolve-failed。
    """
    local = local_auth_file()
    if not local:
        return "no-local"
    session = load_session_file(local)
    token = token_of(session)
    mirror = load_session_file(MIRROR_PATH) if os.path.isfile(MIRROR_PATH) else None

    if isinstance(token, str):
        # 明文凭据：直接镜像（桌面端旧版未启用加密信封）
        if sync_mirror(session, token):
            return "local-plaintext"
        return "mirror-newer"

    if isinstance(token, dict):
        # 加密信封：镜像里已有未过期的明文会话就不必再跑一次客户端解密
        if mirror and isinstance(token_of(mirror), str) \
                and session_expiry(mirror) > time.time() + MIRROR_MIN_TTL_SECONDS \
                and session_expiry(mirror) >= session_expiry(session) - 60:
            return "mirror-ok"
        plaintext = _resolve_via_signin(signin_path, session)
        if not plaintext:
            return "resolve-failed"
        return "synced" if sync_mirror(session, plaintext) else "resolve-failed"

    return "no-local"


def _resolve_via_signin(signin_path: str, session: dict) -> str | None:
    """复用 vendored signin.py 的解密协议（WorkBuddy.exe helper），拿明文 token。"""
    if not signin_path or not os.path.isfile(signin_path):
        return None
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_wb_signin_kernel", signin_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        resolved = mod.resolve_session(json.loads(json.dumps(session)))
        tok = (resolved.get("auth") or {}).get("accessToken")
        return tok if isinstance(tok, str) and tok else None
    except Exception:
        return None
