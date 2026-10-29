"""AutoClaw 每日签到 · ClawCheckin 接入脚本（单账号）

参考：https://github.com/Zhengyuuuui/autoclaw2api（check.py）

读取唯一凭证文件 auths/autoclaw.json（兼容旧名 autoclaw-cn.json），调用签到 API
POST {基址}/autoclaw-proxy/proxy/autoclaw-task-complete

签到只给每日签到那一份（当前 200 积分）。客户端启动时还能拿到一笔运营活动奖励
（当前「用户登录奖励 500 积分，7 天有效」），它不在任务列表里、也没有领取接口：

  1. POST /userapi/v1/user-info —— 官方客户端每次启动都会打这个触点，服务端据此
     判定用户活跃并按活动规则发奖（必须 POST，GET 同路径返回 code=404）；
  2. GET /agent-assetmgr/api/v1/ledgers_std（只看收入方向）—— 活动奖励没有领取
     接口可调，只能读流水复核当天到账多少。

因此本脚本的「今日收益」= 签到奖励 + 当日活动奖励，两者都只在首次确认到账的那
一跑计入 today_credit（state 里 activity_date 守住，同日重复执行不双计）。

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
    "total_credits": 0,
    "signed_today": true   // 可选：平台侧确认今天已签（--status 只读查询带上）
  }

`--status`：只读查询今天签没签（GET autoclaw-task-list 看 daily_signin 是否
completed），**不发签到请求、不领积分**。面板打开/切回时用它承认「你在 AutoClaw
客户端已经签过」的到，免得卡片一直显示未签到。

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _autoclaw_credsync as credsync  # noqa: E402  同目录共享模块：跨机会话漫游镜像

APP_ID = "100003"
APP_SECRET = "38d2391985e2369a5fb8227d8e6cd5e5"
CLIENT_VER = "1.18.5"
TASK_ID = "daily_signin"
USERAPI_BASE = {
    "cn": "https://autoglm-acceleration-api.zhipuai.cn",
    "global": "https://autoglm-api.autoglm.ai",
}
PATH_TASK_COMPLETE = "/autoclaw-proxy/proxy/autoclaw-task-complete"
# 只读任务列表：能查到 daily_signin 的完成状态，用于 `--status`
PATH_TASK_LIST = "/autoclaw-proxy/proxy/autoclaw-task-list"
# 「登录」触点：官方客户端每次启动都会打一次，服务端据此判定用户活跃并发放
# 运营活动奖励（当前是「用户登录奖励积分 500，7 天有效」）。必须 POST（GET 同
# 路径返回 code=404），body 传空对象即可
PATH_USER_INFO = "/userapi/v1/user-info"
# 积分流水（只读）：活动奖励是「事件发奖」，没有领取接口，只能读流水复核到账
PATH_LEDGERS = "/agent-assetmgr/api/v1/ledgers_std"
LEDGERS_QUERY = ("?asset_type=point&wallet_type=all&cursor=&page_size=50"
                 "&flow_direction=income")
# 活动奖励在流水里的标识：metadata.activity_id 以它开头，或 metadata.event_type 等于它
ACTIVITY_PREFIX = "autoclaw_user_active"
REQUEST_TIMEOUT = 20
# user-info 触发后奖励可能晚一个心跳到账：读流水拿不到时补读一次，间隔 1.2 秒
ACTIVITY_RETRY_DELAY = 1.2

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
    """读取积分状态（自动合并同步盘冲突副本，见 _autoclaw_credsync）。"""
    return credsync.load_state()


def _save_state(state: dict):
    credsync.save_state(state)


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
    """AutoClaw 2 凭据自动就绪：本地凭据库优先（token 有效直接采用；过期时用
    refreshToken 自主续期——客户端运行中也允许续，但只写项目内 auths/，不碰
    桌面端凭据库），走不通时退回跨机会话镜像 auths/autoclaw_sync.json（同步盘
    双机轮流使用，见 _autoclaw_credsync）。

    「客户端运行中不碰凭据库」的初衷是避免与客户端抢内存态凭据；但实测存在
    死锁：客户端虽在运行却不会自行轮换，脚本又因它在跑而放弃续期，token 干等
    过期导致签到 401。故改为：续期照做，仅「写回凭据库」这一步在客户端运行时
    跳过；项目 auths/ 始终更新，签到不再被卡。
    """
    vault = _read_autoclaw2_vault()
    device_id = _autoclaw2_device_id()
    if vault:
        credsync.note(vault["access"], vault["refresh"], vault["exp"], device_id)
        if vault["exp"] > time.time() + 300:
            return _upsert_autoclaw_auth(auth_dir, vault["access"], vault["exp"])
        renewed = _autoclaw2_remote_refresh(vault["refresh"], device_id, vault["access"])
        if renewed:
            # 客户端运行中不写凭据库（防并发抢凭据），但仍写项目 auths/
            if not _autoclaw2_running():
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


def _out(result: str, report: str, today: int = 0, streak: int = 0,
         total: int = 0, signed_today: bool | None = None) -> dict:
    out = {
        "result": result,
        "report": report,
        "today_credit": int(today),
        "streak_days": int(streak),
        "total_credits": int(total),
    }
    if signed_today is not None:
        out["signed_today"] = bool(signed_today)
    return out


def _task_completed(row: dict) -> bool:
    """判定任务行是否「今天已完成」。

    上游实测（2026-10）用 `status: "completed" | "incomplete"` 表达状态，
    **不是** `completed: true` 布尔（该字段已废弃、实测恒为 None）。早期照抄
    参考实现只认布尔，导致「平台明明显示已签到、面板却报未签到」的静默误报。
    两种形态都认，并兼容大写与常见同义值。
    """
    status = str(row.get("status") or "").strip().lower()
    if status in ("completed", "complete", "done", "claimed", "finished"):
        return True
    if status in ("incomplete", "pending", "todo", "uncompleted"):
        return False
    # status 缺失/不可识别时退回旧布尔字段（老版本接口可能只有它）
    legacy = row.get("completed")
    if isinstance(legacy, bool):
        return legacy
    return False


def _status_probe(acct: dict | None, state: dict) -> dict:
    """只读查询今日是否已签：拉任务列表看 daily_signin 的状态。

    绝不调用 task-complete（那个会真的领积分）。只有列表明确表示「已完成」才置
    signed_today——列表拿不到、结构不认识都按「没有证据」处理，宁可让卡片显示
    未签到，也不谎报已签。
    """
    if not acct:
        return _out("NO_AUTH", "未找到 AutoClaw 凭据，请先登录 AutoClaw 桌面端")
    base = USERAPI_BASE.get(acct["region"], USERAPI_BASE["cn"])
    payload, status, err = _http_json("GET", base + PATH_TASK_LIST, acct["token"], None)
    if payload is None:
        if status == -1:
            return _out("NETWORK", f"任务列表查询失败：{err}")
        if status in (401, 403):
            return _out("NO_AUTH", f"任务列表查询被拒绝（HTTP {status}），请重新登录 AutoClaw")
        return _out("NETWORK" if status in (429, 502, 503, 504) else "ERROR",
                    f"任务列表查询失败（HTTP {status}）：{err}")
    if not isinstance(payload, dict) or payload.get("code") not in (0, None):
        return _out("ERROR", f"任务列表返回异常：{str(payload)[:120]}")
    tasks = payload.get("data")
    if not isinstance(tasks, list):
        return _out("ERROR", "任务列表结构不可识别")

    row = next((t for t in tasks
                if isinstance(t, dict) and t.get("task_id") == TASK_ID), None)
    if row is None:
        return _out("ALREADY", "任务列表里没有每日签到任务，可能今日未开放")
    done = _task_completed(row)
    # 平台给了「已签到 N 天」这样的可读状态，优先照搬，比自造文案可信
    desc = str(row.get("status_description") or "").strip()
    if done:
        report = "平台确认今日已签到" + (f"（{desc}）" if desc else "")
    else:
        report = "平台显示今天尚未签到" + (f"（{desc}）" if desc else "")
    return _out("ALREADY", report,
                streak=state.get("streak_days", 0),
                total=state.get("total_credits", 0),
                signed_today=done)


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


def _trigger_login(acct: dict) -> str:
    """冒充一次「用户登录」，让服务端判定用户活跃并发放当天的运营活动奖励。

    这是官方客户端启动时打的同一个触点（POST /userapi/v1/user-info，空 body）。
    与签到无关、也不是领取接口：奖励由服务端按活动规则发，这里只负责把「用户
    活跃」这个事件打出去。返回空串表示成功，否则返回一句可读的失败原因——
    调用方只把它写进汇报，不影响签到本身的结论。

    幂等：服务端按 grant_date 每天只发一次，重复调用不会重复加分；出错也不抛。
    """
    base = USERAPI_BASE.get(acct["region"], USERAPI_BASE["cn"])
    payload, status, err = _http_json("POST", base + PATH_USER_INFO, acct["token"], {})
    if payload is None:
        return err if status == -1 else f"HTTP {status}：{err}"
    if not isinstance(payload, dict):
        return f"响应非 JSON：{str(payload)[:80]}"
    if payload.get("code") not in (0, None):
        return str(payload.get("msg") or f"code={payload.get('code')}")
    return ""


def _activity_today(acct: dict, today: str) -> tuple:
    """读当日入账流水，汇总运营活动发的积分。返回 (合计, 明细文案列表)。

    只读，不产生任何副作用。活动奖励在流水里长这样：

        amount 500, source campaign, wallet_scope campaign
        description "AutoClaw 用户登录奖励积分，7天有效"
        metadata.activity_id  = autoclaw_user_active_reward_202609
        metadata.event_type   = autoclaw_user_active
        metadata.grant_date   = "2026-10-23"

    筛选口径：金额为正 + 属于 autoclaw_user_active 活动 + grant_date 是今天。
    没有 grant_date 的老记录退回按 occurred_at 的日期判。每日签到（description
    为「每日签到」）不带这些字段，不会被算进来。
    """
    base = USERAPI_BASE.get(acct["region"], USERAPI_BASE["cn"])
    payload, _status, _err = _http_json("GET", base + PATH_LEDGERS + LEDGERS_QUERY,
                                        acct["token"], None)
    if not isinstance(payload, dict) or payload.get("code") not in (0, None):
        return 0, []
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    entries = data.get("entries")
    if not isinstance(entries, list):
        return 0, []

    total = 0
    details = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            amount = int(entry.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        if amount <= 0:
            continue
        meta = entry.get("metadata") if isinstance(entry.get("metadata"), dict) else {}
        activity_id = str(meta.get("activity_id") or "")
        event_type = str(meta.get("event_type") or "")
        if not (activity_id.startswith(ACTIVITY_PREFIX) or event_type == ACTIVITY_PREFIX):
            continue
        # grant_date 是发奖日（服务端按它去重），比 occurred_at 更能代表「哪一天的份额」
        grant_date = str(meta.get("grant_date") or "")[:10]
        day = grant_date or str(entry.get("occurred_at") or "")[:10]
        if day != today:
            continue
        total += amount
        desc = str(entry.get("description") or "").strip()
        details.append(f"{desc or '活动奖励'} +{amount}")

    return total, details


def main():
    refresh_only = "--refresh" in sys.argv
    status_only = "--status" in sys.argv

    state = _load_state()
    today = time.strftime("%Y-%m-%d")

    auth_dir = DEFAULT_AUTH_DIR

    # 自动从桌面端凭据库刷新 token（AutoClaw 2 优先，失败退回旧版 openclaw.json）
    _refresh_from_openclaw(auth_dir)
    _refresh_from_autoclaw2(auth_dir)

    acct = _load_auth(auth_dir)

    if status_only:
        out = _status_probe(acct, state)
        print(json.dumps(out, ensure_ascii=False))
        return 0 if out["result"] in ("CLAIMED", "ALREADY") else 1

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
        state["last_daily"] = today_points
    if today_points > 0 and state.get("last_date") != today:
        state["streak_days"] = state.get("streak_days", 0) + 1
        state["last_date"] = today

    # 活动福利：签到之外还有一笔运营活动奖励（当前是「用户登录奖励 500 积分，
    # 7 天有效」）。它不在任务列表里、也没有领取接口——只能先打一次「登录」触点
    # 把用户活跃事件发出去，再由服务端按活动规则发奖，我们读流水复核到账。
    # 同一天只入账一次（activity_date 守卫，跨机共用同一份 state 也不双计）；
    # 这一步失败只写进汇报，不改变签到本身的结论。
    activity_sum = 0
    activity_note = ""
    if r["ok"] and state.get("activity_date") != today:
        trigger_err = _trigger_login(acct)
        activity_sum, activity_details = _activity_today(acct, today)
        if activity_sum == 0 and not trigger_err:
            time.sleep(ACTIVITY_RETRY_DELAY)
            activity_sum, activity_details = _activity_today(acct, today)
        if activity_sum > 0:
            state["activity_date"] = today
            state["activity_credit"] = activity_sum
            state["total_credits"] = state.get("total_credits", 0) + activity_sum
            joined = "、".join(activity_details) if activity_details else f"+{activity_sum}"
            activity_note = f"；活动福利 {joined} 已到账"
        elif trigger_err:
            activity_note = f"；活动福利未触发（{trigger_err}）"
    elif r["ok"] and state.get("activity_credit"):
        # 今天早先那一跑已经记过账，这里只做陈述、不再计入 today_credit
        activity_note = f"；今日活动福利 +{state['activity_credit']} 已到账"

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

    # 已签到场景：API 可能返回 reward_points=0，改用本地记录的实际值
    if r["already_completed"] and r["reward_points"] == 0 and state.get("last_date") == today:
        r["reward_points"] = state.get("last_daily", 0)
        r["report"] = f"今日已签到（+{r['reward_points']} 积分）"
        if r["balance"] is not None:
            r["report"] += f"，余额 {r['balance']} 积分"

    # 签到成功。today_credit 报「这一跑实际新增到账」（签到 + 活动福利），
    # 已签到场景下签到部分归 0，与既有口径一致
    print(json.dumps({
        "result": "ALREADY" if r["already_completed"] else "CLAIMED",
        "report": r["report"] + activity_note,
        "today_credit": (0 if r["already_completed"] else today_points) + activity_sum,
        "streak_days": state["streak_days"],
        "total_credits": total,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())