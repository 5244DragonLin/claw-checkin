"""AutoClaw 权威积分查询 · ClawCheckin 隐藏积分源

与 scripts/autoclaw.py（纯签到）分工：本脚本只查官方钱包，不调用签到接口。
面板在每次签到后与启动时自动补跑本脚本，用权威余额刷新卡片上的「总积分」，
并把各笔积分的到期时间写入过期日历。

凭证：auths/autoclaw.json（兼容旧名 autoclaw-cn.json）；
jwt 自动提取（优先级）：
  1. AutoClaw 2 桌面端：解密 %APPDATA%/AutoClaw-official 的加密凭据库
     （Chromium os_crypt：DPAPI 保护的 AES-256-GCM，仅 Windows，需 cryptography 包）；
     jwt 过期且客户端未运行时，自动用 refreshToken 调官方 /userapi/v1/agent-refresh
     续期并写回凭据库——完全无需打开客户端
  2. 旧版 / OpenClaw：~/.openclaw-autoclaw/openclaw.json 中的有效 JWT

输出契约（末行 JSON）：
  {
    "result": "ALREADY|NO_AUTH|NETWORK|ERROR",
    "report": "可读汇报",
    "today_credit": 0,
    "streak_days": 0,
    "total_credits": 3310,
    "expiring": [ { "date": "2026-09-27", "amount": 110, "source": "每日签到" } ]
  }
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _autoclaw_credsync as credsync  # noqa: E402  同目录共享模块：跨机会话漫游镜像

APP_ID = "100003"
CLIENT_VER = "1.18.5"
TASK_ID = "daily_signin"
USERAPI_BASE = {
    "cn": "https://autoglm-acceleration-api.zhipuai.cn",
    "global": "https://autoglm-api.autoglm.ai",
}
PATH_TASK_COMPLETE = "/autoclaw-proxy/proxy/autoclaw-task-complete"
# 官方钱包接口（与 AutoClaw 客户端"积分详情"同源），返回 total_balance = 账户总积分
PATH_WALLETS = "/agent-assetmgr/api/v2/wallets"
# 官方钱包实例接口：返回各笔积分的 balance / expires_at（即将过期积分按此口径）
PATH_WALLET_INSTANCES = "/agent-assetmgr/api/v1/wallet-instances"
REQUEST_TIMEOUT = 20


# 自动探测 ClawCheckin 项目根下的 auths/ 目录，也支持环境变量覆盖
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_AUTH_DIR = os.environ.get("AUTOCLAW_AUTH_DIR") or os.path.join(PROJECT_ROOT, "auths")
# 签名盐值：原为仓库内硬编码，现从环境变量 AUTOCLAW_SIGN_SALT 读取；
# 未设置时回退 auths/autoclaw_sign_salt.txt（本地文件，已在 .gitignore 的 auths/ 内）
SIGN_SALT = os.environ.get("AUTOCLAW_SIGN_SALT") or ""
if not SIGN_SALT:
    try:
        with open(os.path.join(DEFAULT_AUTH_DIR, "autoclaw_sign_salt.txt"), encoding="utf-8") as _f:
            SIGN_SALT = _f.read().strip()
    except OSError:
        SIGN_SALT = ""
STATE_FILE = os.path.join(PROJECT_ROOT, "data", "autoclaw_state.json")
# AutoClaw 2 桌面端（Electron）的凭据库与刷新端点（release/cn 环境）
AUTOCCLAW2_DIR = os.path.join(os.environ.get("APPDATA", ""), "AutoClaw-official")
AUTOCCLAW2_API_BASE = "https://autoglm-acceleration-api.zhipuai.cn"
AUTOCCLAW2_REFRESH_PATH = "/userapi/v1/agent-refresh"
AUTOCCLAW2_PROCESSES = ("AutoClaw2.exe", "AutoClaw.exe")
OPENCLAW_JSON = os.path.join(os.path.expanduser("~"), ".openclaw-autoclaw", "openclaw.json")
OPENCLAW_RUNTIME = os.path.join(os.path.expanduser("~"), ".openclaw-autoclaw", "openclaw.runtime.json")


def _sign(ts: str) -> str:
    return hashlib.md5(f"{APP_ID}&{ts}&{SIGN_SALT}".encode()).hexdigest()


def _bearer(jwt: str) -> str:
    t = jwt.strip()
    return t if t.lower().startswith("bearer ") else "Bearer " + t


def _build_headers(jwt: str) -> dict:
    ts = str(int(time.time()))
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Version": CLIENT_VER,
        "X-Tm": "mac",
        "X-Product": "autoclaw",
        "X-Client-Type": "pc",
        "X-Channel": "official",
        "X-Auth-Appid": APP_ID,
        "X-Auth-TimeStamp": ts,
        "X-Auth-Sign": _sign(ts),
        "X-Trace-Id": str(uuid.uuid4()),
        "X-Lang": "en",
        "Authorization": _bearer(jwt),
        "User-Agent": f"AutoClaw/{CLIENT_VER}",
    }


def _load_auth(auth_dir: str) -> dict | None:
    """读取唯一凭证文件 auths/autoclaw.json（兼容旧名 autoclaw-cn.json）。"""
    for name in ("autoclaw.json", "autoclaw-cn.json"):
        path = os.path.join(auth_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except Exception:
            continue
        jwt = (doc.get("accessToken") or "").strip()
        if not jwt:
            continue
        return {
            "path": path,
            "region": doc.get("region") or "cn",
            "jwt": jwt,
        }
    return None


def _refresh_from_openclaw(auth_dir: str) -> bool:
    """从 openclaw.json 提取有效 JWT，写入 auths/。返回 True 表示已刷新。"""
    for src in (OPENCLAW_JSON, OPENCLAW_RUNTIME):
        if not os.path.isfile(src):
            continue
        try:
            with open(src, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            continue
        zai = cfg.get("models", {}).get("providers", {}).get("zai", {})
        best = None
        for m in zai.get("models", []):
            auth = m.get("headers", {}).get("X-Authorization", "")
            jwt = auth.replace("Bearer ", "") if auth.startswith("Bearer ") else ""
            if not jwt or not jwt.startswith("ey") or jwt.count(".") != 2:
                continue
            try:
                payload_b64 = jwt.split(".")[1]
                payload_b64 += "=" * (-len(payload_b64) % 4)
                payload = json.loads(__import__("base64").urlsafe_b64decode(
                    payload_b64.replace("-", "+").replace("_", "/")))
                exp = payload.get("exp", 0)
                if exp > __import__("time").time() + 300:
                    if best is None or exp > best["exp"]:
                        best = {"jwt": jwt, "exp": exp}
            except Exception:
                continue
        if best:
            os.makedirs(auth_dir, exist_ok=True)
            dst = os.path.join(auth_dir, "autoclaw.json")
            doc = {
                "accessToken": "Bearer " + best["jwt"],
                "region": "cn",
                "userId": "from-openclaw",
                "userName": "openclaw-auto",
            }
            with open(dst, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
            return True
    return False


def _jwt_exp(jwt: str) -> float:
    """解析 JWT 的 exp（秒），失败返回 0。"""
    try:
        seg = jwt.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
        return float(payload.get("exp", 0))
    except Exception:
        return 0.0


def _dpapi_decrypt(data: bytes) -> bytes | None:
    """Windows DPAPI 解密（CryptUnprotectData，当前用户作用域）。"""
    import ctypes
    import ctypes.wintypes as wt

    class _BLOB(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("pb", ctypes.POINTER(ctypes.c_char))]

    blob_in = _BLOB(len(data), ctypes.cast(
        ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    blob_out = _BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        return None
    try:
        return ctypes.string_at(blob_out.pb, blob_out.cb)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pb)


def _read_autoclaw2_vault() -> dict | None:
    """解密 AutoClaw 2 桌面端凭据库，返回凭据材料；失败返回 None。

    存储位置：%APPDATA%/AutoClaw-official/accounts/<hash>/account-credentials.enc
    文件格式：b"v10" + AES-256-GCM（nonce 12 字节 + 密文 + tag 16 字节）；
    AES 密钥存于同目录 Local State 的 os_crypt.encrypted_key（DPAPI 保护）。
    依赖 cryptography 包做 GCM；仅 Windows（macOS/Linux 的 safeStorage 走钥匙串，
    无法离线解密，退回 openclaw.json 或手动凭据）。
    """
    if os.name != "nt":
        return None
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        return None
    local_state = os.path.join(AUTOCCLAW2_DIR, "Local State")
    accounts_dir = os.path.join(AUTOCCLAW2_DIR, "accounts")
    if not (os.path.isfile(local_state) and os.path.isdir(accounts_dir)):
        return None
    try:
        with open(local_state, encoding="utf-8") as f:
            wrapped = base64.b64decode(json.load(f)["os_crypt"]["encrypted_key"])
    except Exception:
        return None
    if not wrapped.startswith(b"DPAPI"):
        return None
    aes_key = _dpapi_decrypt(wrapped[5:])
    if not aes_key:
        return None

    active = ""
    try:
        with open(os.path.join(AUTOCCLAW2_DIR, "device", "active-product-account.json"),
                  encoding="utf-8") as f:
            active = str(json.load(f).get("accountKey") or "")
    except Exception:
        pass

    best = None  # 无活跃账号指针时，取 accessToken exp 最新的账号
    for name in os.listdir(accounts_dir):
        try:
            enc_path = os.path.join(accounts_dir, name, "account-credentials.enc")
            with open(enc_path, "rb") as f:
                enc = f.read()
            if not enc.startswith(b"v10"):
                continue
            plain = AESGCM(aes_key).decrypt(enc[3:15], enc[15:], None)
            doc = json.loads(plain.decode("utf-8"))
            access = str(doc.get("accessToken") or "").strip()
            refresh = str(doc.get("refreshToken") or "").strip()
            if not access or not refresh:
                continue
            entry = {"enc_path": enc_path, "aes_key": aes_key,
                     "revision": int(doc.get("revision") or 1),
                     "access": access, "refresh": refresh,
                     "exp": _jwt_exp(access.split(" ", 1)[-1])}
            if name == active:
                return entry
            if best is None or entry["exp"] > best["exp"]:
                best = entry
        except Exception:
            continue  # 单个账号解析失败不影响其余
    return best


def _autoclaw2_running() -> bool:
    """客户端是否正在运行（其内存态持有凭据缓存与修订号，并发写回会导致
    客户端下次刷新失败而要求重新登录，故运行期间不碰凭据库）。"""
    try:
        for name in AUTOCCLAW2_PROCESSES:
            # 中文 Windows 的 tasklist 输出是 GBK：不指定解码会让读取线程崩掉、
            # stdout 变空，客户端运行中会被误判为未运行而误写凭据库
            out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}"],
                                 capture_output=True, text=True,
                                 encoding="utf-8", errors="replace").stdout or ""
            if name.lower() in out.lower():
                return True
    except Exception:
        return True  # 探测失败按运行中处理，宁可保守
    return False


def _autoclaw2_device_id() -> str:
    try:
        with open(os.path.join(AUTOCCLAW2_DIR, "device", "device-id.json"), encoding="utf-8") as f:
            return str(json.load(f).get("deviceId") or "")
    except Exception:
        return ""


def _autoclaw2_remote_refresh(refresh_token: str, device_id: str, old_access: str) -> dict | None:
    """用 refreshToken 向官方端点换取新凭据（服务端强制轮换）。

    主路径 /userapi/v1/refresh 需要签名中间件（客户端自身在收到 400002 时
    也回落到 agent-refresh），这里直接走无需签名的 fallback 端点。
    返回 {access, refresh}（均含 Bearer 前缀）；响应缺 refresh_token 时沿用旧值。
    """
    body = {"refresh_token": refresh_token, "source_id": "autoclaw", "device_id": device_id}
    req = urllib.request.Request(
        AUTOCCLAW2_API_BASE + AUTOCCLAW2_REFRESH_PATH,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT,
                                    context=ssl.create_default_context()) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    access = str(data.get("access_token") or "").strip()
    if not access:
        return None
    refresh = str(data.get("refresh_token") or "").strip() or refresh_token

    def norm(jwt: str) -> str:
        return jwt if jwt[:7].lower() == "bearer " else "Bearer " + jwt

    return {"access": norm(access), "refresh": norm(refresh) if refresh else old_access}


def _write_autoclaw2_vault(vault: dict, access: str, refresh: str) -> bool:
    """把轮换后的凭据写回凭据库（保持 v10+AES-GCM 格式），供客户端下次启动读取。"""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        stored = {"schemaVersion": 1, "revision": vault["revision"] + 1,
                  "accessToken": access, "refreshToken": refresh,
                  "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000Z"}
        nonce = os.urandom(12)
        ct = AESGCM(vault["aes_key"]).encrypt(
            nonce, json.dumps(stored, ensure_ascii=False).encode("utf-8"), None)
        tmp = vault["enc_path"] + ".tmp"
        with open(tmp, "wb") as f:
            f.write(b"v10" + nonce + ct)
        os.replace(tmp, vault["enc_path"])
        return True
    except Exception:
        try:
            os.remove(vault["enc_path"] + ".tmp")
        except OSError:
            pass
        return False


def _upsert_autoclaw_auth(auth_dir: str, access: str, exp: float) -> bool:
    """把有效 accessToken 写入 auths/autoclaw.json（本地凭据已更新时跳过）。"""
    if exp <= time.time() + 300:
        return False
    current = _load_auth(auth_dir)
    if current and _jwt_exp(current["jwt"].replace("Bearer ", "").strip()) >= exp - 60:
        return False
    os.makedirs(auth_dir, exist_ok=True)
    with open(os.path.join(auth_dir, "autoclaw.json"), "w", encoding="utf-8") as f:
        json.dump({"accessToken": access, "region": "cn",
                   "userId": "from-autoclaw2", "userName": "autoclaw2-auto"},
                  f, ensure_ascii=False, indent=2)
    return True


def _refresh_from_autoclaw2(auth_dir: str) -> bool:
    """AutoClaw 2 凭据自动就绪：本地凭据库优先（jwt 有效直接采用；过期且
    客户端未运行时用 refreshToken 自主续期并写回凭据库），走不通时退回跨机
    会话镜像 auths/autoclaw_sync.json（同步盘双机轮流使用，见 _autoclaw_credsync）。
    """
    vault = _read_autoclaw2_vault()
    device_id = _autoclaw2_device_id()
    if vault:
        credsync.note(vault["access"], vault["refresh"], vault["exp"], device_id)
        if vault["exp"] > time.time() + 300:
            return _upsert_autoclaw_auth(auth_dir, vault["access"], vault["exp"])
        if not _autoclaw2_running():
            renewed = _autoclaw2_remote_refresh(vault["refresh"], device_id, vault["access"])
            if renewed:
                _write_autoclaw2_vault(vault, renewed["access"], renewed["refresh"])
                credsync.note(renewed["access"], renewed["refresh"],
                              _jwt_exp(renewed["access"].split(" ", 1)[-1]), device_id)
                return _upsert_autoclaw_auth(auth_dir, renewed["access"],
                                             _jwt_exp(renewed["access"].split(" ", 1)[-1]))
    return credsync.fallback_refresh(
        auth_dir,
        upsert=_upsert_autoclaw_auth,
        remote_refresh=_autoclaw2_remote_refresh,
        jwt_exp=_jwt_exp,
        vault=vault, vault_writer=_write_autoclaw2_vault,
        client_running=_autoclaw2_running)


def _http_json(method: str, url: str, jwt: str, body: dict | None) -> tuple:
    """发起 HTTP 请求，返回 (parsed_json_or_None, status_code, error_msg)。"""
    data = None
    headers = _build_headers(jwt)
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=ctx) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
    except urllib.error.URLError as e:
        return None, -1, str(e.reason)
    except Exception as e:
        return None, -1, str(e)
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
        return parsed, status, ""
    except Exception:
        return None, status, raw.decode("utf-8", "replace")[:300]


def _wallet_balance(acct: dict) -> int | None:
    """查询官方钱包总积分（total_balance），与 AutoClaw 客户端积分页同源。"""
    base = USERAPI_BASE.get(acct["region"], USERAPI_BASE["cn"])
    url = f"{base}{PATH_WALLETS}?biz_app_id=autoclaw"
    payload, status, err = _http_json("GET", url, acct["jwt"], None)
    if payload is None or not isinstance(payload, dict):
        return None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    tb = data.get("total_balance")
    return int(tb) if isinstance(tb, (int, float)) else None


def _wallet_source(cycle_key: str) -> str:
    """把钱包实例 cycle_key 映射为可读的积分来源说明。"""
    key = str(cycle_key or "")
    if "daily_signin" in key:
        return "每日签到"
    if "update" in key:
        return "版本更新奖励"
    if "download_pc_app" in key:
        return "下载客户端奖励"
    if "task_reward" in key:
        return "任务奖励"
    if "permanent" in key or "long_term" in key:
        return "长期奖励"
    return key or "Earned Points"


def _wallet_expiring(acct: dict) -> list:
    """查询官方钱包实例中未过期、且未来到期的积分（与客户端"即将过期"口径一致）。

    返回按到期日升序的列表：[{"date": "YYYY-MM-DD", "amount": int, "source": str}, ...]
    """
    base = USERAPI_BASE.get(acct["region"], USERAPI_BASE["cn"])
    url = f"{base}{PATH_WALLET_INSTANCES}?wallet_type=all&wallet_scope=all"
    payload, status, err = _http_json("GET", url, acct["jwt"], None)
    if payload is None or not isinstance(payload, dict):
        return []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    insts = data.get("wallet_instances")
    if not isinstance(insts, list):
        return []

    today = time.strftime("%Y-%m-%d")
    expiring = []
    for inst in insts:
        if not isinstance(inst, dict):
            continue
        bal = inst.get("balance")
        amount = int(bal) if isinstance(bal, (int, float)) else 0
        if amount <= 0:
            continue
        exp = str(inst.get("expires_at") or "")
        if len(exp) < 10:
            continue
        # 上游把「某日 24:00 失效」记为「次日 00:00:00」（如 2026-10-10T00:00:00
        # 实际是 10-09 24 点失效）：时间部分恰为 00:00:00 的实例归入前一日，
        # 否则到期日整体偏移一天，跨零点的行还会以「未来行」身份滞留日历
        if exp[10:19] == "T00:00:00":
            try:
                exp_date = time.strftime(
                    "%Y-%m-%d",
                    time.localtime(time.mktime(time.strptime(exp[:10], "%Y-%m-%d")) - 86400))
            except ValueError:
                exp_date = exp[:10]
        else:
            exp_date = exp[:10]
        if exp_date < today:
            continue
        expiring.append({
            "date": exp_date,
            "amount": amount,
            "source": _wallet_source(inst.get("cycle_key") or ""),
        })
    expiring.sort(key=lambda e: e["date"])
    return expiring


def main():
    auth_dir = DEFAULT_AUTH_DIR
    # 自动从桌面端凭据库刷新 jwt（AutoClaw 2 优先，失败退回旧版 openclaw.json）
    _refresh_from_openclaw(auth_dir)
    _refresh_from_autoclaw2(auth_dir)
    acct = _load_auth(auth_dir)
    if not acct:
        print(json.dumps({
            "result": "NO_AUTH",
            "report": "未找到 auths/autoclaw.json 凭证；请先登录 AutoClaw 桌面端",
            "today_credit": 0, "streak_days": 0, "total_credits": 0,
        }, ensure_ascii=False))
        return 2

    total = _wallet_balance(acct)
    if total is None:
        print(json.dumps({
            "result": "NETWORK",
            "report": "钱包接口不可达或响应异常，未能获取权威余额",
            "today_credit": 0, "streak_days": 0, "total_credits": 0,
        }, ensure_ascii=False))
        return 1

    expiring = _wallet_expiring(acct)
    report = f"AutoClaw 余额 {total} 分"
    if expiring:
        exp_total = sum(e["amount"] for e in expiring)
        report += f"，{len(expiring)} 笔到期（最早 {expiring[0]['date']}）"
    print(json.dumps({
        "result": "ALREADY",
        "report": report,
        "today_credit": 0,
        "streak_days": 0,
        "total_credits": total,
        "expiring": expiring,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
