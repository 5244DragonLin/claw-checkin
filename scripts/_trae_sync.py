"""Trae 凭据解析 / 续期 / 登录原语共享层 · ClawCheckin 接入脚本

协议参考：https://github.com/aimod-cc/agent2api
（desktop-tauri/.../core/providers/trae/{credentials,refresh,oauth,device,headers}.rs）

── Trae 签到链路一句话 ───────────────────────────────────────
Trae（SOLO 通道）每日签到打的是 ug 一族端点：
  POST https://api.trae.cn/trae/api/v2/ug/checkin_credits/status   body {}
  POST https://api.trae.cn/trae/api/v2/ug/checkin_credits/claim    body {}
一轮 = status → claim → 同设备号回查，恒 3 发（未签到时）。判「真领到」不看
claim 的 code:0（当日已签过的账号对任何 device_id 都回 code:0，这是假成功陷阱），
只看回查后 checked_in / did_checked_in 有没有翻转。这些逻辑在 trae.py 里，
本模块只负责「凭据 + 续期 + 组头 + 设备密钥 + 登录上下文」这些共享原语。

── 凭据从哪来 ────────────────────────────────────────────────
Trae 客户端把登录态存在本机，脚本不尝试解密客户端密文，改为按优先级取：

  1. auths/trae.json      脚本工作副本（--login / --auth-token 写入）
  2. auths/trae_sync.json 跨机漫游镜像（双机轮流使用时随同步盘走）
  3. 环境变量 TRAE_ACCESS_TOKEN / TRAE_REFRESH_TOKEN / TRAE_UID 等

── 续期（refreshToken 路径）──────────────────────────────────
POST {apiHost}/cloudide/api/v3/trae/oauth/ExchangeToken
body {"ClientID":.., "RefreshToken":.., "ClientSecret":"-", "UserID":""}

ClientID 按 variant 选：solo/solo-intl → SOLO_CLIENT_ID，其余 → IDE_CLIENT_ID。
归属可不一致（参考实现实测：库里 variant=solo、凭据里的 ClientID 是非 solo 的），
所以续期按 [凭据里记的, variant 推的, 另一把] 依次试，只有上游回
400 + 10101/"not matched to the client" 才换一把。失败不改写原凭据。

── 设备密钥（登录必需）──────────────────────────────────────
官方按 DevicePublicKey + DeviceID/MachineID 做设备绑定，公钥为空会被判
HTTP 401 / 业务码 20405。登录时现生成一套 EC P-256（SPKI 公钥进请求、
PKCS#8 私钥落凭据）。私钥只存不用，但必须存——抹掉等于弄断绑定关系。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import ssl
import time
import urllib.error
import urllib.request

import _syncio

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK_PATH = os.path.join(PROJECT_ROOT, "auths", "trae.json")
MIRROR_PATH = os.path.join(PROJECT_ROOT, "auths", "trae_sync.json")

# ── 谱系（variant）──────────────────────────────────────────
# 本家只接 CN 谱系（solo / cn）：国际版（intl / solo-intl）上游没有签到通道。
SOLO = "solo"
CN = "cn"
INTL_VARIANTS = ("intl", "solo-intl")

# ── OAuth 应用 ClientID（参考实现 constants.go，逐字对齐）────
SOLO_CLIENT_ID = "en1oxy7wnw8j9n"
IDE_CLIENT_ID = "ono9krqynydwx5"

# ── 设备画像常量（参考实现 constants.go，2026-09-03 真实客户端抓包）──
PLUGIN_VERSION = "1.0.0"
DEVICE_NAME = "DESKTOP-CPASOLO"
DEVICE_TYPE = "windows"
DEVICE_BRAND = "83DG"
OS_VERSION = "Windows 11 Pro"
ENV = "prod"
APP_TYPE = "trae"
IDE_VERSION = "0.1.61"
IDE_VERSION_CODE = "20260820"
APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
CLIENT_USER_AGENT = "Trae/0.1.61"

# ── 登录 / 续期端点 ─────────────────────────────────────────
# 签到与续期都打这条族的 host：api.trae.cn（与账号 apiHost 无关）
UG_HOST = "https://api.trae.cn"
FALLBACK_HOSTS = ["https://api.trae.cn", "https://api.trae.com.cn"]
REFRESH_PATH = "/cloudide/api/v3/trae/oauth/ExchangeToken"
AUTH_CODE_EXCHANGE_PATH = "/trae/api/v3/oauth/ExchangeToken"
CALLBACK_PATH = "/authorize"

# GetLoginGuidance 候选（依次试；全失败兜到 DEFAULT_LOGIN_HOST）
GUIDANCE_URLS_CN = [
    "https://api.trae.cn/cloudide/api/v3/trae/GetLoginGuidance",
    "https://api.trae.com.cn/cloudide/api/v3/trae/GetLoginGuidance",
    "https://www.trae.cn/cloudide/api/v3/trae/GetLoginGuidance",
]
DEFAULT_LOGIN_HOST = "https://api.trae.cn"
# 授权码换证的候选 origin（回调没带回 loginHost 时用；www.* 派生 api.*）
CN_DEFAULT_ORIGINS = ["https://api.trae.cn", "https://api.trae.com.cn", "https://www.trae.cn"]
ACCOUNT_API_ORIGINS = ["https://api.trae.cn", "https://api.trae.com.cn"]

REQUEST_TIMEOUT = 20
REFRESH_TIMEOUT = 30

# ── 积分/权益端点（参考实现 usage.rs，同一 ug 画像族）────────
# 只读查询余额，不领取任何东西；请求体同样是空对象。
EP_ENT_USAGE = "/trae/api/v2/pay/ide_user_ent_usage"


# ── 凭据解析 ────────────────────────────────────────────────

def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _first_str(doc: dict, keys: list[str]) -> str:
    for key in keys:
        value = str(doc.get(key) or "").strip()
        if value:
            return value
    return ""


def _from_doc(doc: dict) -> dict | None:
    """从一份记录里取凭据；字段名兼容 agent2api 的几种写法。"""
    access = _first_str(doc, ["accessToken", "access", "token", "access_token"])
    if not access:
        return None
    refresh = _first_str(doc, ["refreshToken", "refresh", "refresh_token"])
    variant = _first_str(doc, ["variant", "mode", "edition"]).lower()
    if variant not in (SOLO, CN, "intl", "solo-intl"):
        # 认不出的写法按 solo 收（SOLO 通道）；intl 两种写法必须原样保留，
        # 否则国际版凭据会被当成 CN 去打注定失败的签到。
        variant = SOLO
    expires_raw = doc.get("expiresAt") or doc.get("expires_at") or doc.get("tokenExpireAt")
    expires_at = _normalize_expires_at(expires_raw)
    return {
        "source": "work",
        "variant": variant,
        "access": access,
        "refresh": refresh,
        "expires_at": expires_at,
        "uid": _first_str(doc, ["uid", "userId", "user_id"]),
        "nickname": _first_str(doc, ["nickname", "name"]),
        "machine_id": _first_str(doc, ["machineId", "machine_id"]),
        "device_id": _first_str(doc, ["deviceId", "device_id"]),
        "device_public_key": _first_str(doc, ["devicePublicKey", "device_public_key"]),
        "device_private_key": _first_str(doc, ["devicePrivateKey", "device_private_key"]),
        "auth_client_id": _first_str(doc, ["authClientId", "auth_client_id"]),
        "domain": _first_str(doc, ["domain"]) or "trae.cn",
        "api_host": _first_str(doc, ["apiHost", "api_host"]) or UG_HOST,
    }


def _normalize_expires_at(value) -> int:
    """秒/毫秒归一（同 agent2api normalize_expires_at）。"""
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return 0
    if ms <= 0:
        return 0
    return ms * 1000 if ms < 100_000_000_000 else ms


def credential_sources() -> list[tuple[str, str]]:
    """(来源标签, 路径) 列表，按优先级排序。"""
    return [("auths/trae.json", WORK_PATH), ("跨机漫游镜像", MIRROR_PATH)]


def resolve_credentials() -> dict | None:
    """按优先级解析凭据，返回 credential dict 或 None。"""
    for _label, path in credential_sources():
        creds = _from_doc(_read_json(path))
        if creds:
            return creds

    access = (os.environ.get("TRAE_ACCESS_TOKEN") or "").strip()
    if access:
        variant = (os.environ.get("TRAE_VARIANT") or "").strip().lower()
        if variant not in (SOLO, CN, "intl", "solo-intl"):
            variant = SOLO
        return {
            "source": "环境变量",
            "variant": variant,
            "access": access,
            "refresh": (os.environ.get("TRAE_REFRESH_TOKEN") or "").strip(),
            "expires_at": 0,
            "uid": (os.environ.get("TRAE_UID") or "").strip(),
            "nickname": "",
            "machine_id": (os.environ.get("TRAE_MACHINE_ID") or "").strip(),
            "device_id": (os.environ.get("TRAE_DEVICE_ID") or "").strip(),
            "device_public_key": "",
            "device_private_key": "",
            "auth_client_id": "",
            "domain": "trae.cn",
            "api_host": UG_HOST,
        }
    return None


def save_credentials(creds: dict) -> bool:
    """把凭据写进工作副本与跨机漫游镜像（同步盘双机轮流使用场景）。

    空值不覆盖：手工粘贴常只给 accessToken，把 refreshToken 写成空串等于让
    凭据「到期即死」（参考实现 trae_accounts.rs 的「空值不覆盖」纪律）。
    """
    existing = {}
    for path in (WORK_PATH, MIRROR_PATH):
        existing.update(_read_json(path))
    payload = {
        "variant": creds.get("variant") or SOLO,
        "accessToken": creds.get("access") or "",
        "refreshToken": creds.get("refresh") or existing.get("refreshToken") or "",
        "expiresAt": creds.get("expires_at") or 0,
        "uid": creds.get("uid") or "",
        "nickname": creds.get("nickname") or "",
        "machineId": creds.get("machine_id") or "",
        "deviceId": creds.get("device_id") or "",
        "devicePublicKey": creds.get("device_public_key") or "",
        "devicePrivateKey": creds.get("device_private_key") or "",
        "authClientId": creds.get("auth_client_id") or "",
        "domain": creds.get("domain") or "trae.cn",
        "apiHost": creds.get("api_host") or UG_HOST,
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


# ── 请求层 ──────────────────────────────────────────────────

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


# ── 组头（ug 画像，参考实现 ug_headers）─────────────────────

def uuid_v4() -> str:
    """RFC 4122 v4 形态的随机 id（login_trace_id / machine_id 都用它）。"""
    import uuid

    return str(uuid.uuid4())


def _random_hex(n: int) -> str:
    import secrets

    return secrets.token_hex(n)


def request_id() -> str:
    """uuid v4 形状的请求 id（X-Request-Id）。"""
    return uuid_v4()


def trace_id(req: str) -> str:
    """X-TT-Trace-Id（抓包格式 00-<32hex>-<16hex>-01，第二段取自请求 id）。"""
    return f"00-{_random_hex(16)}-{req.replace('-', '')[:16] or '0000000000000000'}-01"


def _platform_name_for(variant: str) -> str:
    if variant == "solo":
        return "TRAE SOLO CN"
    if variant == "cn":
        return "TRAE CN"
    if variant == "intl":
        return "TRAE"
    if variant == "solo-intl":
        return "TRAE SOLO"
    return "TRAE CN"


def _package_type_for(variant: str) -> str:
    return "stable" if variant in ("intl", "solo-intl") else "stable_cn"


def ug_headers(variant: str, access_token: str, device_id: str,
               scheme: str = "Cloud-IDE-JWT") -> list[tuple[str, str]]:
    """ug 族（签到 / 余额）请求头。

    ⚠️ 只有 Authorization 一个头随鉴权方案变化（换 Bearer 兜底时只换这一个），
    其余画像头逐字一致——混改会撞上「画像对不上」的风控（参考实现同款纪律）。
    """
    req = request_id()
    headers = [
        ("Content-Type", "application/json"),
        ("Accept", "*/*"),
        ("User-Agent", f"VSCode 1.107.1 ({_platform_name_for(variant)})"),
        ("X-User-Region", "CN"),
        ("Accept-Language", "zh-CN"),
        ("Package-Type", _package_type_for(variant)),
        ("X-Lgw-Req-Sdk-Type", "3"),
        ("X-Market-Client-Id", "VSCode 1.107.1"),
        ("X-Device-Brand", DEVICE_BRAND),
        ("X-Device-Type", "windows"),
        ("X-OS-Version", OS_VERSION),
        ("App-Version", IDE_VERSION),
        ("X-Request-Id", req),
        ("X-TT-Trace-Id", trace_id(req)),
        ("Sec-Fetch-Dest", "empty"),
        ("Sec-Fetch-Mode", "no-cors"),
        ("Sec-Fetch-Site", "none"),
        ("Authorization", f"{scheme} {access_token}"),
    ]
    if device_id:
        headers.append(("X-Device-Id", device_id))
    return headers


# ── 设备密钥（EC P-256，登录必需）───────────────────────────

def generate_device_key_pair() -> tuple[str, str] | None:
    """生成一套设备密钥，返回 (公钥 PEM, 私钥 PEM)；失败返回 None。

    PEM 行宽按 Go encoding/pem 的 64 字符折行，与官方客户端产物可互读。
    """
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        return None
    try:
        key = ec.generate_private_key(ec.SECP256R1())
        public_der = key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        private_der = key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return (_pem("PUBLIC KEY", public_der), _pem("PRIVATE KEY", private_der))
    except Exception:
        return None


def _pem(label: str, der: bytes) -> str:
    encoded = base64.b64encode(der).decode("ascii")
    lines = [encoded[i:i + 64] for i in range(0, len(encoded), 64)]
    body = "\n".join(lines)
    return f"-----BEGIN {label}-----\n{body}\n-----END {label}-----\n"


# ── PKCE ────────────────────────────────────────────────────

def pkce_pair() -> tuple[str, str]:
    """S256：48 随机字节 → base64url 无填充 verifier；challenge = SHA-256 同编码。"""
    import secrets

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def random_digits(length: int) -> str:
    """纯数字随机串（device_id 是 16 位数字串）。"""
    import secrets

    return "".join(str(b % 10) for b in secrets.token_bytes(length))


# ── URL 编码（Go url.QueryEscape 同规则）────────────────────

def url_encode(value: str) -> str:
    """application/x-www-form-urlencoded 转义（字母数字与 -_.~ 原样，空格成 +）。

    ⚠️ * 要转义（%2A）—— 与 encodeURIComponent 那一组不同，别抄错。
    """
    out = []
    for byte in value.encode("utf-8"):
        char = chr(byte)
        if char.isalnum() or char in "-_.~":
            out.append(char)
        elif char == " ":
            out.append("+")
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)


# ── variant 表 ──────────────────────────────────────────────

def client_id_for(variant: str) -> str:
    return SOLO_CLIENT_ID if variant in ("solo", "solo-intl") else IDE_CLIENT_ID


def other_client_id(client_id: str) -> str | None:
    if client_id == SOLO_CLIENT_ID:
        return IDE_CLIENT_ID
    if client_id == IDE_CLIENT_ID:
        return SOLO_CLIENT_ID
    return None


def auth_from_for(variant: str) -> str:
    return "solo" if variant == "solo" else "trae"


def platform_code_for(variant: str) -> str:
    return "SOLO_PC" if variant == "solo" else "IDE_PC"


def hide_saas_login_for(variant: str) -> bool:
    return variant == "solo"


def device_brand_for_context(device_type: str) -> str:
    return "Apple" if device_type == "mac" else "Microsoft"


# ── 授权地址（verification_uri，参数顺序与编码由向量钉住）──

def verification_uri(login_host: str, context: dict) -> str:
    """拼授权页地址。

    ⚠️ auth_callback_url 刻意**不**编码（官方页面对它做正则匹配，编码后就
    匹配不上 → 登录页连出现的机会都没有）；其余按 url_encode。顺序保留。
    """
    login_host = _ensure_https_scheme(login_host)
    parts: list[str] = []

    def push(key: str, value: str, encode: bool) -> None:
        parts.append(f"{key}={url_encode(value) if encode else value}")

    push("login_version", "1", False)
    push("auth_from", auth_from_for(context["variant"]), False)
    push("login_channel", "native_ide", False)
    push("plugin_version", PLUGIN_VERSION, True)
    push("auth_type", "local", False)
    push("client_id", client_id_for(context["variant"]), False)
    push("redirect", "0", False)
    push("login_trace_id", context["login_trace_id"], True)
    push("auth_callback_url", context["callback_url"], False)
    push("machine_id", context["machine_id"], True)
    push("device_id", context["device_id"], True)
    push("x_device_id", context["device_id"], True)
    push("x_machine_id", context["machine_id"], True)
    push("x_device_brand", DEVICE_BRAND, True)
    push("x_device_type", DEVICE_TYPE, True)
    push("x_os_version", OS_VERSION, True)
    push("x_env", ENV, True)
    push("x_app_version", IDE_VERSION, True)
    push("x_app_type", APP_TYPE, True)
    push("code_challenge", context["code_challenge"], True)
    push("code_challenge_method", "S256", False)
    if hide_saas_login_for(context["variant"]):
        push("hide_saas_login", "true", False)
    return f"{login_host.rstrip('/')}/authorization?{'&'.join(parts)}"


def _ensure_https_scheme(raw: str) -> str:
    trimmed = raw.strip()
    if not trimmed:
        return ""
    if trimmed.startswith("http://") or trimmed.startswith("https://"):
        return trimmed
    return f"https://{trimmed.lstrip('/')}"


def candidate_api_origins(login_host: str) -> list[str]:
    """换证候选 origin：loginHost 派生的在前（www.* 额外派生 api.*），CN 默认源垫后。"""
    origins: list[str] = []
    derived = _ensure_https_scheme(login_host)
    if derived:
        host = derived.split("://", 1)[1].split("/", 1)[0]
        if host:
            scheme = derived.split("://", 1)[0]
            origins.append(f"{scheme}://{host}")
            if host.startswith("www."):
                origins.append(f"{scheme}://api.{host[4:]}")
    origins.extend(CN_DEFAULT_ORIGINS)
    seen: set[str] = set()
    out: list[str] = []
    for origin in origins:
        if origin not in seen:
            seen.add(origin)
            out.append(origin)
    return out


def auth_code_exchange_urls(login_host: str) -> list[str]:
    """授权码换证候选：两把官方源在前，candidate_api_origins 垫后，去重保序。"""
    urls = [origin.rstrip("/") + AUTH_CODE_EXCHANGE_PATH for origin in ACCOUNT_API_ORIGINS]
    urls.extend(origin.rstrip("/") + AUTH_CODE_EXCHANGE_PATH
                for origin in candidate_api_origins(login_host))
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def refresh_candidates(api_host: str) -> list[str]:
    """续期候选 URL：凭据 apiHost 优先，两个兜底 host 垫后。"""
    hosts = [api_host.strip().rstrip("/")] + FALLBACK_HOSTS
    seen: set[str] = set()
    out: list[str] = []
    for host in hosts:
        if not host:
            continue
        if not host.startswith(("http://", "https://")):
            host = f"https://{host}"
        if host not in seen:
            seen.add(host)
            out.append(host.rstrip("/") + REFRESH_PATH)
    return out


# ── 续期（refreshToken 路径）────────────────────────────────

def refresh_body(client_id: str, refresh_token: str) -> dict:
    return {"ClientID": client_id, "RefreshToken": refresh_token,
            "ClientSecret": "-", "UserID": ""}


def parse_refresh_response(body: str) -> tuple[str, str, int] | None:
    """解析换证响应 → (access, refresh, expires_ms)；失败返回 None。

    上游把令牌放在 Result 里，字段名有两套写法，逐个试；TokenExpireAt 是毫秒。
    """
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return None
    for key in ("Result", "result"):
        object_ = parsed.get(key)
        if not isinstance(object_, dict):
            continue
        token = _first_str(object_, ["Token", "token", "AccessToken", "accessToken"])
        if not token:
            continue
        refresh = _first_str(object_, ["RefreshToken", "refreshToken", "refresh_token"])
        expires = 0
        for field in ("TokenExpireAt", "tokenExpireAt", "ExpiresAt", "expiresAt"):
            raw = object_.get(field)
            if isinstance(raw, (int, float)):
                expires = _normalize_expires_at(int(raw))
                break
        return token, refresh, expires
    return None


def response_client_id(body: str) -> str:
    """读上游承认的 ClientID（Result.ClientID）；读不到返回空串（保留原值）。"""
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return ""
    for key in ("Result", "result"):
        object_ = parsed.get(key)
        if isinstance(object_, dict):
            value = _first_str(object_, ["ClientID", "clientId", "client_id"])
            if value:
                return value
    return ""


def refresh_client_ids(creds: dict) -> list[str]:
    """续期该试的 ClientID 候选（顺序 = 可信度）。"""
    variant_id = client_id_for(creds.get("variant") or SOLO)
    candidates: list[str] = []
    recorded = (creds.get("auth_client_id") or "").strip()
    if recorded:
        candidates.append(recorded)
    candidates.append(variant_id)
    other = other_client_id(variant_id)
    if other:
        candidates.append(other)
    out: list[str] = []
    for item in candidates:
        if item and item not in out:
            out.append(item)
    return out


def _attribution_reject(status: int, body: str) -> bool:
    """上游是不是在说「这枚令牌不属于你报的这个 client」（只有这种失败值得换一把）。"""
    if status != 400:
        return False
    text = body.lower()
    return "10101" in text or "not matched to the client" in text


def refresh_once(creds: dict, urls: list[str]) -> dict | None:
    """打上游换新凭据，返回新凭据 dict；失败返回 None（原凭据不受影响）。

    失败不改写任何字段：旧的 refreshToken 仍可重试。
    """
    return refresh_once_ex(creds, urls)[0]


def refresh_once_ex(creds: dict, urls: list[str]) -> tuple[dict | None, str]:
    """同 refresh_once，但把失败原因原样带回（供 1001 换发失败时的文案）。

    返回 (新凭据 dict | None, 失败原因)。成功时原因为空串。
    """
    refresh = (creds.get("refresh") or "").strip()
    if not refresh:
        return None, "凭据里没有 refreshToken，无法换发"
    headers = [("User-Agent", CLIENT_USER_AGENT)]
    errors: list[str] = []
    for client_id in refresh_client_ids(creds):
        body = refresh_body(client_id, refresh)
        wrong_attribution = False
        for url in urls:
            payload, status, err = api_json("POST", url, headers, body,
                                            timeout=REFRESH_TIMEOUT)
            label = f"{url}（ClientID {client_id}）"
            if payload is None:
                if _attribution_reject(status, err):
                    wrong_attribution = True
                errors.append(f"{label} => HTTP {status} {err[:160]}")
                continue
            if status >= 400:
                if _attribution_reject(status, json.dumps(payload)):
                    wrong_attribution = True
                errors.append(f"{label} => HTTP {status}")
                continue
            parsed = parse_refresh_response(json.dumps(payload))
            if parsed is None:
                errors.append(f"{label} => 响应里没有 access token")
                continue
            token, new_refresh, expires_ms = parsed
            next_creds = dict(creds)
            next_creds["access"] = token
            if new_refresh:
                next_creds["refresh"] = new_refresh
            next_creds["expires_at"] = expires_ms
            acknowledged = response_client_id(json.dumps(payload))
            next_creds["auth_client_id"] = acknowledged or client_id
            return next_creds, ""
        if not wrong_attribution:
            break
    return None, "；".join(errors[-3:]) or "全部候选 host 与 ClientID 都失败"


def needs_refresh(creds: dict) -> bool:
    """临期 24h 窗口 + 签发龄 15 天（参考实现 needs_refresh 两条判据）。"""
    if not creds.get("refresh"):
        return False
    expires = creds.get("expires_at") or 0
    if expires <= 0:
        return False
    return expires - time.time() * 1000 < 24 * 3600 * 1000


def renew_if_due(creds: dict) -> dict:
    """临期才换发；否则原样返回。"""
    if needs_refresh(creds):
        renewed = refresh_once(creds, refresh_candidates(creds.get("api_host") or UG_HOST))
        return renewed if renewed else creds
    return creds


# ── 登录上下文 ──────────────────────────────────────────────

def new_login_context(variant: str, port: int) -> dict:
    """生成一轮登录的上下文（PKCE 对 + 设备指纹 + 回调地址）。

    回调地址形状是死的：官方授权页前端把 auth_callback_url 硬校验成
    ^http://127.0.0.1:<port>/authorize$ —— 端口必须先 bind 成功再拼。
    """
    key_pair = generate_device_key_pair()
    if key_pair is None:
        raise RuntimeError("生成设备密钥对失败（请确认已安装 cryptography 包）")
    verifier, challenge = pkce_pair()
    return {
        "login_trace_id": uuid_v4(),
        "code_verifier": verifier,
        "code_challenge": challenge,
        "device_id": random_digits(16),
        "machine_id": uuid_v4(),
        "callback_url": f"http://127.0.0.1:{port}{CALLBACK_PATH}",
        "variant": variant,
        "device_public_pem": key_pair[0],
        "device_private_pem": key_pair[1],
    }


def device_info(context: dict) -> dict:
    """DeviceInfo 体（官方客户端形状；DevicePublicKey 空值会被判设备绑定拒绝）。"""
    return {
        "DeviceID": context["device_id"],
        "MachineID": context["machine_id"],
        "PlatformCode": platform_code_for(context["variant"]),
        "DeviceType": "PC",
        "DeviceName": DEVICE_NAME,
        "DeviceModel": DEVICE_BRAND,
        "ClientVersion": IDE_VERSION,
        "DevicePublicKey": context["device_public_pem"],
        "DeviceBrand": device_brand_for_context(DEVICE_TYPE),
        "DeviceCPU": "",
        "OSInfo": DEVICE_TYPE,
        "OSVersion": OS_VERSION,
    }


# ── 登录链：guidance / 回调 / 换证 / 身份回读 ───────────────

USER_INFO_PATH = "/cloudide/api/v3/trae/GetUserInfo"


def extract_login_host(body: str) -> str:
    """从 guidance 响应里找登录 host（层次顺序照参考实现：顶层 → Result/result
    → data → data.Result/result，每层按同一串候选键）。找不到返回空串。"""
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    keys = ["LoginHost", "loginHost", "LoginURL", "loginUrl", "login_url"]

    def pick(value: dict) -> str:
        for key in keys:
            text = str(value.get(key) or "")
            if text:
                return text
        return ""

    def layer(value: dict, key: str) -> dict:
        nested = value.get(key)
        return nested if isinstance(nested, dict) else {}

    candidates = [parsed, layer(parsed, "Result"), layer(parsed, "result"),
                  layer(parsed, "data"), layer(layer(parsed, "data"), "Result"),
                  layer(layer(parsed, "data"), "result")]
    for candidate in candidates:
        found = pick(candidate)
        if found:
            return found
    return ""


def request_login_guidance() -> str:
    """问上游「该去哪个登录页」。全挂时兜到默认 host（CN 不因它阻塞登录）。"""
    body = {"loginTraceID": "", "login_trace_id": ""}
    headers = [("User-Agent", f"Trae/{PLUGIN_VERSION} antigravity-cockpit-tools")]
    for url in GUIDANCE_URLS_CN:
        payload, status, _err = api_json("POST", url, headers, body, timeout=5)
        if payload is not None and status < 400:
            found = extract_login_host(json.dumps(payload))
            if found:
                return _ensure_https_scheme(found)
    return DEFAULT_LOGIN_HOST


def _percent_decode(value: str) -> str:
    """query 语义解码：`+` 成空格、`%XX` 还原；认不出的 `%` 原样留着。"""
    out: list[int] = []
    index = 0
    bytes_ = value.encode("utf-8", "replace")
    while index < len(bytes_):
        byte = bytes_[index]
        if byte == ord("+"):
            out.append(32)
            index += 1
        elif byte == ord("%") and index + 2 < len(bytes_):
            try:
                out.append(int(bytes_[index + 1:index + 3].decode("ascii"), 16))
                index += 3
            except (ValueError, UnicodeDecodeError):
                out.append(byte)
                index += 1
        else:
            out.append(byte)
            index += 1
    return bytes(out).decode("utf-8", "replace")


def query_pairs(query: str) -> list[tuple[str, str]]:
    """切 query 并做一次百分号还原（保留重复键与顺序）。"""
    pairs: list[tuple[str, str]] = []
    for pair in query.split("&"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        pairs.append((_percent_decode(key), _percent_decode(value)))
    return pairs


def _lookup(pairs: list[tuple[str, str]], keys: list[str]) -> str:
    for key in keys:
        for name, value in pairs:
            if name == key and value.strip():
                return value
    return ""


def parse_callback(query: str) -> dict:
    """解析授权回调 query（键名候选集合是上游历史包袱，顺序即优先级）。

    返回 {auth_code, refresh_token, login_host, user_tag, error}。
    错误分支先于其余字段：error / isRedirect=false 时其余一律留空。
    """
    pairs = query_pairs(query)
    error_keys = ["error", "error_code", "err", "errorCode"]
    error = ""
    for key in error_keys:
        for name, value in pairs:
            if name == key and value.strip():
                error = f"oauth callback error: {name}={value}"
                break
        if error:
            break
    if not error and _lookup(pairs, ["isRedirect"]) == "false":
        error = "oauth callback: isRedirect=false"
    if error:
        return {"auth_code": "", "refresh_token": "", "login_host": "",
                "user_tag": "", "error": error}
    auth_code = _lookup(pairs, ["authCode", "auth_code", "AuthCode",
                                "authorization_code", "code"])
    if not auth_code:
        raw = _lookup(pairs, ["authCodeInfo", "auth_code_info", "AuthCodeInfo"])
        auth_code = _extract_auth_code(raw)
    return {
        "auth_code": auth_code,
        "refresh_token": _lookup(pairs, ["refreshToken", "refresh_token",
                                         "RefreshToken", "refresh-token"]),
        "login_host": _lookup(pairs, ["loginHost", "login_host", "LoginHost",
                                      "host", "consoleHost"]),
        "user_tag": _lookup(pairs, ["userTag", "user_tag", "UserTag"]),
        "error": "",
    }


def callback_resolves(callback: dict) -> bool:
    """这次回调是否把登录推进到可判定的状态（否则监听要继续等）。"""
    return bool(callback.get("error") or callback.get("auth_code")
                or callback.get("refresh_token"))


def _extract_auth_code(raw: str) -> str:
    """authCodeInfo 里可能藏的是 JSON 串（可能再套一层引号），递归找码。"""
    text = raw.strip()
    for _ in range(2):
        if text.startswith('"'):
            try:
                text = json.loads(text)
            except ValueError:
                return ""
        else:
            break
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return ""
    keys = ["authCode", "auth_code", "AuthCode", "AuthCodeToken", "code"]

    def walk(value) -> str:
        if isinstance(value, dict):
            for key in keys:
                text = str(value.get(key) or "")
                if text.strip():
                    return text
            for nested in value.values():
                found = walk(nested)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = walk(item)
                if found:
                    return found
        return ""

    return walk(parsed)


UID_KEYS = ["UserID", "userId", "uid", "UID", "user_id"]
NICKNAME_KEYS = ["ScreenName", "screenName", "Nickname", "nickname",
                 "Name", "name", "displayName"]


def callback_identity(query: str) -> dict:
    """从回调 query 的 userInfo 回显读身份（GetUserInfo 挂掉时的第二来源）。"""
    pairs = query_pairs(query)
    raw = _lookup(pairs, ["userInfo", "user_info", "UserInfo", "userinfo"])
    found = _identity_from_json(raw, 0)
    return {"uid": found[0], "nickname": found[1]}


def _identity_from_json(raw: str, depth: int) -> tuple[str, str]:
    if depth > 2 or not raw.strip():
        return "", ""
    text = raw.strip()
    if text.startswith('"'):
        try:
            inner = json.loads(text)
        except ValueError:
            return "", ""
        return _identity_from_json(inner, depth + 1)
    if not text.startswith("{"):
        return "", ""
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return "", ""

    def pick(value: dict, keys: list[str]) -> str:
        for key in keys:
            text = str(value.get(key) or "").strip()
            if text:
                return text
        return ""

    return pick(parsed, UID_KEYS), pick(parsed, NICKNAME_KEYS)


def unknown_uid_fallback(variant: str) -> str:
    """每个 variant 一个**固定**的 unknown 身份（重复登录覆盖同一条记录，
    而不是每次登录新造一个账号）。"""
    if variant == "intl":
        return "unknown-intl"
    if variant == "solo":
        return "solo-unknown"
    return "unknown-cn"


def exchange_auth_code(context: dict, auth_code: str, login_host: str) -> tuple[dict | None, str]:
    """授权码换证：返回 ({access, refresh, expires_at, client_id}, 失败原因)。

    响应里只有 refreshToken 时用它的值当 accessToken 兜底（参考实现同一条）。
    """
    body = {
        "ClientID": client_id_for(context["variant"]),
        "AuthCode": auth_code,
        "CodeVerifier": context["code_verifier"],
        "IDEVersion": IDE_VERSION,
        "DeviceInfo": device_info(context),
    }
    headers = [
        ("x-cloudide-token", ""),
        ("User-Agent", f"Trae/{PLUGIN_VERSION} antigravity-cockpit-tools"),
    ]
    errors: list[str] = []
    for url in auth_code_exchange_urls(login_host):
        payload, status, err = api_json("POST", url, headers, body,
                                        timeout=REFRESH_TIMEOUT)
        if payload is None:
            errors.append(f"{url} => HTTP {status} {err[:120]}")
            continue
        if status >= 400:
            errors.append(f"{url} => HTTP {status}")
            continue
        parsed = parse_refresh_response(json.dumps(payload))
        if parsed is None:
            errors.append(f"{url} => 响应里没有令牌")
            continue
        token, refresh, expires_ms = parsed
        access = token or refresh
        if not access:
            errors.append(f"{url} => 响应里没有任何令牌")
            continue
        return {"access": access, "refresh": refresh, "expires_at": expires_ms,
                "client_id": response_client_id(json.dumps(payload))}, ""
    return None, "；".join(errors[-3:]) or "全部换证候选都失败"


def get_user_info(creds: dict) -> dict:
    """GetUserInfo 读账号身份（uid / nickname / enterprise_id）。失败返回空 dict。

    这是**读数**不是门槛：拿不到照常落盘（参考实现同一条）。
    """
    body = {"ReqSource": "IDE", "IDEVersion": IDE_VERSION}
    headers = [
        ("X-Cloudide-Token", creds.get("access") or ""),
        ("User-Agent", CLIENT_USER_AGENT),
    ]
    hosts = [creds.get("api_host") or UG_HOST] + FALLBACK_HOSTS
    seen: set[str] = set()
    for host in hosts:
        if not host or host in seen:
            continue
        seen.add(host)
        url = host.rstrip("/") + USER_INFO_PATH
        payload, status, _err = api_json("POST", url, headers, body,
                                         timeout=REQUEST_TIMEOUT)
        if payload is None or status >= 400:
            continue
        source = None
        for key in ("Result", "result"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                source = nested
                break
        if source is None:
            source = payload

        def pick(value: dict, keys: list[str]) -> str:
            for key in keys:
                text = str(value.get(key) or "").strip()
                if text:
                    return text
            return ""

        return {"uid": pick(source, UID_KEYS),
                "nickname": pick(source, NICKNAME_KEYS),
                "enterprise_id": pick(source, ["EnterpriseID", "enterpriseId",
                                                "TenantID", "tenantId"])}
    return {}


# ── 积分余额（ide_user_ent_usage，只读）─────────────────────
#
# 上游把「还剩多少积分」拆成两类读数：
#   · `user_entitlement_pack_list` 里每个权益包的 `quota.credits_limit`
#     （授予额）与 `usage.credits_amount`（已用量）—— SOLO 积分制真正扣的
#     就是这一笔；
#   · 顶层 `usage_summary`（total_amount / consumed_amount / consumption_ratio）
#     —— 官方自己算的同一个池子，不逐包取整。
# 主口径按参考实现取逐包求和（实测与官方汇总一致，两边都是 4600），只在
# 逐包拿不到任何 `credits_limit` 时才退到官方汇总。
#
# 三条不能「顺手美化」的读数纪律（参考实现 usage.rs 头部，都有实测依据）：
#   1. **缺失 ≠ 0**：上游没给 `credits_limit` 与给了 0 是两回事。拿前者当
#      后者，就是那个「免费账户渲染成剩 0 积分」的 bug。
#   2. **-1 是不限，不是耗尽**：积分池用 -1 表达无上限。
#   3. 每个包的 `end_time`（秒级 epoch）是该包失效的时刻，正好是过期日历
#      要的到期日；没有领取台账也能给出临期。

CREDITS_UNLIMITED = -1


def _as_number(value) -> float | None:
    """上游数值宽容：整数 / 浮点 / 数字字符串都收下，其它一律算「没给」。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _as_int(value) -> int | None:
    number = _as_number(value)
    return None if number is None else int(number)


