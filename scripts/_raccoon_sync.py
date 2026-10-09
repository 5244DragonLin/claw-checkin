"""小浣熊（商汤）凭据跨机漫游与共享请求层（同步盘双机轮流使用场景）。

背景：小浣熊桌面客户端的登录态在 ~/.box-agent/config/auth.json（明文 JSON），
accessToken 有效期仅约 3 小时，refreshToken 约 30 天；客户端重新登录后文件即
更新，office_identity 等未知字段也在其中。三个文件的分工（与 AutoClaw 的
credshare 同一思路）：
  ~/.box-agent/config/auth.json  桌面端实时登录态（客户端自己维护，优先读）
  auths/raccoon.json             脚本工作副本：续期结果落在这里，也可手动粘贴凭据
  auths/raccoon_sync.json        跨机漫游镜像：access + refreshToken + exp

漫游规则：
  1. 任一来源有未过期的 access 就直接采用，并记入镜像（exp 明显更旧的一轮不
     覆盖镜像——那可能是另一台电脑刚轮换出的更新凭据）；
  2. access 过期/临期（5 分钟窗口）：用 refreshToken 向
     {主站}/api/web/auth/v1/refresh 续期，结果写回工作副本与镜像；来源是桌面端
     登录态时再做一次比较-再写（文件当前 token 仍等于旧值才覆盖，且保留
     office_identity 等未知字段）——客户端重新登录后的新凭据绝不被旧结果盖掉；
  3. 双机轮流使用时镜像随同步盘走：B 机不装客户端也能用 A 机漫游来的会话续期。

协议参考：https://github.com/aimod-cc/agent2api（desktop-tauri providers/raccoon），
接口形态来自对小浣熊桌面端通信的观察，上游随时可能调整。
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import time
import urllib.error
import urllib.request

import _syncio

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK_PATH = os.path.join(PROJECT_ROOT, "auths", "raccoon.json")
MIRROR_PATH = os.path.join(PROJECT_ROOT, "auths", "raccoon_sync.json")
STATE_PATH = os.path.join(PROJECT_ROOT, "data", "raccoon_state.json")

# 小浣熊主站 origin（RACCOON_MAIN_SITE_URL 可覆盖，语义与 agent2api 一致）
DEFAULT_MAIN_SITE = "https://xiaohuanxiong.com"
PATH_REFRESH = "/api/web/auth/v1/refresh"

# 临期主动续期窗口：过期前 5 分钟（agent2api PROACTIVE_REFRESH_MARGIN_MS）
REFRESH_MARGIN = 300.0
# 桌面端身份头：官方客户端启动时携带（desktop 登录积分链路必须）
DESKTOP_HEADERS = [
    ("X-Client-Platform", "desktop-windows"),
    ("X-Client-Version", "v1.0.0"),
]
REQUEST_TIMEOUT = 25


def main_site() -> str:
    value = (os.environ.get("RACCOON_MAIN_SITE_URL") or "").strip().rstrip("/")
    return value or DEFAULT_MAIN_SITE


def desktop_auth_file() -> str:
    """桌面端登录态文件路径（环境变量可覆盖，口径与 agent2api 一致）。"""
    for name in ("RACCOON_BOX_AGENT_CONFIG_DIR", "BOX_AGENT_CONFIG_DIR"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return os.path.join(value, "auth.json")
    return os.path.join(os.path.expanduser("~"), ".box-agent", "config", "auth.json")


# ---------- JWT（只解 payload，不验签；与 agent2api jwt.rs 同口径） ----------

def strip_bearer(token: str) -> str:
    t = (token or "").strip()
    if t[:7].lower() == "bearer ":
        return t[7:].strip()
    return t


def jwt_exp(token: str) -> float:
    """解析 JWT 的 exp（秒）；解析失败返回 0。"""
    try:
        seg = strip_bearer(token).split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
        return float(payload.get("exp") or 0)
    except Exception:
        return 0.0


def jwt_claim(token: str, key: str) -> str:
    try:
        seg = strip_bearer(token).split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
        return str(payload.get(key) or "")
    except Exception:
        return ""


# ---------- 凭据来源与解析 ----------

def _credentials_from_doc(doc: dict) -> dict | None:
    """从任意来源 JSON 里取凭据：access_token/token/auth_token/accessToken 均认。"""
    access = ""
    for key in ("access_token", "token", "auth_token", "accessToken"):
        value = doc.get(key)
        if isinstance(value, str) and strip_bearer(value):
            access = strip_bearer(value)
            break
    if not access:
        return None
    refresh = ""
    for key in ("refresh_token", "refreshToken"):
        value = doc.get(key)
        if isinstance(value, str) and value.strip():
            refresh = value.strip()
            break
    exp = 0.0
    raw_exp = doc.get("exp")
    if isinstance(raw_exp, (int, float)) and raw_exp > 0:
        exp = float(raw_exp)
    if exp <= 0:
        exp = jwt_exp(access)
    return {"access": access, "refresh": refresh, "exp": exp}


def credential_sources() -> list[tuple[str, str, dict]]:
    """按优先级列出全部凭据来源：[(来源名, 路径, 原始JSON)]。"""
    sources: list[tuple[str, str, dict]] = []
    env_file = (os.environ.get("RACCOON_AUTH_FILE") or "").strip()
    if env_file:
        doc = _syncio.load_json(env_file)
        if isinstance(doc, dict):
            sources.append(("env", env_file, doc))
    auth_file = desktop_auth_file()
    doc = _syncio.load_json(auth_file)
    if isinstance(doc, dict):
        sources.append(("desktop", auth_file, doc))
    for name, path in (("work", WORK_PATH), ("mirror", MIRROR_PATH)):
        doc = _syncio.load_json(path)
        if isinstance(doc, dict):
            sources.append((name, path, doc))
    return sources


def write_back_auth_file(path: str, old_access: str, old_refresh: str,
                         new_access: str, new_refresh: str) -> bool:
    """比较-再写：文件当前凭据仍等于旧值时才替换 token 字段，其余原样保留。

    读不到文件或凭据已被更换（客户端重新登录）一律拒绝写入，返回 False。
    """
    doc = _syncio.load_json(path)
    if not isinstance(doc, dict):
        return False
    current = _credentials_from_doc(doc)
    if not current or current["access"] != old_access:
        return False
    if old_refresh and current["refresh"] != old_refresh:
        return False
    doc["access_token"] = new_access
    if new_refresh:
        doc["refresh_token"] = new_refresh
    return _syncio.atomic_write_json(path, doc)


# ---------- 跨机漫游镜像与状态 ----------

def load_mirror() -> dict | None:
    doc = _syncio.load_json(MIRROR_PATH)
    if not isinstance(doc, dict):
        return None
    creds = _credentials_from_doc(doc)
    if not creds or not creds["refresh"]:
        return None
    return creds


def note_mirror(access: str, refresh: str, exp: float) -> bool:
    """把一轮凭据记入镜像（单调守卫：exp 明显更旧则不覆盖）。"""
    if not access or not refresh or exp <= 0:
        return False
    cur = load_mirror()
    if cur and exp < cur["exp"] - 60:
        return False
    doc = {"accessToken": access, "refreshToken": refresh, "exp": exp,
           "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S")}
    return _syncio.atomic_write_json(MIRROR_PATH, doc)


def save_work(access: str, refresh: str, exp: float) -> bool:
    if not access:
        return False
    doc = {"accessToken": access, "refreshToken": refresh, "exp": exp,
           "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S")}
    return _syncio.atomic_write_json(WORK_PATH, doc)


def load_state() -> dict:
    """读取签到状态（自动合并同步盘冲突副本，取 last_date 最新的一份）。"""
    base = {"streak_days": 0, "last_date": "", "last_total": 0}
    directory = os.path.dirname(STATE_PATH)
    stem = os.path.splitext(os.path.basename(STATE_PATH))[0]
    paths = []
    if os.path.isfile(STATE_PATH):
        paths.append(STATE_PATH)
    try:
        for name in os.listdir(directory):
            if name.startswith(stem + "_冲突文件") and name.endswith(".json"):
                paths.append(os.path.join(directory, name))
    except OSError:
        pass
    best, best_key = None, ("", -1, -1)
    for path in paths:
        doc = _syncio.load_json(path)
        if not isinstance(doc, dict):
            continue
        key = (str(doc.get("last_date") or ""),
               float(doc.get("last_total") or 0),
               float(doc.get("streak_days") or 0))
        if key > best_key:
            best, best_key = doc, key
    if best is None:
        return dict(base)
    merged = dict(base)
    merged.update({k: v for k, v in best.items() if k in base})
    for path in paths:
        if os.path.abspath(path) != os.path.abspath(STATE_PATH):
            try:
                os.remove(path)
            except OSError:
                pass
    return merged


def save_state(state: dict) -> bool:
    return _syncio.atomic_write_json(STATE_PATH, state)


# ---------- HTTP（{code, message, data} 信封统一解包） ----------

def api_json(method: str, path: str, token: str, extra_headers=None,
             body: dict | None = None, timeout: int = REQUEST_TIMEOUT) -> tuple:
    """调用小浣熊主站接口，返回 (data, status, err)。

    成功时 data = 信封的 data（缺失/null 时回落整个 payload，与 agent2api
    request_json_ex 的 `payload?.data ?? payload` 同口径）；失败时 data 为 None，
    err 为可读原因。status 保留原值供 401 判断。
    """
    headers = {"Accept": "application/json"}
    if strip_bearer(token):
        headers["Authorization"] = "Bearer " + strip_bearer(token)
    for k, v in (extra_headers or []):
        headers[k] = v
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(main_site() + path, data=data,
                                 headers=headers, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    except urllib.error.URLError as e:
        return None, -1, f"网络不可达：{e.reason}"
    except Exception as e:
        return None, -1, str(e)
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None, status, f"HTTP {status}：响应非 JSON"
    if status == 401:
        return None, 401, "登录态已过期"
    if status >= 400:
        msg = _envelope_message(payload)
        return None, status, f"HTTP {status}：{msg or str(payload)[:120]}"
    if isinstance(payload, dict) and payload.get("code") not in (None, 0):
        return None, status, _envelope_message(payload) or f"code={payload.get('code')}"
    if isinstance(payload, dict) and "data" in payload and payload["data"] is not None:
        return payload["data"], status, ""
    return payload, status, ""


def _envelope_message(payload) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("message", "msg"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def refresh_session(refresh_token: str) -> dict | None:
    """用 refreshToken 换新会话，返回 {access, refresh, exp}；失败返回 None。

    响应缺 refresh_token 时沿用旧值（agent2api 同口径）。
    """
    data, status, err = api_json(
        "POST", PATH_REFRESH, "", extra_headers=[("Content-Type", "application/json")],
        body={"refresh_token": refresh_token}, timeout=30)
    if data is None or not isinstance(data, dict):
        return None
    access = strip_bearer(str(data.get("access_token") or ""))
    if not access:
        return None
    refresh = str(data.get("refresh_token") or "").strip() or refresh_token
    exp = jwt_exp(access)
    if exp <= 0:
        exp = time.time() + 3600
    return {"access": access, "refresh": refresh, "exp": exp}


def resolve_with_refresh() -> dict | None:
    """选出可用凭据；全部临期/过期时逐个尝试续期并写回。

    返回 {source, path, access, refresh, exp}；拿不到返回 None。
    来源优先级：RACCOON_AUTH_FILE > 桌面端登录态 > 工作副本 > 漫游镜像；
    同一来源内 access 的 exp 越新越优先（镜像里可能是另一台电脑刚轮换的）。
    """
    cands = []
    for prio, (name, path, doc) in enumerate(credential_sources()):
        creds = _credentials_from_doc(doc)
        if creds:
            cands.append({"source": name, "path": path, "prio": prio, **creds})
    if not cands:
        return None
    cands.sort(key=lambda c: (-c["exp"], c["prio"]))
    for cand in cands:
        if cand["access"] and cand["exp"] > time.time() + REFRESH_MARGIN:
            if cand["source"] != "mirror":
                note_mirror(cand["access"], cand["refresh"], cand["exp"])
            return cand
    for cand in cands:
        if not cand["refresh"]:
            continue
        renewed = refresh_session(cand["refresh"])
        if not renewed:
            continue
        note_mirror(renewed["access"], renewed["refresh"], renewed["exp"])
        save_work(renewed["access"], renewed["refresh"], renewed["exp"])
        # 桌面端登录态比较-再写：客户端重登后的新凭据不被旧结果盖掉
        if cand["source"] in ("desktop", "env"):
            write_back_auth_file(cand["path"], cand["access"], cand["refresh"],
                                 renewed["access"], renewed["refresh"])
        return {"source": cand["source"], "path": cand["path"], **renewed}
    return None
