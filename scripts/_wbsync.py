"""WorkBuddy 登录凭据的跨机同步镜像（auths/workbuddy-desktop.info）。

背景：桌面端登录态在 %LOCALAPPDATA% 的 workbuddy-desktop.info，其中 accessToken
是 $wbEncrypted 信封，须由本机安装的 WorkBuddy.exe 解密；解密密钥在客户端安装
目录里，凭据文件原样拷到另一台电脑会 KEY_MISMATCH。

镜像做法：在能解密的一侧把 accessToken 解成明文，连同会话其余字段原样存进项目
auths/ 目录（随同步盘漫游）。accessToken 有效期长达 55 天（auth.expiresIn=
4752000 秒），另一台电脑即使不登录客户端也能直接用。

新旧比较用 auth.expiresAt（毫秒时间戳，凭据文件里明文可读），无须解密即可判断
本地与镜像哪份更新。**但过期时间不等于能用**：桌面端每次刷新会话后，服务端会
作废先前签发的 accessToken，镜像里那份的日期可能更晚、实际已被拒绝（HTTP 401）。
因此选择顺序按「谁一定还在台上」而不是「谁日期更晚」：本机凭据可用就用本机，
镜像只在本机凭据缺失或过期时兜底。镜像文件与桌面端凭据同构，signin.py 通过
WORKBUDDY_AUTH_FILE 直接消费。
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

    source ∈ local / mirror / none。**本机优先**：本机凭据存在且未过期就直接用。
    桌面端会持续刷新它，是唯一「一定还在台上」的一份；镜像只是给没登录的电脑
    兜底，本机凭据缺失或已过期时才轮到它。

    早先按 expiresAt 取较新一份，会被镜像里那份自报日期更晚、实则已被服务端
    作废的明文 token 长期劫持（全员 HTTP 401），故改为就近优先。
    """
    now = time.time()
    local = local_auth_file()
    if local:
        exp = session_expiry(load_session_file(local))
        if exp > now:
            return local, "local"
    if os.path.isfile(MIRROR_PATH):
        return MIRROR_PATH, "mirror"
    if local:
        # 本机凭据已过期，但报「已过期，请重新登录」比「未找到凭据」更准确
        return local, "local"
    return None, "none"


def alternate(path: str | None) -> str | None:
    """返回与 path 互为备份的另一份凭据文件；没有或与 path 同一份则 None。

    一份被服务端拒绝时，用它去试另一份（本机 ↔ 镜像）。
    """
    for cand in (local_auth_file(), MIRROR_PATH):
        if not cand or not os.path.isfile(cand):
            continue
        if path and os.path.abspath(cand) == os.path.abspath(path):
            continue
        return cand
    return None


def sync_mirror(session: dict, plaintext_token: str, force: bool = False) -> bool:
    """把已解密的完整会话写入镜像。

    force=False 时保留单调守卫：镜像明显更新就跳过。守卫只比 expiresAt，
    而 expiresAt 能被服务端作废的事实推翻，所以「某份凭据刚被服务端接受」
    这类有实证的场景必须 force=True。
    """
    if not isinstance(session, dict) or not plaintext_token:
        return False
    mirror = load_session_file(MIRROR_PATH)
    if not force and mirror and session_expiry(mirror) > session_expiry(session) + 60:
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


def update_from_local(signin_path: str, force: bool = False) -> str:
    """签到包装层在调 signin.py 前调用：尽力把本机凭据同步进镜像。

    force=True 表示本机这份凭据已被服务端接受过（有实证），跳过「镜像日期更晚
    就不用换」的乐观判断，直接覆盖。

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
        if sync_mirror(session, token, force=force):
            return "local-plaintext"
        return "mirror-newer"

    if isinstance(token, dict):
        # 加密信封：镜像里已有未过期的明文会话就不必再跑一次客户端解密
        if not force and mirror and isinstance(token_of(mirror), str) \
                and session_expiry(mirror) > time.time() + MIRROR_MIN_TTL_SECONDS \
                and session_expiry(mirror) >= session_expiry(session) - 60:
            return "mirror-ok"
        plaintext = _resolve_via_signin(signin_path, session)
        if not plaintext:
            return "resolve-failed"
        return "synced" if sync_mirror(session, plaintext, force=force) else "resolve-failed"

    return "no-local"


def heal_mirror(auth_file: str | None, signin_path: str) -> bool:
    """某份凭据刚被服务端成功接受：用它刷新镜像，让另一台电脑也能用。

    与 update_from_local 的差别是先有实证再写，因此绕过单调守卫——守卫只看
    expiresAt，而「日期更晚」既不保证能用，也不该挡住一份刚验证过可用的凭据。
    """
    if not auth_file or not signin_path:
        return False
    session = load_session_file(auth_file)
    if not session:
        return False
    token = token_of(session)
    plaintext = token if isinstance(token, str) else _resolve_via_signin(signin_path, session)
    return heal_mirror_with_token(auth_file, plaintext)


def heal_mirror_with_token(auth_file: str | None, plaintext_token: str | None) -> bool:
    """调用方手里已有明文 token（刚被服务端接受）时直接回写镜像。"""
    if not auth_file or not plaintext_token:
        return False
    if os.path.abspath(auth_file) == os.path.abspath(MIRROR_PATH):
        return False
    session = load_session_file(auth_file)
    if not session:
        return False
    return sync_mirror(session, plaintext_token, force=True)


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