def _quota_of(node) -> dict:
    """一层 quota → 已知字段（参考实现 quota_of）。"""
    if not isinstance(node, dict):
        return {}
    return {"credits_limit": _as_int(node.get("credits_limit"))}


def _extra_quota(extra: dict, key: str) -> dict:
    """`product_extra.<key>.quota` 那一层。"""
    part = extra.get(key)
    part = part if isinstance(part, dict) else {}
    return _quota_of(part.get("quota"))


def _effective_quota(pack: dict) -> dict:
    """三层探测：base → subscription → package，取第一层带 `credits_limit` 的。

    上游可能只往其中一层放字段（实测免费包三层都带 SOLO 标志、赠送包只有
    base 与 package），固定读某一层会漏。
    """
    for key in ("base_quota", "subscription_quota", "package_quota"):
        quota = pack.get(key) or {}
        if quota.get("credits_limit") is not None:
            return quota
    return {}


def parse_packs(payload: dict) -> list[dict]:
    """`user_entitlement_pack_list` → 包列表（保持上游给的顺序）。"""
    entries = payload.get("user_entitlement_pack_list")
    if not isinstance(entries, list):
        return []
    packs: list[dict] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        base = entry.get("entitlement_base_info")
        base = base if isinstance(base, dict) else {}
        extra = base.get("product_extra")
        extra = extra if isinstance(extra, dict) else {}
        usage = entry.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        packs.append({
            "product_type": _as_int(base.get("product_type")) or 0,
            # 上游给秒级 epoch；>1e11 的当毫秒用（参考实现同一处置）
            "end_time": _as_int(base.get("end_time")) or 0,
            "is_hide": base.get("is_hide") is True,
            # 缺省与 null 都算「未取消」：判反会把整张清单剔空
            "status": _as_int(base.get("status")),
            "base_quota": _quota_of(base.get("quota")),
            "subscription_quota": _extra_quota(extra, "subscription_extra"),
            "package_quota": _extra_quota(extra, "package_extra"),
            "credits_amount": _as_number(usage.get("credits_amount")),
            "title": str(entry.get("display_desc") or "").strip(),
        })
    return packs


