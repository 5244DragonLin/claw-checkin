"""库库AI（百度 GenFlow Pro）凭据 / 请求层 / 签到原语共享 · ClawCheckin 接入脚本

协议参考：https://github.com/xinxinshuhao-create/kuku2api
（网页端实测抓包，2026-09；本仓接入时按真实账号复核过）

── 库库的「签到」长什么样 ────────────────────────────────────
库库没有传统的「一天一次签到接口」。每日积分以**活动任务**形式下发，
走 freepoint 一族端点（全部挂在 kuku.baidu.com，走网页 Cookie 鉴权）：

  GET  {TARGET}/api/genflowpro/common/userreport?{QUERY}
   → {errno:0, data:{bdstoken, uinfo, uk}}     ← 会话三件套（短时效）
  GET  {TARGET}/api/genflowpro/freepoint/homenew?{QUERY}
   → activities[].tabs[].tasks[]               ← 任务面板 + 领取状态
  POST {TARGET}/api/genflowpro/freepoint/taskComplete?{QUERY}
   body form: task_type=LOGIN                  ← 领「每日登录」（=签到）
             task_type=CHAT                    ← 领「完成一次对话」

判「真领到」看 taskComplete 回的 complete_status=SUCCESS 且 reward_point>0；
已领过的任务重复领取幂等（BADGE_ALREADY_RECEIVED / reward_point=0），
所以可以放心在面板手动刷新与定时任务里重复触发，不会重复加分。

── 凭据与续期（关键设计）────────────────────────────────────
鉴权用**网页版登录态 Cookie**（百度通行证族：BDUSS / STOKEN 等）。分两层：

  · Cookie（BDUSS 等）＝根凭据，长期有效（年），无 refresh 机制 —— 它自己
    就是「续期凭据」；失效只能重新登录导一次。
  · 三件套（bdstoken / uinfo / uk）＝会话凭据，短时效（分钟级~小时级），
    由 userreport 用 Cookie 随时换新 —— **这就是本平台的「token 自动续期」**：
    每次签到/查询前都调一次 userreport，三件套永远新鲜；userreport 失败
    （Cookie 过期/被顶下线）才报 NO_AUTH。

Cookie 来源（按优先级）：
  1. auths/kuku.json            脚本工作副本（--cookie 写入 / login 助手导出）
  2. auths/kuku_sync.json       跨机漫游镜像（同步盘双机轮流使用时随同步盘走）
  3. 库库桌面客户端             客户端退出后直接解 Chromium Cookie 库（本机已
                                 登录即免配置）；运行中被独占锁，如实提示
  4. 环境变量 KUKU_BDUSS / KUKU_STOKEN 等

写入助手 `--login`：起一个本地引导页，教用户从浏览器 DevTools 复制 Cookie；
`--cookie "BDUSS=...; STOKEN=..."` 直接落盘。Cookie 永不写日志。

── 为什么不用 requests ──────────────────────────────────────
与项目其余脚本保持一致用 urllib（标准库、零依赖）。库库网页端对 TLS 指纹
不做强校验（kuku2api 用 curl_cffi 伪装是为了过 Cloudflare 类前置；实测
2026-11 标准库 UA+Cookie 组合可通），带足浏览器画像头即可。
"""

from __future__ import annotations

import base64
import json
import os
import random
import re
import shutil
import sqlite3
import ssl
import string
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import _syncio

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK_PATH = os.path.join(PROJECT_ROOT, "auths", "kuku.json")
MIRROR_PATH = os.path.join(PROJECT_ROOT, "auths", "kuku_sync.json")

# 库库桌面客户端（Electron）的 Chromium 数据目录：登录态 Cookie 在这里。
# 客户端运行时把 Cookies 库独占锁住读不了；退出后是普通 sqlite 且 Cookie
# 为明文（未启用 App-Bound Encryption），直接可读。
CLIENT_DATA_DIR = os.path.join(
    os.environ.get("APPDATA", ""), "baidugenflowpro")
CLIENT_COOKIE_DBS = (
    os.path.join(CLIENT_DATA_DIR, "Network", "Cookies"),
    os.path.join(CLIENT_DATA_DIR, "Partitions", "genflow-auth", "Network", "Cookies"),
)

TARGET = "https://kuku.baidu.com"
APP_ID = 123971023
CHANNEL_NAME = "kuku_web_genflowpro_v1"
# 网页端所有 API 都挂这串公共 query（抓包固定形状）；三件套由调用方追加
QUERY_BASE = f"clienttype=400&app_id={APP_ID}&web=1&channel=chunlei&version=1.4.4"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")

PATH_USERREPORT = "/api/genflowpro/common/userreport"
PATH_FREEPOINT = "/api/genflowpro/freepoint"

# 每日积分任务（task_type → 中文名）。LOGIN 即「每日登录」=每日签到；
# CHAT 是「完成一次对话」的奖励，签到顺手一起领（幂等，不会重复拿）。
TASK_LOGIN = "LOGIN"
TASK_CHAT = "CHAT"
TASK_NAMES = {
    "LOGIN": "每日登录",
    "CHAT": "完成一次对话",
    "INVITE_VISIT": "邀请新用户登录网页版",
    "INVITE_DOWNLOAD": "邀请新用户登录电脑版",
}

