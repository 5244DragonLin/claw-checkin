"""AutoClaw 每日签到 · ClawCheckin 接入脚本（单账号）

参考：https://github.com/Zhengyuuuui/autoclaw2api（check.py）

读取唯一凭证文件 auths/autoclaw.json（兼容旧名 autoclaw-cn.json），调用签到 API
POST {基址}/autoclaw-proxy/proxy/autoclaw-task-complete

token 自动提取（优先级）：
  1. AutoClaw 2 桌面端：解密 %APPDATA%/AutoClaw-official/accounts/*/account-credentials.enc
     （Chromium os_crypt：DPAPI 保护的 AES-256-GCM 密钥，仅 Windows，需 cryptography 包）；
     token 过期且客户端未运行时，自动用 refreshToken 调官方 /userapi/v1/agent-refresh
     续期并写回凭据库——完全无需打开客户端
  2. 旧版 / OpenClaw：~/.openclaw-autoclaw/openclaw.json 中的有效 JWT

权威积分与临期明细由隐藏积分源 scripts/autoclaw_credits.py 查询，签到后面板自动补跑。

输出契约（末行 JSON）：
  {
    "result": "CLAIMED|ALREADY|NETWORK|ERROR|NO_AUTH",
    "report": "可读汇报",
    "today_credit": 100,
    "streak_days": 0,
    "total_credits": 0
  }

凭证文件格式（autoclaw.json）：
  { "accessToken": "...", "region": "cn|global" }
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

APP_ID = "100003"
APP_SECRET = "38d2391985e2369a5fb8227d8e6cd5e5"
CLIENT_VER = "1.18.5"
TASK_ID = "daily_signin"
USERAPI_BASE = {
    "cn": "https://autoglm-acceleration-api.zhipuai.cn",
    "global": "https://autoglm-api.autoglm.ai",
}
PATH_TASK_COMPLETE = "/autoclaw-proxy/proxy/autoclaw-task-complete"
REQUEST_TIMEOUT = 20

# 自动探测 ClawCheckin 项目根下的 auths/ 目录，也支持环境变量覆盖
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_AUTH_DIR = os.environ.get("AUTOCLAW_AUTH_DIR") or os.path.join(PROJECT_ROOT, "auths")
STATE_FILE = os.path.join(PROJECT_ROOT, "data", "autoclaw_state.json")
# AutoClaw 2 桌面端（Electron）的凭据库与刷新端点（release/cn 环境）
AUTOCCLAW2_DIR = os.path.join(os.environ.get("APPDATA", ""), "AutoClaw-official")
AUTOCCLAW2_API_BASE = "https://autoglm-acceleration-api.zhipuai.cn"
AUTOCCLAW2_REFRESH_PATH = "/userapi/v1/agent-refresh"
AUTOCCLAW2_PROCESSES = ("AutoClaw2.exe", "AutoClaw.exe")
OPENCLAW_JSON = os.path.join(os.path.expanduser("~"), ".openclaw-autoclaw", "openclaw.json")
OPENCLAW_RUNTIME = os.path.join(os.path.expanduser("~"), ".openclaw-autoclaw", "openclaw.runtime.json")


def _load_state() -> dict:
    if os.path.isfile(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"total_credits": 0, "streak_days": 0, "last_date": ""}


def _save_state(state: dict):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def _sign(ts: str) -> str:
    return hashlib.md5(f"{APP_ID}&{ts}&{APP_SECRET}".encode()).hexdigest()


def _bearer(token: str) -> str:
    t = token.strip()
    return t if t.lower().startswith("bearer ") else "Bearer " + t


def _build_headers(token: str) -> dict:
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
        "Authorization": _bearer(token),
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
        token = (doc.get("accessToken") or "").strip()
        if not token:
            continue
        return {
            "path": path,
            "region": doc.get("region") or "cn",
            "token": token,
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
            token = auth.replace("Bearer ", "") if auth.startswith("Bearer ") else ""
            if not token or not token.startswith("ey") or token.count(".") != 2:
                continue
            try:
                payload_b64 = token.split(".")[1]
                payload_b64 += "=" * (-len(payload_b64) % 4)
                payload = json.loads(__import__("base64").urlsafe_b64decode(
                    payload_b64.replace("-", "+").replace("_", "/")))
                exp = payload.get("exp", 0)
                if exp > __import__("time").time() + 300:
                    if best is None or exp > best["exp"]:
                        best = {"token": token, "exp": exp}
            except Exception:
                continue
        if best:
            os.makedirs(auth_dir, exist_ok=True)
            dst = os.path.join(auth_dir, "autoclaw.json")
            doc = {
                "accessToken": "Bearer " + best["token"],
                "region": "cn",
                "userId": "from-openclaw",
                "userName": "openclaw-auto",
            }
            with open(dst, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
            return True
    return False


def _jwt_exp(token: str) -> float:
    """解析 JWT 的 exp（秒），失败返回 0。"""
    try:
        seg = token.split(".")[1]
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
            out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}"],
                                 capture_output=True, text=True).stdout or ""
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

    def norm(token: str) -> str:
        return token if token[:7].lower() == "bearer " else "Bearer " + token

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
    if current and _jwt_exp(current["token"].replace("Bearer ", "").strip()) >= exp - 60:
        return False
    os.makedirs(auth_dir, exist_ok=True)
    with open(os.path.join(auth_dir, "autoclaw.json"), "w", encoding="utf-8") as f:
        json.dump({"accessToken": access, "region": "cn",
                   "userId": "from-autoclaw2", "userName": "autoclaw2-auto"},
                  f, ensure_ascii=False, indent=2)
    return True


def _refresh_from_autoclaw2(auth_dir: str) -> bool:
    """AutoClaw 2 凭据自动就绪：token 有效直接采用；过期且客户端未运行时
    用 refreshToken 自主续期并写回凭据库——签到无需客户端参与。"""
    vault = _read_autoclaw2_vault()
    if not vault:
        return False
    if vault["exp"] > time.time() + 300:
        return _upsert_autoclaw_auth(auth_dir, vault["access"], vault["exp"])
    if _autoclaw2_running():
        return False  # 客户端运行中会自行轮换，不并发写凭据库
    renewed = _autoclaw2_remote_refresh(vault["refresh"], _autoclaw2_device_id(), vault["access"])
    if not renewed:
        return False
    _write_autoclaw2_vault(vault, renewed["access"], renewed["refresh"])
    return _upsert_autoclaw_auth(auth_dir, renewed["access"],
                                 _jwt_exp(renewed["access"].split(" ", 1)[-1]))


def _http_json(method: str, url: str, token: str, body: dict | None) -> tuple:
    """发起 HTTP 请求，返回 (parsed_json_or_None, status_code, error_msg)。"""
    data = None
    headers = _build_headers(token)
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


def _checkin_api(acct: dict) -> dict:
    """对单个 AutoClaw 账号执行签到，返回原始结果。"""
    base = USERAPI_BASE.get(acct["region"], USERAPI_BASE["cn"])
    url = base + PATH_TASK_COMPLETE
    payload, status, err = _http_json("POST", url, acct["token"], {"task_id": TASK_ID})

    result = {
        "ok": False,
        "already_completed": False,
        "reward_points": 0,
        "balance": None,
        "auth_failed": False,
        "report": "",
    }

    if payload is None:
        if status == -1:
            result["report"] = f"网络不可达：{err}"
        else:
            result["report"] = f"HTTP {status}：{err}"
        return result

    if not isinstance(payload, dict):
        result["report"] = f"响应非 JSON：{str(payload)[:100]}"
        return result

    code = payload.get("code")
    msg = str(payload.get("msg") or "")
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}

    # 401 = token 失效 / 未认证：标记鉴权失败，主流程归为 NO_AUTH
    if status == 401:
        result["auth_failed"] = True

    if status >= 400 and code is None:
        result["report"] = f"HTTP {status}：{msg or str(payload)[:100]}"
        return result

    ok = code == 0 or (code is None and bool(data))
    if not ok:
        result["report"] = f"签到失败：{msg or f'code={code}'}"
        return result

    result["ok"] = True
    result["already_completed"] = bool(data.get("already_completed"))
    rp = data.get("reward_points")
    result["reward_points"] = int(rp) if isinstance(rp, (int, float)) else 0
    bal = data.get("balance")
    result["balance"] = int(bal) if isinstance(bal, (int, float)) else None

    if result["already_completed"]:
        result["report"] = f"今日已签到（+{result['reward_points']} 积分）"
    else:
        result["report"] = f"签到成功，+{result['reward_points']} 积分"

    if result["balance"] is not None:
        result["report"] += f"，余额 {result['balance']} 积分"

    return result


def main():
    refresh_only = "--refresh" in sys.argv

    state = _load_state()
    today = time.strftime("%Y-%m-%d")

    auth_dir = DEFAULT_AUTH_DIR

    # 自动从桌面端凭据库刷新 token（AutoClaw 2 优先，失败退回旧版 openclaw.json）
    _refresh_from_openclaw(auth_dir)
    _refresh_from_autoclaw2(auth_dir)

    acct = _load_auth(auth_dir)

    if refresh_only:
        if acct:
            print(json.dumps({"result": "ALREADY", "report": "Token 已刷新，凭据就绪",
                              "today_credit": 0, "streak_days": 0, "total_credits": 0}, ensure_ascii=False))
        else:
            print(json.dumps({"result": "NO_AUTH", "report": "openclaw.json 中未找到有效 JWT，请先登录 AutoClaw 桌面端",
                              "today_credit": 0, "streak_days": 0, "total_credits": 0}, ensure_ascii=False))
        return 0 if acct else 1

    if not acct:
        report_parts = []
        if os.path.isdir(auth_dir):
            report_parts.append(f"auths/ 目录 {auth_dir} 下未找到 autoclaw.json 凭证文件")
        else:
            report_parts.append(f"auths/ 目录 {auth_dir} 不存在")
        report_parts.append("请从 AutoClaw 登录获取 accessToken，放入 auths/autoclaw.json 中")
        report_parts.append("也可设置 AUTOCLAW_AUTH_DIR 环境变量指定凭证目录")
        print(json.dumps({
            "result": "NO_AUTH",
            "report": "；".join(report_parts),
            "today_credit": 0,
            "streak_days": 0,
            "total_credits": 0,
        }, ensure_ascii=False))
        return 2

    r = _checkin_api(acct)
    today_points = r["reward_points"]
    # state 口径：本地累计仅在拿到今日积分时 +1；
    # streak 每自然日只 +1（last_date 守卫，同日重复执行不双计）。
    # 权威余额与临期明细由 autoclaw_credits 查询，不再在此请求钱包接口。
    if today_points > 0:
        state["total_credits"] = state.get("total_credits", 0) + today_points
    if today_points > 0 and state.get("last_date") != today:
        state["streak_days"] = state.get("streak_days", 0) + 1
        state["last_date"] = today
    _save_state(state)
    # 展示口径：签到接口余额 > 本地累计
    total = r["balance"] if r["balance"] is not None else state.get("total_credits", 0)

    if not r["ok"]:
        # 鉴权失败（401 / token 失效）归为 NO_AUTH，面板显示「待验证」而非「签到失败」
        result_status = "NO_AUTH" if r["auth_failed"] else "ERROR"
        print(json.dumps({
            "result": result_status,
            "report": r["report"],
            "today_credit": today_points,
            "streak_days": state["streak_days"],
            "total_credits": total,
        }, ensure_ascii=False))
        return 1

    # 签到成功
    print(json.dumps({
        "result": "ALREADY" if r["already_completed"] else "CLAIMED",
        "report": r["report"],
        "today_credit": 0 if r["already_completed"] else today_points,
        "streak_days": state["streak_days"],
        "total_credits": total,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())