def _is_active_pack(pack: dict) -> bool:
    """`product_type == 3` 是兑换码活动包，上游明确排除（参考实现同一过滤）。"""
    return pack["product_type"] != 3 and not pack["is_hide"] and pack["status"] != 3


def official_credit(payload: dict) -> dict | None:
    """顶层 `usage_summary`（官方汇总）：总额 / 已用 / 消耗比。"""
    summary = payload.get("usage_summary")
    if not isinstance(summary, dict):
        return None
    total = _as_number(summary.get("total_amount"))
    consumed = _as_number(summary.get("consumed_amount"))
    if total is None or consumed is None:
        return None
    return {"total": total, "consumed": consumed,
            "ratio": _as_number(summary.get("consumption_ratio"))}


def credits_pool(payload: dict, packs: list[dict] | None = None) -> dict:
    """积分池剩余：`Σ max(credits_limit - credits_amount, 0)`，逐包四舍五入。

    返回 `{"remain", "known", "unlimited", "source"}`：`known=False` 表示上游
    既没给可读额度也没有官方汇总（界面该显示「—」而不是 0）；
    `unlimited=True` 时 `remain` 是 -1，不参与求和。
    """
    packs = parse_packs(payload) if packs is None else packs
    billing = payload.get("is_credits_billing") is True
    active = [pack for pack in packs if _is_active_pack(pack)]
    has_field = any(_effective_quota(pack).get("credits_limit") is not None
                    for pack in active)
    if not active:
        # 一个包都没有：`is_credits_billing` 为真时官方按 0 展示
        return {"remain": 0, "known": billing, "unlimited": False, "source": "pack"}
    if not has_field and not billing:
        return {"remain": 0, "known": False, "unlimited": False, "source": "pack"}

    unlimited = False
    total = 0
    for pack in active:
        limit = _effective_quota(pack).get("credits_limit")
        if limit is None:
            continue          # 官方 `?? 0`：没有这个键就贡献 0
        if limit == CREDITS_UNLIMITED:
            unlimited = True
            continue
        used = pack.get("credits_amount") or 0.0
        left = max(float(limit) - float(used), 0.0)
        total += int(left + 0.5)   # 官方 Math.round
    if unlimited:
        return {"remain": CREDITS_UNLIMITED, "known": True, "unlimited": True,
                "source": "pack"}
    if has_field:
        return {"remain": total, "known": True, "unlimited": False, "source": "pack"}
    # 一个包的 credits_limit 都没有、但官方说是积分计费：退到官方汇总
    official = official_credit(payload)
    if official is not None:
        left = max(official["total"] - official["consumed"], 0.0)
        return {"remain": int(left + 0.5), "known": True, "unlimited": False,
                "source": "official"}
    return {"remain": 0, "known": billing, "unlimited": False, "source": "pack"}