# 「领取成功」的两种上游口径
STATUS_SUCCESS = "SUCCESS"
STATUS_ALREADY = "BADGE_ALREADY_RECEIVED"

REQUEST_TIMEOUT = 20
REFRESH_TIMEOUT = 30


# ── 凭据解析 ────────────────────────────────────────────────

def _read_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _from_doc(doc: dict) -> dict | None:
    """从一份记录里取 Cookie 对；没有 BDUSS 视为无效（签到必须）。"""
    # 三种存法都兼容：
    # {"cookie": "BDUSS=...; STOKEN=..."}      ← 客户端全量导出（推荐，19 条画像头）
    # {"cookies": [{"name","value"},...]}      ← login 助手浏览器导出原样
    # {"bduss": "...", "stoken": "..."}        ← --cookie 手工键值
    cookie = str(doc.get("cookie") or "").strip()
    if cookie and "BDUSS=" in cookie.upper():
        bduss = ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name.strip().upper() == "BDUSS":
                bduss = value.strip()
                break
        if bduss:
            return {"source": "work", "cookie": cookie, "bduss": bduss,
                    "saved_at": str(doc.get("savedAt") or doc.get("timestamp") or "")}
    pairs: list[tuple[str, str]] = []
    rows = doc.get("cookies")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("name") and row.get("value"):
                pairs.append((str(row["name"]), str(row["value"])))
    if not pairs:
        for key in ("BDUSS", "bduss", "STOKEN", "stoken", "PTOKEN", "ptoken",
                    "STOKEN_LOGIN", "stoken_login"):
            value = str(doc.get(key) or "").strip()
            if value:
                pairs.append((key.upper(), value))
    if not any(name.upper() == "BDUSS" for name, _value in pairs):
        return None
    cookie = "; ".join(f"{name}={value}" for name, value in pairs)
    return {
        "source": "work",
        "cookie": cookie,
        "bduss": next((v for n, v in pairs if n.upper() == "BDUSS"), ""),
        "saved_at": str(doc.get("savedAt") or doc.get("timestamp") or ""),
    }


def credential_sources() -> list[tuple[str, str]]:
    """(来源标签, 路径) 列表，按优先级排序。"""
    return [("auths/kuku.json", WORK_PATH), ("跨机漫游镜像", MIRROR_PATH)]


def _read_client_cookie_db(path: str) -> dict[str, str]:
    """读一个 Chromium Cookies 库，返回 {name: value}（明文列，只收非空值）。

    客户端运行时文件被独占锁（errno 32/sqlite "file is not a database" 也可能
    出现在半写状态），调用方按异常/空结果处理，不区分失败细节。
    """
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        shutil.copyfile(path, tmp)
        conn = sqlite3.connect(tmp)
        try:
            rows = conn.execute(
                "SELECT name, value FROM cookies WHERE value != ''").fetchall()
        finally:
            conn.close()
        return {str(name): str(value) for name, value in rows}
    except Exception:
        return {}
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


# 客户端 Cookies 库里与 kuku 会话相关的 Cookie 名（按抓包重要性排序；
# BDUSS 是登录态本体，其余是通行证辅助与风控画像，带上更稳）
CLIENT_COOKIE_NAMES = (
    "BDUSS", "BDUSS_BFESS", "STOKEN", "STOKEN_BFESS", "PTOKEN", "PTOKEN_BFESS",
    "BAIDUID", "BAIDUID_BFESS", "SAVEUSERID", "SAVEUSERID_BFESS",
    "USERNAMETYPE", "USERNAMETYPE_BFESS", "HISTORY", "HISTORY_BFESS",
    "UBI", "UBI_BFESS", "HOSUPPORT", "HOSUPPORT_BFESS", "gfprotpl",
)

CLIENT_LOCKED_ERRNO = 32       # Win32 共享冲突（客户端正在运行）
CLIENT_RUNNING_HINT = "库库客户端正在运行"


def read_cookies_from_client() -> tuple[dict | None, str]:
    """从库库桌面客户端的 Chromium 数据目录读登录态 Cookie。

    返回 (credential dict, 失败说明)。成功时 credential 与 resolve_credentials
    的产物同构；客户端目录不存在 / 都被锁（客户端在运行）/ 没有 BDUSS 时
    返回 (None, 原因说明)。读到即返回，不写任何文件。
    """
    if not os.path.isdir(CLIENT_DATA_DIR):
        return None, "本机没有找到库库客户端数据目录（未安装或未登录过）"

    merged: dict[str, str] = {}
    locked = False
    for path in CLIENT_COOKIE_DBS:
        if not os.path.isfile(path):
            continue
        cookies = _read_client_cookie_db(path)
        if not cookies:
            # Windows 独占锁：Win32 报 errno 32（共享冲突）、CRT 层 copyfile
            # 报 errno 13（PermissionError）—— 两者都指向「客户端正在运行」
            try:
                open(path, "rb").close()
            except OSError as exc:
                if exc.errno in (13, CLIENT_LOCKED_ERRNO):
                    locked = True
            continue
        for name in CLIENT_COOKIE_NAMES:
            if name in cookies and name not in merged:
                merged[name] = cookies[name]

    bduss = merged.get("BDUSS") or ""
    if not bduss:
        if locked:
            return None, (CLIENT_RUNNING_HINT + "，Cookie 库被独占无法读取；"
                          "退出客户端后重试，或改用浏览器导出 Cookie")
        return None, "客户端 Cookie 库里没有 BDUSS（可能未登录）"

    cookie = "; ".join(f"{name}={merged[name]}" for name in CLIENT_COOKIE_NAMES
                       if name in merged)
    return {"source": "库库客户端", "cookie": cookie, "bduss": bduss,
            "saved_at": ""}, ""


