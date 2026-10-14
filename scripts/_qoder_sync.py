"""Qoder 凭据解析与共享请求层（中国版 / 国际版）。

协议参考：https://github.com/aimod-cc/agent2api
（desktop-tauri/src-tauri/server/src/server/core/providers/qoder/*）

── 每日权益是「活动」不是「签到接口」──────────────────────────
Qoder 没有 WorkBuddy 那种一天一次的签到端点，每日权益以 **campaign** 形式下发：

  GET  {openapi}/sash/api/v1/me/campaigns?forceRefresh=true
  POST {openapi}/sash/api/v1/me/campaigns/{campaignId}/claim   body {}

  中国版 openapi = https://openapi.qoder.com.cn
  国际版 openapi = https://openapi.qoder.sh

活动行里 `actionType == "CLAIM_BENEFIT"` 的才是可领取的权益，`VIEW_DETAILS`
是纯促销展示、不能领；`claimStatus` 在 `CLAIMABLE` / `CLAIMED` 之间变化，
**领完活动行不会消失**，所以「今天领没领」直接读活动列表即可，无需本地记账。
顶层 `claimable` 不能当判据（领完后它可能仍为 true）。

── 鉴权：最小头束，不打 COSY 签名 ─────────────────────────────
  authorization:  Bearer <accessToken>
  cosy-clienttype: 10      ← 这个头在门控返回内容：用 5（IDE 形态）会让上游
                            返回空活动列表，必须用 10（桌面端形态）
  accept / accept-language / user-agent

国际版另有前置条件：服务端**只对带设备风控头**的请求下发「每天领 100 Credits」
活动，风控三件套由本机 UMID 组件（runtime-info）生成，见 qoder_risk()。

── 凭据从哪来 ────────────────────────────────────────────────
Qoder 客户端把登录态加密存放（Electron safeStorage，Windows 上 DPAPI 密文），
脚本不尝试解密客户端密文，改为按优先级取：

  1. auths/qoder.json      脚本工作副本（自动写入，也可手工粘贴）
  2. auths/qoder_sync.json 跨机漫游镜像（双机轮流使用时随同步盘走）
  3. 环境变量 QODER_ACCESS_TOKEN / QODER_REFRESH_TOKEN / QODER_USER_ID

拿不到凭据时给出 NO_AUTH 并说明怎么补，不猜测、不伪造。
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request

import _syncio

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK_PATH = os.path.join(PROJECT_ROOT, "auths", "qoder.json")
MIRROR_PATH = os.path.join(PROJECT_ROOT, "auths", "qoder_sync.json")

CN = "cn"
GLOBAL = "global"

OPEN_API = {
    CN: "https://openapi.qoder.com.cn",
    GLOBAL: "https://openapi.qoder.sh",
}

# ── 活动 / 签到接口 ──────────────────────────────────────────
CAMPAIGNS_PATH = "/sash/api/v1/me/campaigns"
CAMPAIGNS_QUERY = "?forceRefresh=true"
ACTION_CLAIM_BENEFIT = "CLAIM_BENEFIT"
STATUS_CLAIMABLE = "CLAIMABLE"
STATUS_CLAIMED = "CLAIMED"

# desktop 形态的客户端类型：这个值在门控返回内容，不能用 5（IDE）
SASH_CLIENT_TYPE = "10"
SASH_USER_AGENT = "Qoder"

REQUEST_TIMEOUT = 20
CLAIM_TIMEOUT = 25


def open_api(region: str) -> str:
    return OPEN_API.get(region, OPEN_API[CN])


# ── 凭据 ────────────────────────────────────────────────────

def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _from_doc(doc: dict) -> dict | None:
    """从一份记录里取凭据；字段名兼容 agent2api 的几种写法。"""
    access = ""
    for key in ("accessToken", "access", "token", "access_token"):
        access = str(doc.get(key) or "").strip()
        if access:
            break
    if not access:
        return None
    refresh = ""
    for key in ("refreshToken", "refresh", "refresh_token"):
        refresh = str(doc.get(key) or "").strip()
        if refresh:
            break
    region = str(doc.get("region") or doc.get("mode") or doc.get("edition") or "").strip().lower()
    if region in ("", "intl", "global"):
        region = GLOBAL if region else CN
    elif region not in (CN,):
        region = CN
    user_id = str(doc.get("userId") or doc.get("user_id") or doc.get("uid") or "").strip()
    return {
        "source": "work",
        "region": region,
        "access": access,
        "refresh": refresh,
        "user_id": user_id,
        "name": str(doc.get("nickname") or doc.get("name") or "").strip(),
    }


def credential_sources() -> list[tuple[str, str]]:
    """(来源标签, 路径) 列表，按优先级排序。"""
    return [("auths/qoder.json", WORK_PATH), ("跨机漫游镜像", MIRROR_PATH)]


def resolve_credentials() -> dict | None:
    """按优先级解析凭据，返回 {region, access, refresh, user_id, source} 或 None。"""
    for label, path in credential_sources():
        doc = _read_json(path)
        creds = _from_doc(doc)
        if creds:
            return creds

    access = (os.environ.get("QODER_ACCESS_TOKEN") or "").strip()
    if access:
        region = (os.environ.get("QODER_REGION") or "").strip().lower()
        return {
            "source": "环境变量",
            "region": region if region in (CN, GLOBAL) else CN,
            "access": access,
            "refresh": (os.environ.get("QODER_REFRESH_TOKEN") or "").strip(),
            "user_id": (os.environ.get("QODER_USER_ID") or "").strip(),
            "name": "",
        }
    return None


def save_credentials(creds: dict) -> bool:
    """把凭据写进工作副本与跨机漫游镜像（同步盘双机轮流使用场景）。"""
    payload = {
        "region": creds.get("region") or CN,
        "accessToken": creds.get("access") or "",
        "refreshToken": creds.get("refresh") or "",
        "userId": creds.get("user_id") or "",
        "savedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    ok = False
    for path in (WORK_PATH, MIRROR_PATH):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            ok = _syncio.atomic_write_json(path, payload) or ok
        except Exception:
            continue
    return ok


def note_mirror(creds: dict) -> None:
    """只补写镜像（不动工作副本），用于「镜像里有更新凭据」的场景。"""
    try:
        os.makedirs(os.path.dirname(MIRROR_PATH), exist_ok=True)
        _syncio.atomic_write_json(MIRROR_PATH, {
            "region": creds.get("region") or CN,
            "accessToken": creds.get("access") or "",
            "refreshToken": creds.get("refresh") or "",
            "userId": creds.get("user_id") or "",
            "savedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
    except Exception:
        pass


# ── 请求层 ──────────────────────────────────────────────────

def sash_headers(token: str) -> list[tuple[str, str]]:
    """活动/签到接口的最小头束（无签名）。"""
    return [
        ("authorization", f"Bearer {token}"),
        ("cosy-clienttype", SASH_CLIENT_TYPE),
        ("accept", "application/json"),
        ("accept-language", "zh-CN"),
        ("user-agent", SASH_USER_AGENT),
    ]


def api_json(
    method: str,
    url: str,
    headers: list[tuple[str, str]],
    body: dict | None = None,
    timeout: float = REQUEST_TIMEOUT,
) -> tuple[dict | None, int, str]:
    """发一次请求，返回 (解析后的 JSON, HTTP 状态码, 错误说明)。"""
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = headers + [("content-type", "application/json")]
    request = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            raw = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    except Exception as exc:
        return None, -1, str(exc)

    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text)
    except ValueError:
        return None, status, text[:200] or f"HTTP {status} 返回非 JSON"
    if isinstance(parsed, dict):
        parsed.setdefault("_httpStatus", status)
        return parsed, status, ""
    return None, status, "响应不是 JSON 对象"


# ── 设备授权登录（拿 accessToken）──────────────────────────────
# 与 agent2api 的 PKCE 设备授权同构（oauth.rs）：
#   1. 生成 verifier / challenge / nonce，拼授权页 URL 让用户在浏览器点授权；
#   2. 轮询 {openapi}/api/v1/deviceToken/poll?nonce=..&verifier=..，202/404 表示
#      还没授权，拿到 200 + token 就算成功。
DEVICE_LOGIN_PATH = "/device/selectAccounts"
DEVICE_POLL_PATH = "/api/v1/deviceToken/poll"
MACHINE_ID_PATH = os.path.join(PROJECT_ROOT, "auths", "qoder_machine_id")


def machine_id() -> str:
    """本机标识：优先复用 Qoder 客户端 / qodercli 落盘的，否则持久化一个。"""
    try:
        value = open(MACHINE_ID_PATH, encoding="utf-8").read().strip()
        if value:
            return value
    except OSError:
        pass
    home = os.path.expanduser("~")
    for candidate in (
        os.path.join(home, ".qoder-cn", ".auth", "machine_id"),
        os.path.join(home, ".qoder", ".auth", "machine_id"),
        os.path.join(home, ".qoder-proxy", "machine_id"),
        os.path.join(home, ".qoder", "machine_id"),
    ):
        try:
            value = open(candidate, encoding="utf-8").read().strip()
            if value:
                return value
        except OSError:
            continue

    import uuid

    value = str(uuid.uuid4())
    try:
        os.makedirs(os.path.dirname(MACHINE_ID_PATH), exist_ok=True)
        with open(MACHINE_ID_PATH, "w", encoding="utf-8") as handle:
            handle.write(value)
    except OSError:
        pass
    return value


def device_login_url(region: str) -> tuple[str, str, str]:
    """生成授权页地址；返回 (授权页 URL, nonce, verifier)。"""
    import base64 as b64
    import hashlib
    import secrets
    import uuid

    verifier = b64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge = b64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    nonce = str(uuid.uuid4()).replace("-", "")
    from urllib.parse import urlencode

    query = urlencode({
        "challenge": challenge,
        "challenge_method": "S256",
        "machine_id": machine_id(),
        "nonce": nonce,
    })
    web = "https://qoder.com.cn" if region == CN else "https://qoder.com"
    return f"{web}{DEVICE_LOGIN_PATH}?{query}", nonce, verifier


def poll_device_token(region: str, nonce: str, verifier: str) -> dict | None:
    """轮询一次授权结果：返回凭据 dict（已授权）或 None（还没授权）。"""
    from urllib.parse import urlencode

    query = urlencode({"nonce": nonce, "verifier": verifier, "challenge_method": "S256"})
    url = f"{open_api(region)}{DEVICE_POLL_PATH}?{query}"
    headers = [
        ("cosy-version", "1.0.1"),
        ("cosy-clienttype", "5"),
        ("user-agent", "qoder-local-proxy"),
        ("accept", "application/json"),
    ]
    payload, status, _err = api_json("GET", url, headers)
    if payload is None:
        if status in (202, 404):
            return None
        return None
    token = str(payload.get("token") or "").strip()
    if not token:
        return None
    refresh = str(payload.get("refresh_token") or "").strip()
    user_id = str(payload.get("user_id") or "").strip()
    return {
        "region": region,
        "access": token,
        # agent2api 同款：refresh 串后两段固定是「用户 | 设备」
        "refresh": f"{refresh}|{user_id}|{machine_id()}" if refresh else "",
        "user_id": user_id,
        "name": str(payload.get("name") or payload.get("username") or "").strip(),
        "source": "device-login",
    }


# ── 设备风控身份（国际版签到必需）─────────────────────────────

UMID_ENV = {CN: 0, GLOBAL: 4}
RISK_CACHE: dict[str, tuple[float, dict]] = {}
RISK_TTL = 50 * 60


def _umid_component(region: str) -> str | None:
    """找本机 UMID 组件（runtime-info）。客户端与 qodercli 两种布局都找。"""
    home = os.path.expanduser("~")
    exe = "runtime-info.exe" if os.name == "nt" else "runtime-info"

    # 1) qodercli 解压出来的：~/.qoder-cn/.bin/umid-*  或  ~/.qoder/.bin/umid-*
    bin_dirs = [".qoder-cn", ".qoder"] if region == CN else [".qoder", ".qoder-cn"]
    for name in bin_dirs:
        base = os.path.join(home, name, ".bin")
        if not os.path.isdir(base):
            continue
        for entry in sorted(os.listdir(base)):
            candidate = os.path.join(base, entry, exe)
            if entry.startswith("umid-") and os.path.isfile(candidate):
                return candidate

    # 2) 客户端安装目录：<安装目录>/resources/umid/runtime-info.exe
    names = ["Qoder CN"] if region == CN else ["Qoder"]
    roots = [
        os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), name) for name in names
    ] + [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", name) for name in names
    ] + [os.path.join("D:\\", name) for name in names] + [os.path.join("C:\\", name) for name in names]
    for root in roots:
        candidate = os.path.join(root, "resources", "umid", exe)
        if os.path.isfile(candidate):
            return candidate
        candidate = os.path.join(root, "resources", exe)
        if os.path.isfile(candidate):
            return candidate
    return None


def qoder_risk(region: str, uid: str) -> dict | None:
    """生成设备风控身份 {machineToken, machineCode, machineType}；失败返回 None。

    调用方式照抄 agent2api：runtime-info <env>，Windows/macOS 另带
    --account-stdin 并把 {"account": "<uid>"} 写进 stdin，首行输出 JSON。
    """
    if not uid:
        return None
    cached = RISK_CACHE.get(f"{region}:{uid}")
    if cached and time.time() - cached[0] < RISK_TTL:
        return cached[1]

    exe = _umid_component(region)
    if exe is None:
        return None
    try:
        import subprocess

        args = [exe, str(UMID_ENV.get(region, 0))]
        stdin_data = None
        if os.name == "nt" or "darwin" in (os.sys.platform or ""):
            args.append("--account-stdin")
            stdin_data = json.dumps({"account": uid}) + "\n"
        completed = subprocess.run(
            args,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=25,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        line = (completed.stdout or "").splitlines()[0].strip() if completed.stdout else ""
        parsed = json.loads(line)
    except Exception:
        return None

    identity = {}
    for key in ("machineToken", "machineCode", "machineType"):
        value = str(parsed.get(key) or "").strip()
        if not value:
            return None
        identity[key] = value
    RISK_CACHE[f"{region}:{uid}"] = (time.time(), identity)
    return identity


def augmented_headers(region: str, token: str, uid: str) -> tuple[list[tuple[str, str]], str | None]:
    """叠风控头；返回 (头束, 风控不可用原因)。"""
    headers = sash_headers(token)
    headers.append(("cosy-version", "1.0.1"))
    arch = "x86_64" if os.environ.get("PROCESSOR_ARCHITECTURE", "").endswith("64") else "unknown"
    platform = "win32" if os.name == "nt" else "darwin"
    headers.append(("cosy-machineos", f"{arch}_{platform}"))
    identity = qoder_risk(region, uid)
    if identity is None:
        reason = ("本机没有 Qoder 客户端 / qodercli 的 UMID 组件"
                  if _umid_component(region) is None else "UMID 组件生成身份失败")
        return headers, reason
    headers.append(("cosy-machinetoken", identity["machineToken"]))
    headers.append(("cosy-machinecode", identity["machineCode"]))
    headers.append(("cosy-machinetype", identity["machineType"]))
    return headers, None