def credit_buckets(packs: list[dict]) -> dict[str, int]:
    """按包名合并「还剩多少」：`{display_desc: 剩余}`，只收还有余额的包。

    明细与日历共用这一份口径，保证两处加得起来等于卡片余额。
    """
    buckets: dict[str, int] = {}
    for pack in packs:
        if not _is_active_pack(pack):
            continue
        limit = _effective_quota(pack).get("credits_limit")
        if limit is None or limit == CREDITS_UNLIMITED or limit <= 0:
            continue
        left = int(max(float(limit) - float(pack.get("credits_amount") or 0.0), 0.0) + 0.5)
        if left <= 0:
            continue
        title = str(pack.get("title") or "").strip() or "权益包"
        buckets[title] = buckets.get(title, 0) + left
    return buckets


def credit_expiring(packs: list[dict]) -> list[dict]:
    """把每个还有余额的权益包摊成过期日历的一行。

    到期日取该包的 `end_time`；按**本机时区**换算成「那一天到期」（官方界面
    也按用户本地日历展示）。`amount` 用该包剩余，与 `credits_pool` 同一个
    `max(limit - used, 0)` 口径，所以日历合计与卡片余额对得上。
    """
    rows: list[dict] = []
    for pack in packs:
        if not _is_active_pack(pack):
            continue
        limit = _effective_quota(pack).get("credits_limit")
        if limit is None or limit == CREDITS_UNLIMITED or limit <= 0:
            continue
        end_time = pack.get("end_time") or 0
        if end_time <= 0:
            continue
        seconds = end_time / 1000 if end_time > 100_000_000_000 else end_time
        try:
            day = time.strftime("%Y-%m-%d", time.localtime(seconds))
        except (OverflowError, OSError, ValueError):
            continue
        left = int(max(float(limit) - float(pack.get("credits_amount") or 0.0), 0.0) + 0.5)
        if left <= 0:
            continue
        rows.append({"date": day, "amount": left,
                     "source": pack.get("title") or "权益包"})
    return rows