def resolve_credentials() -> dict | None:
    """按优先级解析凭据，返回 credential dict 或 None。

    1. auths/kuku.json（工作副本）/ auths/kuku_sync.json（漫游镜像）
    2. 库库桌面客户端的 Cookie 库（客户端退出时可直接读出登录态）
    3. 环境变量 KUKU_BDUSS / KUKU_STOKEN 等
    """
    for _label, path in credential_sources():
        creds = _from_doc(_read_json(path))
        if creds:
            return creds

    client_creds, _reason = read_cookies_from_client()
    if client_creds:
        return client_creds

    bduss = (os.environ.get("KUKU_BDUSS") or "").strip()
    if bduss:
        extra = []
        for name, env in (("STOKEN", "KUKU_STOKEN"), ("PTOKEN", "KUKU_PTOKEN"),
                          ("STOKEN_LOGIN", "KUKU_STOKEN_LOGIN")):
            value = (os.environ.get(env) or "").strip()
            if value:
                extra.append(f"{name}={value}")
        cookie = "; ".join(["BDUSS=" + bduss] + extra)
        return {"source": "环境变量", "cookie": cookie, "bduss": bduss, "saved_at": ""}
    return None


def save_credentials(creds: dict) -> bool:
    """Cookie 写进工作副本与跨机漫游镜像（同步盘双机轮流使用场景）。"""
    bduss = (creds.get("bduss") or "").strip()
    if not bduss:
        return False
    payload = {
        "bduss": bduss,
        "stoken": (creds.get("stoken") or "").strip(),
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


def cookie_from_string(text: str) -> dict | None:
    """把用户粘贴的 Cookie 串（BDUSS=xxx; STOKEN=yyy 格式）解析成凭据。"""
    pairs: list[tuple[str, str]] = []
    for part in (text or "").replace("\n", "; ").split(";"):
        name, _, value = part.strip().partition("=")
        name, value = name.strip(), value.strip()
        if name and value:
            pairs.append((name, value))
    if not any(name.upper() == "BDUSS" for name, _value in pairs):
        return None
    return {
        "bduss": next(v for n, v in pairs if n.upper() == "BDUSS"),
        "stoken": next((v for n, v in pairs if n.upper() == "STOKEN"), ""),
        "cookie": "; ".join(f"{n}={v}" for n, v in pairs),
        "saved_at": "",
    }


# ── 请求层 ──────────────────────────────────────────────────

def api_json(
    method: str,
    url: str,
    headers: dict,
    body=None,
    timeout: float = REQUEST_TIMEOUT,
) -> tuple[dict | None, int, str]:
    """发一次请求，返回 (解析后的 JSON, HTTP 状态码, 错误说明)。

    body 为 dict 时按 JSON 发送；为 str/bytes 时按原文发送（form 用）。
    """
    data = None
    if isinstance(body, dict):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    elif isinstance(body, (str, bytes)):
        data = body.encode("utf-8") if isinstance(body, str) else body
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
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
        return parsed, status, ""
    return None, status, "响应不是 JSON 对象"


def web_headers(creds: dict, content_type: str = "application/json") -> dict:
    """网页端画像头。签到与聊天走同一个组合，只有 Content-Type 随端点变。"""
    return {
        "Cookie": str(creds.get("cookie") or ""),
        "Content-Type": content_type,
        "Referer": f"{TARGET}/genflowpro",
        "Origin": TARGET,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "User-Agent": UA,
    }


def query_with(creds: dict, trio: dict | None) -> str:
    """公共 query；有三件套时追加（抓包里三件套紧跟在公共参数后面）。"""
    if not trio:
        return QUERY_BASE
    extra = []
    for key in ("bdstoken", "uinfo", "uk"):
        value = trio.get(key)
        if value not in (None, ""):
            extra.append(f"{key}={urllib.parse.quote(str(value), safe='')}")
    return f"{QUERY_BASE}&{'&'.join(extra)}" if extra else QUERY_BASE


# ── 会话三件套（token 自动续期的核心）───────────────────────

def fetch_trio(creds: dict) -> tuple[dict | None, int, str]:
    """GET userreport → {bdstoken, uinfo, uk}。

    这就是「token 续期」：三件套短时效，Cookie 长期有效 —— 任何一次签到/查询
    前都现换一把，不存在「token 过期要人工干预」的窗口。errno!=0 或 HTTP 401
    说明 Cookie 本身失效（被顶下线/过期），返回 None 让调用方报 NO_AUTH。
    """
    url = f"{TARGET}{PATH_USERREPORT}?{QUERY_BASE}"
    payload, status, err = api_json("GET", url, web_headers(creds), timeout=REFRESH_TIMEOUT)
    if payload is None:
        return None, status, err
    if status >= 400:
        head = str(payload.get("errmsg") or payload.get("message") or "").strip()
        return None, status, head or f"HTTP {status}"
    if payload.get("errno") != 0:
        return None, status, f"errno={payload.get('errno')} {payload.get('errmsg') or ''}".strip()
    data = payload.get("data")
    if not isinstance(data, dict) or not data.get("bdstoken"):
        return None, status, "userreport 响应缺少 bdstoken（上游可能已调整接口）"
    return {
        "bdstoken": str(data.get("bdstoken") or ""),
        "uinfo": str(data.get("uinfo") or ""),
        "uk": data.get("uk") or 0,
    }, status, ""


def is_auth_reject(status: int, err: str, payload: dict | None = None) -> bool:
    """这次失败是不是「Cookie 失效/未登录」（区别于网络抖动与接口变动）。

    实测口径（2026-11-09，假 Cookie 真机）：userreport 对失效会话回
    HTTP 200 + errno=-6（百度通行证族对未登录统一给负 errno）；HTTP 鉴权族
    也可能直接 4xx。按保守口径：HTTP 401/403、负 errno、「未登录」字样
    都算登录态问题；正 errno（活动类业务错）与其余不算。
    """
    if status in (401, 403):
        return True
    if payload is not None and isinstance(payload.get("errno"), int):
        return payload["errno"] < 0
    text = (err or "").lower()
    if "errno=-" in text:       # fetch_trio 把负 errno 折进错误串带给调用方
        return True
    return "未登录" in text or "login" in text or "passport" in text


# ── 签到 / 任务面板 ────────────────────────────────────────

def point_panel(creds: dict, trio: dict) -> dict | None:
    """GET freepoint/homenew —— 任务面板（各任务状态与可领积分）。失败返回 None。"""
    url = f"{TARGET}{PATH_FREEPOINT}/homenew?{query_with(creds, trio)}"
    payload, status, err = api_json("GET", url, web_headers(creds))
    if payload is None or status >= 400:
        return None
    if payload.get("errno") != 0:
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def complete_task(creds: dict, trio: dict, task_type: str) -> tuple[dict | None, int, str]:
    """POST freepoint/taskComplete —— 领取指定任务奖励（form 编码）。

    返回 (data, status, err)。data 里的关键字段：
      complete_status = SUCCESS（领到）| BADGE_ALREADY_RECEIVED（已领过）
      reward_point    = 本次入账积分数（已领过为 0）
    """
    url = f"{TARGET}{PATH_FREEPOINT}/taskComplete?{query_with(creds, trio)}"
    body = f"task_type={urllib.parse.quote(task_type, safe='')}"
    headers = web_headers(creds, content_type="application/x-www-form-urlencoded")
    payload, status, err = api_json("POST", url, headers, body)
    if payload is None:
        return None, status, err
    if status >= 400:
        head = str(payload.get("errmsg") or payload.get("message") or "").strip()
        return None, status, head or f"HTTP {status}"
    if payload.get("errno") != 0:
        return None, status, f"errno={payload.get('errno')} {payload.get('errmsg') or ''}".strip()
    data = payload.get("data")
    return (data if isinstance(data, dict) else {}), status, ""


def parse_claim(data: dict | None) -> tuple[str, int]:
    """taskComplete 的 data → (状态, 本次入账)。缺字段一律按没领到收（不猜）。"""
    if not isinstance(data, dict):
        return "", 0
    status = str(data.get("complete_status") or "").upper()
    try:
        reward = int(float(data.get("reward_point") or 0))
    except (TypeError, ValueError):
        reward = 0
    return status, reward


# ── 积分余额（freepoint 面板的当前积分）─────────────────────

def parse_total_points(panel: dict | None) -> int | None:
    """从任务面板里找「当前积分余额」。

    面板结构随活动改版会漂：先找顶层 point/total 字段族，找不到再进
    activities[].tabs[].tasks[] 的累计。全都没有返回 None（不猜 0）。
    """
    if not isinstance(panel, dict):
        return None
    for key in ("total_point", "totalPoint", "point", "points", "user_point",
                "userPoint", "current_point", "currentPoint", "score"):
        value = panel.get(key)
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            try:
                return int(float(value))
            except ValueError:
                continue
    return None


def parse_expiring(panel: dict | None) -> list[dict]:
    """从任务面板找积分有效期（到期日 + 数额）。

    freepoint 面板若有 expire 字段族（expire_time/valid_end…）按天聚合；
    没有就返回空 —— 库库活动积分多为「活动期内有效」，面板不给就不进日历，
    宁缺勿错（不按活动结束日硬算，虚报比不报更糟）。
    """
    if not isinstance(panel, dict):
        return []
    buckets: dict[str, int] = {}
    for act in panel.get("activities") or []:
        if not isinstance(act, dict):
            continue
        for tab in act.get("tabs") or []:
            if not isinstance(tab, dict):
                continue
            for task in tab.get("tasks") or []:
                if not isinstance(task, dict):
                    continue
                raw = (task.get("expire_time") or task.get("expireTime")
                       or task.get("valid_end") or task.get("validEnd"))
                if raw in (None, "", 0):
                    continue
                day = _expire_day(raw)
                if not day:
                    continue
                try:
                    amount = int(float(task.get("claimable_point")
                                       or task.get("reward_point") or 0))
                except (TypeError, ValueError):
                    continue
                if amount > 0:
                    buckets[day] = buckets.get(day, 0) + amount
    return [{"date": day, "amount": amount, "source": "每日积分"}
            for day, amount in sorted(buckets.items())]


def _expire_day(raw) -> str | None:
    """到期字段 → 本地日期 YYYY-MM-DD（秒/毫秒时间戳或 ISO 串都收）。"""
    if isinstance(raw, (int, float)) or (
            isinstance(raw, str) and raw.strip().isdigit()):
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            return None
        if seconds <= 0:
            return None
        if seconds < 100_000_000_000:
            seconds *= 1000
        try:
            return time.strftime("%Y-%m-%d", time.localtime(seconds / 1000))
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return None


# ── 30 天连签活动（kukuPcSignAct，2026-11 真机打通）────────────────
#
# 与 freepoint 家族并行的第二条签到线：连签日历（签到 +1 抽奖），走 act/v2 家族。
# 反爬两层，都可纯 HTTP 复刻（实测 errno=0）：
#   · jsToken：活动页 HTML 内联注入（eval(decodeURIComponent(`fn("...")`)) 形式），
#     每次渲染会变，按需现拉；
#   · jt 签名：百度 HTJ（dfxaf3）。SDK 加载失败时有**降级生成路径**（S 函数），
#     服务端接受降级串 —— "30$CODED--v30" + 位变换 base64(bid_n|href|ts|aid)。
#   · aid 固定 15459（百度网盘 HTJ app id，kuku 活动页复用），ev 按 urlMap 取
#     （signin→sign / claimreward→reward）。
# 活动实例 tid 由 kukuSignConf 配置下发（activityTid，随期数变化）。

ACT_PAGE_URL = f"{TARGET}/operation/activitys/kukuPcSignAct/main"
HTJ_AID = "15459"
ACTIVITY_CONF_KEY = "kukuSignConf"

# homepage 日历格子状态语义（真机核对）：
#   0=已签（当天）/断签待补（过去），1=今天未签，2=未来日期
CAL_SIGNED = 0
CAL_TODO_TODAY = 1
CAL_FUTURE = 2
CAL_MISSED = 3

ALREADY_SIGNED_ERRNO = 10152     # 重复签到的幂等码（"already signed"）


def _b64_shift(text: str) -> str:
    """HTJ 降级串的位变换编码（S 函数逆向）：先 b64，再逐字符移位。"""
    enc = base64.b64encode(text.encode()).decode()
    out = []
    for i, ch in enumerate(enc):
        c = ord(ch)
        if 41 <= c <= 122:
            shift = i % 32
            nc = c + shift
            if nc > 122:
                nc = 40 + (nc - 122)
            out.append(chr(nc))
        else:
            out.append(ch)
    return "".join(out)


def _fallback_jt(aid: str) -> str:
    """HTJ 降级 jt：SDK 不可用时的合法签名（bid_n 随机 32hex，href=活动页地址）。"""
    bid_n = "".join(random.choices("0123456789abcdef", k=32))
    ts = int(time.time() * 1000)
    return "30$CODED--v30" + _b64_shift(f"{bid_n}|{ACT_PAGE_URL}|{ts}|{aid}")


def fetch_js_token(creds: dict) -> tuple[str | None, int, str]:
    """拉活动页 HTML，提取内联注入的 jsToken。失败返回 (None, status, err)。"""
    url = ACT_PAGE_URL
    payload, status, err = api_json("GET", url, web_headers(creds), timeout=REFRESH_TIMEOUT)
    if payload is not None:
        return None, status, "活动页返回了 JSON 而不是 HTML（上游可能已调整）"
    # api_json 只认 JSON；HTML 场景拿到 (None, status, text[:200])。自己发一次拿全文。
    request = urllib.request.Request(url, headers=web_headers(creds))
    try:
        with urllib.request.urlopen(request, timeout=REFRESH_TIMEOUT,
                                    context=ssl.create_default_context()) as response:
            html = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return None, exc.code, str(exc)
    except Exception as exc:
        return None, -1, str(exc)
    match = re.search(r"eval\(decodeURIComponent\(`([^`]+)`\)\)", html)
    if not match:
        return None, status, "活动页没有 jsToken 注入段（可能未登录或页面已改版）"
    decoded = urllib.parse.unquote(match.group(1))
    token_match = re.search(r'"([0-9A-F]{30,})"', decoded)
    if not token_match:
        return None, status, "jsToken 解析失败"
    return token_match.group(1), status, ""


def fetch_sign_conf(creds: dict, trio: dict, js_token: str) -> tuple[dict | None, int, str]:
    """GET kukuSignConf → 活动 tid/时间窗/任务表。失败返回 (None, status, err)。"""
    query = query_with(creds, trio)
    url = (f"{TARGET}/act/v2/api/conf?{query}"
           f"&js_env=h5&app=400&jsToken={urllib.parse.quote(js_token, safe='')}"
           f"&conf_key={ACTIVITY_CONF_KEY}")
    payload, status, err = api_json("GET", url, web_headers(creds))
    if payload is None:
        return None, status, err
    if payload.get("errno") != 0:
        return None, status, f"errno={payload.get('errno')} {payload.get('show_msg') or ''}".strip()
    data = payload.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        conf = data[0].get("conf_value")
        if isinstance(conf, dict):
            return conf, status, ""
    return None, status, "kukuSignConf 响应缺少 conf_value"


def _act_query(creds: dict, trio: dict, js_token: str,
               extra: dict | None = None) -> str:
    """act/v2 家族的公共 query（拦截器 47120 同款参数）。"""
    rand = "".join(random.choices(_RAND_CHARS, k=16))
    cuid = "".join(random.choices(_RAND_CHARS, k=32))
    parts = [
        query_with(creds, trio),
        "js_env=h5",
        "app=400",
        f"rand={rand}",
        f"rand2={rand}",
        f"cuid={cuid}",
        f"time={int(time.time())}",
        f"jsToken={urllib.parse.quote(js_token, safe='')}",
    ]
    for key, value in (extra or {}).items():
        parts.append(f"{key}={urllib.parse.quote(str(value), safe='')}")
    return "&".join(parts)


_RAND_CHARS = string.ascii_lowercase + string.digits


def sign_homepage(creds: dict, trio: dict, js_token: str, tid: str) -> dict | None:
    """GET kukuai30/homepage —— 连签日历与今日状态。失败返回 None。"""
    url = (f"{TARGET}/act/v2/kukuai30/homepage"
           f"?{_act_query(creds, trio, js_token, {'tid': tid})}")
    payload, status, err = api_json("GET", url, web_headers(creds))
    if payload is None or payload.get("errno") != 0:
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def activity_signin(creds: dict, trio: dict, js_token: str,
                    tid: str) -> tuple[dict | None, int, str]:
    """POST kukuai30/signin —— 连签签到（幂等：重复打回 errno=10152 已签）。

    返回 (data, status, err)。data: {continuous: 连续天数, signed_today: bool}
    """
    jt = _fallback_jt(HTJ_AID)
    query = _act_query(creds, trio, js_token, {
        "tid": tid,
        "aid": HTJ_AID,
        "ev": "sign",
        "jt": jt,
    })
    url = f"{TARGET}/act/v2/kukuai30/signin?{query}"
    payload, status, err = api_json("POST", url, web_headers(creds), {})
    if payload is None:
        return None, status, err
    if payload.get("errno") != 0:
        return None, status, f"errno={payload.get('errno')} {payload.get('show_msg') or ''}".strip()
    data = payload.get("data")
    return (data if isinstance(data, dict) else {}), status, ""


def sign_today_status(homepage: dict | None) -> bool | None:
    """homepage 日历 → 今天是否已签。读不到返回 None（不猜）。

    status 语义（真机 2026-11-09 核对）：
      今天格子：1=未签；0=已签（签到后立即翻转）。
      过去格子：0=当天签过；3=断签待补。未来：2。
    """
    if not isinstance(homepage, dict):
        return None
    today = time.strftime("%Y-%m-%d")
    for cell in homepage.get("calendar") or []:
        if not isinstance(cell, dict) or cell.get("date") != today:
            continue
        status = cell.get("status")
        if status == CAL_TODO_TODAY:
            return False
        if status == CAL_SIGNED:
            return True
        return None        # 断签/未知值：无法断言今天已签，不猜
    return None


# ── 连签抽奖（lottery，2026-11-09 真机打通）────────────────────────
#
# 签到每天 +1 抽奖机会，任务也给抽奖次数。抽奖走 lottery 家族：
#   homeinfo  （POST form） {tid=lotteryTid, action:"lottery_cnt", comp_ver:2}
#              → data.lottery_cnt = 剩余次数
#   lotterycommit（POST JSON）{tid, cnt:1}  + jt 签名（ev=lottery）
#              → data[].gift.title = 中奖奖品（每次 1 发）
# 抽奖不产生积分，奖品直接发放（网盘空间/会员卡等），报告列奖品名即可。

PATH_LOTTERY_HOME = "/act/v2/component/lottery/homeinfo"
PATH_LOTTERY_COMMIT = "/act/v2/component/lottery/lotterycommit"


def lottery_count(creds: dict, trio: dict, js_token: str,
                  tid: str) -> int | None:
    """剩余抽奖次数。读不到返回 None（不猜）。"""
    headers = web_headers(creds, content_type="application/x-www-form-urlencoded")
    query = _act_query(creds, trio, js_token, {"tid": tid})
    body = f"tid={urllib.parse.quote(tid, safe='')}&action=lottery_cnt&comp_ver=2"
    payload, status, err = api_json("POST", f"{TARGET}{PATH_LOTTERY_HOME}?{query}",
                                    headers, body)
    if payload is None or payload.get("errno") != 0:
        return None
    data = payload.get("data") or {}
    try:
        return int(data.get("lottery_cnt"))
    except (TypeError, ValueError):
        return None


def lottery_commit(creds: dict, trio: dict, js_token: str,
                   tid: str) -> tuple[list[str] | None, int, str]:
    """抽一次（cnt=1）。返回 (奖品名列表, status, err)。

    一次 commit 只抽一发；抽 N 次需循环调用（每次重新生成 jt，防频控）。
    errno!=0 时 data=None；奖池未中也有 gift（type 字段区分）——标题照列。
    """
    jt = _fallback_jt(HTJ_AID)
    headers = web_headers(creds)   # JSON
    query = _act_query(creds, trio, js_token, {
        "tid": tid,
        "aid": HTJ_AID,
        "ev": "lottery",
        "jt": jt,
    })
    payload, status, err = api_json(
        "POST", f"{TARGET}{PATH_LOTTERY_COMMIT}?{query}", headers,
        {"tid": tid, "cnt": 1})
    if payload is None:
        return None, status, err
    if payload.get("errno") != 0:
        return None, status, f"errno={payload.get('errno')} {payload.get('show_msg') or ''}".strip()
    data = payload.get("data")
    titles: list[str] = []
    if isinstance(data, list):
        for item in data:
            gift = (item or {}).get("gift") if isinstance(item, dict) else None
            title = str((gift or {}).get("title") or "").strip()
            if title:
                titles.append(title)
    return titles, status, ""


# ── 我的积分（cashier bizapi，2026-11-09 真机打通）────────────────
#
# 「我的积分」面板是客户端 cashier overlay 加载的远端页
# （{TARGET}/ndGenflowCashier/browse/pointdetail），数据走 /bizapi/gfpro/ 家族：
#   getgfvipremain       余额（totalPoint = 会员+充值+赠送 三分项）
#   gfgetpointrecordlist 流水（含逐笔有效期 → 过期日历的数据源）
#
# 2026-11-09：连签签到发放 10 积分（kukuSignConf.signPoint），有效期 7 天。
# 流水 actType=5 是签到奖励（正数）、2000 是模型调用消耗（负数小数）——
# 只有正数且带有效期的笔目进过期日历，消耗流水不算临期。

PATH_VIP_REMAIN = "/bizapi/gfpro/getgfvipremain"
PATH_POINT_RECORDS = "/bizapi/gfpro/gfgetpointrecordlist"
CASHIER_REFERER = f"{TARGET}/ndGenflowCashier/browse/pointdetail"
RECORD_ACT_SIGN_REWARD = 5        # 签到奖励
RECORD_ACT_CONSUME = 2000         # 模型调用消耗


def _cashier_headers(creds: dict) -> dict:
    headers = web_headers(creds)
    headers["Referer"] = CASHIER_REFERER
    return headers


def point_balance(creds: dict) -> tuple[dict | None, int, str]:
    """GET getgfvipremain → 积分余额三分项。

    返回 (data, status, err)。data 关键字段：
      list[].assetType=1 的笔目（token 积分池）：
        totalPoint=总余额, vipPoint=会员积分, chargePoint=充值积分,
        bonusPoint=赠送积分（签到奖励落在这）
    """
    payload, status, err = api_json("GET", f"{TARGET}{PATH_VIP_REMAIN}",
                                    _cashier_headers(creds))
    if payload is None:
        return None, status, err
    code = ((payload.get("status") or {}).get("code"))
    if code != 0:
        return None, status, f"status.code={code} {payload.get('errmsg') or ''}".strip()
    data = payload.get("data")
    if not isinstance(data, dict):
        return None, status, "getgfvipremain 响应缺少 data"
    return data, status, ""


def parse_balance(data: dict | None) -> dict | None:
    """余额响应 → {total, vip, charge, bonus}（assetType=1 的积分池）。

    没有积分池笔目时返回 None（账号从未获得过积分，与 0 不同）。
    """
    if not isinstance(data, dict):
        return None
    entry = None
    for row in data.get("list") or []:
        if isinstance(row, dict) and int(row.get("assetType") or 0) == 1:
            entry = row
            break
    if entry is None:
        return None

    def _num(key: str) -> int:
        try:
            return int(float(entry.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    return {
        "total": _num("totalPoint"),
        "vip": _num("vipPoint"),
        "charge": _num("chargePoint"),
        "bonus": _num("bonusPoint"),
    }


def point_records(creds: dict, page_size: int = 100) -> tuple[list[dict], int, str]:
    """GET gfgetpointrecordlist → 积分流水（category=all 覆盖获得+消耗）。

    page_size 默认 100（服务端上限）：流水按时间倒序，取太少会把**最老的发放
    记录**（尤其是尚在有效期的老笔目）挤出窗口，摊薄时缺少笔目会让差额被挂到
    错误的到期日。实测取 30 条时，2026-11-22 到期的 100 分（发放 10-22）刚好
    落在第 31 条上被丢掉，日历只剩 11-17 一笔。pageNum 翻页实测失效
    （pageNum>1 恒回空），故一次取满上限；余额仍大于流水合计时由
    parse_expiring_records 把差额挂到最早未来到期日兜底，不静默丢弃。
    """
    url = (f"{TARGET}{PATH_POINT_RECORDS}"
           f"?businessId=0&assetType=1&category=all&pageNum=1&pageSize={page_size}")
    payload, status, err = api_json("GET", url, _cashier_headers(creds))
    if payload is None:
        return [], status, err
    if payload.get("errno") != 0:
        return [], status, f"errno={payload.get('errno')} {payload.get('errmsg') or ''}".strip()
    data = payload.get("data") or {}
    rows = data.get("list")
    return (rows if isinstance(rows, list) else []), status, ""


def parse_expiring_records(rows: list[dict], balance_total: int | None = None) -> list[dict]:
    """流水 → 过期日历笔目：只收「获得（正数）且带有效期」的记录，逐笔摊薄后按到期日上报。

    消耗流水（actType=2000，负数小数）与无有效期记录不进日历；
    已过期（endTime < 今天）的不进 —— 与其它平台同一条「宁缺勿错」纪律。

    摊薄（2026-11-10 修正，与 Qoder/小浣熊同口径）：流水记的是发放**原额**，
    而模型调用消耗不绑定笔目、按积分池先进先出扣减 —— 直接照搬原额会把已花掉的
    份额虚报成临期。按余额接口的 totalPoint 从**最早发放**的笔目起逐笔扣减（先进
    先出），剩余份额各自留在自己的到期日上。

    旧实现（2026-11-09）把全部余额挂到「最近一笔发放」的单个到期日：真机上是
    「11-16 一笔 10 分、11-17 一笔 10 分」被合并成「11-17 一格 20 分」，日历上
    11-16 该有的 10 分凭空消失、11-17 多出一倍。当时与面板截图的巧合一致（那时
    恰好只有一笔 10 分），但两笔起就错——归属必须逐笔而不是整笔。
    """
    today = time.strftime("%Y-%m-%d")
    # 按获得时间正序（流水列表的时间倒序反过来）建立队列：消耗按发放顺序扣减。
    # 排序用发放时刻 startTime（缺失退回 createTime），不用到期日——同一到期日
    # 可能对应多笔（如 7 天有效模式下每天一笔），按到期日排会打乱发放先后。
    gains: list[tuple[float, str, float]] = []   # (发放时间, 到期日, 原额)
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            point = float(row.get("point") or 0)
        except (TypeError, ValueError):
            continue
        if point < 0:
            continue          # 消耗不绑定笔目，统一按余额差额摊薄（见下）
        try:
            end_ts = int(float(row.get("endTime") or 0))
        except (TypeError, ValueError):
            continue          # 脏字段（非数字）跳过，不拖垮整次上报
        if end_ts <= 0:
            continue          # 无有效期的记录不入册
        try:
            day = time.strftime("%Y-%m-%d", time.localtime(end_ts))
        except (OverflowError, OSError, ValueError):
            continue
        try:
            start_ts = float(row.get("startTime") or row.get("createTime") or 0)
        except (TypeError, ValueError):
            start_ts = 0.0
        gains.append((start_ts, day, point))
    if not gains:
        return []

    # 余额为 0 / 未知：没有任何笔目会因到期损失（与 Qoder/小浣熊同一条纪律）
    if balance_total is None or balance_total <= 0:
        return []
    try:
        balance = float(balance_total)
    except (TypeError, ValueError):
        return []

    total = sum(amount for _s, _d, amount in gains)
    consumed = total - balance      # 已花掉（含已到期作废）的部分

    # 先进先出口径：已消耗的积分从**最早发放**的笔目起扣（老笔目先花掉），
    # 剩余份额完整留在较晚发放的笔目上。真机核对（2026-11-10，余额 20）：
    # 原额合计 170 = 11-22:100 + 2027:30 + 11-05:10 + 11-13:10 + 11-16:10
    # + 11-17:10，扣掉 150 后恰好剩「11-16:10 + 11-17:10」——与「我的积分」
    # 面板逐笔明细一致。若反过来从最新发放扣，剩余会落在最老那笔（11-22:80），
    # 与面板明显不符。
    buckets: dict[str, float] = {}
    if consumed <= 0:
        held_rows = [(day, amount) for _s, day, amount in gains]
    else:
        held_rows = []
        eat = consumed
        for _start, day, amount in sorted(gains, key=lambda g: g[0]):
            take = min(amount, eat)
            eat -= take
            held = amount - take
            if held > 0:
                held_rows.append((day, held))

    for day, held in held_rows:
        # 到期日早于今天的不上报：其作废由到期当日那次运行捕获进快照，
        # 反复重报会重复计入作废统计
        if day >= today and round(held) >= 1:
            buckets[day] = buckets.get(day, 0.0) + held

    # 余额仍大于流水原额合计：流水窗口没覆盖全部获得笔目（分页上限 / 上游截断）。
    # 差额是真实持有、且终将过期的积分，静默丢弃就是漏报（小浣熊 2026-10-12 的
    # 教训：limit 不足时差额被吃掉，日历少算 424）。挂到已知的最早未来到期日。
    gap = balance - total
    if gap >= 1:
        future = [day for _s, day, _a in gains if day >= today]
        if future:
            buckets[min(future)] = buckets.get(min(future), 0.0) + gap
    if not buckets:
        return []
    return [{"date": day, "amount": int(round(amount)), "source": "签到奖励"}
            for day, amount in sorted(buckets.items())]