def fetch_ent_usage(creds: dict) -> tuple[dict | None, int, str]:
    """读一次权益包清单（只读，请求体恒空对象、走 ug 画像头）。"""
    url = UG_HOST + EP_ENT_USAGE
    headers = ug_headers(creds.get("variant") or SOLO,
                         str(creds.get("access") or ""),
                         str(creds.get("device_id") or ""))
    return api_json("POST", url, headers, body={})


def read_credit(creds: dict) -> dict:
    """高层读数：请求 + 解析 + 汇总。

    成功时返回 `{"ok": True, "remain", "known", "unlimited", "basis",
    "official", "expiring", "packs"}`；失败时返回 `{"ok": False, "kind",
    "status", "error"}`，`kind` 分三档（`auth` / `network` / `error`）——
    调用方据此决定是报「重新登录」还是「网络问题」，而不是一律报失败。

    读不到时**不拿 0 冒充「没有余额」**：0 与「没读到」在面板上是两种读数。
    """
    payload, status, err = fetch_ent_usage(creds)
    if payload is None:
        kind = "network" if status in (-1, 429, 502, 503, 504) else "error"
        return {"ok": False, "kind": kind, "status": status,
                "error": err or f"HTTP {status} 返回非 JSON"}
    if status in (401, 403):
        return {"ok": False, "kind": "auth", "status": status,
                "error": f"HTTP {status}：登录态被上游拒绝"}
    if status >= 400:
        return {"ok": False, "kind": "error", "status": status,
                "error": f"HTTP {status}：{err or '请求被拒'}"}
    code = _as_int(payload.get("code"))
    if code is not None and code != 0:
        message = str(payload.get("msg") or payload.get("message") or "").strip()
        # 1001 = 服务端已作废这张 JWT（本地到期时间看不出来），按鉴权失败处置
        kind = "auth" if code == 1001 else "error"
        return {"ok": False, "kind": kind, "status": status,
                "error": f"上游返回业务码 {code}：{message or '未给文案'}"}
    packs = parse_packs(payload)
    pool = credits_pool(payload, packs)
    return {
        "ok": True,
        "status": status,
        "remain": pool["remain"],
        "known": pool["known"],
        "unlimited": pool["unlimited"],
        "basis": pool["source"],
        "official": official_credit(payload),
        "expiring": credit_expiring(packs),
        "packs": packs,
    }
