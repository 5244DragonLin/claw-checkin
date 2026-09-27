#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 积分到期检查 —— 读取本机登录态，查询官方积分接口，输出 ClawCheckin 契约 JSON。

数据流（与 ClawCheckin "过期日历" 集成）：
    本机 token(workbuddy-desktop.info, 可能为 $wbEncrypted 加密信封)
    → 用 WorkBuddy 运行时(Electron safeStorage)解密
    → POST https://copilot.tencent.com/billing/meter/get-user-resource（Bearer 鉴权）
    → 解析"权益赠送包"（CapacityType != 4，剩余 > 0）的到期时间
    → 输出 expiring: [{date, amount, source}]，由 ledger 写入 expiring_snapshot 快照表
    → web 端 /api/expiring 自动纳入"过期日历"展示（无需改前端）

参考实现：
    - xmgzxmgz/workbuddy-account-dashboard 的 credits-api.js（接口路径、Accounts 字段解析、
      UA 必须带浏览器 User-Agent，否则接口返回 403 code=10085"请求不合法"）
    - 88lin/workbuddy-auto-signin 的 signin.py（$wbEncrypted 加密信封的 Electron helper 解密协议）

输出契约（ClawCheckin runner 校验）：
    {
      "result": "CLAIMED|ALREADY|NO_SESSION|NO_AUTH|NETWORK|ERROR",
      "report": "...",
      "today_credit": 0,          # 本脚本不产生签到积分，恒 0
      "streak_days": 0,
      "total_credits": <当前剩余额度(权益赠送包, 向上取整; 体验版周期额度不计入)>,
      "expiring": [ { "date": "2026-10-08", "amount": 100, "source": "CodeBuddy个人版国内运营裂变包" } ]
    }

说明：
    - 体验版（CapacityType=4）是周期额度重置而非积分作废，不计入 expiring，仅在 report 中提示。
    - 已用尽的包（剩余 0）不计入 expiring。
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import date, datetime

API_URL = "https://copilot.tencent.com/billing/meter/get-user-resource"
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TOKEN_LIMIT = 32768
HELPER_TIMEOUT = 15.0

AUTH_REASONS = {
    "INVALID_FORMAT": "登录凭据格式无效，请检查凭据来源和客户端版本",
    "UNSUPPORTED_ENVELOPE": "尚不支持此加密凭据格式，请更新脚本；重新登录不会改变加密格式",
    "RUNTIME_NOT_FOUND": "未找到 WorkBuddy 客户端，请用 WORKBUDDY_EXE 指定其可执行文件",
    "RUNTIME_UNAVAILABLE": "客户端运行时不支持所需的原生存储接口，请检查客户端和脚本版本",
    "KEY_MISMATCH": "凭据与所选客户端的密钥不匹配，请用 WORKBUDDY_EXE 指定对应客户端",
    "DECRYPT_FAILED": "加密凭据认证失败，请检查客户端版本及凭据是否完整",
    "HELPER_TIMEOUT": "凭据处理超时，已停止子进程，请稍后重试",
    "HELPER_PROTOCOL": "客户端凭据助手返回无效结果，请检查客户端和脚本版本",
}

# 与 88lin/workbuddy-auto-signin 的 signin.py 相同的 helper 协议：
# 用 ELECTRON_RUN_AS_NODE=1 运行 WorkBuddy.exe -e <js>，stdin 传 JSON 请求，stdout 收 JSON 应答。
AUTH_HELPER_JS = r"""
'use strict';
const crypto = require('crypto');
const inputLimit = 65536;
const failure = reason => { throw {reason}; };
const object = x => x !== null && typeof x === 'object' && !Array.isArray(x);
function base64(value, length) {
  if (typeof value !== 'string' || value.length > inputLimit) failure('INVALID_FORMAT');
  const bytes = Buffer.from(value, 'base64');
  if (bytes.toString('base64') !== value || (length !== undefined && bytes.length !== length))
    failure('INVALID_FORMAT');
  return bytes;
}
function utf8(bytes) {
  const text = bytes.toString('utf8');
  if (!Buffer.from(text, 'utf8').equals(bytes)) failure('INVALID_FORMAT');
  return text;
}
function decode(value) {
  if (!object(value) || Object.keys(value).sort().join(',') !== '$wbEncrypted,envelope' || value.$wbEncrypted !== 1)
    failure('UNSUPPORTED_ENVELOPE');
  let envelope;
  try { envelope = JSON.parse(utf8(base64(value.envelope))); }
  catch (e) { failure(e.reason || 'INVALID_FORMAT'); }
  if (!object(envelope) || !Number.isInteger(envelope.suite)) failure('INVALID_FORMAT');
  if (envelope.suite !== 1) failure('UNSUPPORTED_ENVELOPE');
  if (Object.keys(envelope).sort().join(',') !== 'authTag,ciphertext,keyId,nonce,suite' ||
      typeof envelope.keyId !== 'string' || !/^[0-9a-f]{16}$/.test(envelope.keyId)) failure('INVALID_FORMAT');
  return {keyId: envelope.keyId, nonce: base64(envelope.nonce, 12),
    tag: base64(envelope.authTag, 16), ciphertext: base64(envelope.ciphertext)};
}
function nativeStorage() {
  try {
    const storage = process._linkedBinding('electron_browser_workbuddy_storage');
    if (typeof storage.loggerGet !== 'function') failure('RUNTIME_UNAVAILABLE');
    return storage;
  } catch (_) { failure('RUNTIME_UNAVAILABLE'); }
}
function decrypt(envelope) {
  let payload;
  try { payload = JSON.parse(nativeStorage().loggerGet()); }
  catch (_) { failure('RUNTIME_UNAVAILABLE'); }
  let key;
  let plaintext;
  try {
    if (!object(payload) || payload.version !== 1) failure('RUNTIME_UNAVAILABLE');
    let secret;
    try { secret = base64(payload.atRestSecretKey, 32); }
    catch (_) { failure('RUNTIME_UNAVAILABLE'); }
    const empty = secret.every(b => b === 0);
    secret.fill(0);
    if (empty) failure('RUNTIME_UNAVAILABLE');
    key = crypto.createHash('sha256').update(payload.atRestSecretKey, 'utf8').digest();
    payload = null;
    if (crypto.createHash('sha256').update(key).digest('hex').slice(0, 16) !== envelope.keyId)
      failure('KEY_MISMATCH');
    const lp = s => {
      const bytes = Buffer.from(s, 'utf8');
      const length = Buffer.alloc(4); length.writeUInt32BE(bytes.length);
      return Buffer.concat([length, bytes]);
    };
    const aad = Buffer.concat([Buffer.from('WB-AAD\0', 'ascii'), Buffer.from([1]),
      lp('WBEV1'), lp('sym-v1'), Buffer.from([0, 0, 0, 1]), lp(envelope.keyId), Buffer.from([2, 0, 0])]);
    try {
      const cipher = crypto.createDecipheriv('aes-256-gcm', key, envelope.nonce, {authTagLength: 16});
      cipher.setAAD(aad); cipher.setAuthTag(envelope.tag);
      plaintext = Buffer.concat([cipher.update(envelope.ciphertext), cipher.final()]);
    } catch (_) { failure('DECRYPT_FAILED'); }
    const token = utf8(plaintext);
    if (!token.length || token.length > 32768 || !/^[A-Za-z0-9._~+\/-]+=*$/.test(token))
      failure('INVALID_FORMAT');
    return token;
  } finally {
    if (key) key.fill(0);
    if (plaintext) plaintext.fill(0);
  }
}
let chunks = [], size = 0;
function reply(value) {
  process.stdout.write(JSON.stringify({version: 1, ...value}), () => process.exit(value.ok ? 0 : 1));
}
process.stdin.on('data', chunk => {
  size += chunk.length;
  if (size > inputLimit) reply({ok: false, reason: 'INVALID_FORMAT'});
  else chunks.push(chunk);
});
process.stdin.on('error', () => reply({ok: false, reason: 'HELPER_PROTOCOL'}));
process.stdin.on('end', () => {
  try {
    const request = JSON.parse(utf8(Buffer.concat(chunks))); chunks = [];
    if (!object(request) || request.version !== 1) failure('HELPER_PROTOCOL');
    if (request.operation === 'probe') {
      nativeStorage();
      if (!crypto.getCiphers().includes('aes-256-gcm')) failure('RUNTIME_UNAVAILABLE');
      reply({ok: true, electron: process.versions.electron || 'unknown'});
    } else if (request.operation === 'decrypt') {
      reply({ok: true, accessToken: decrypt(decode(request.value))});
    } else failure('HELPER_PROTOCOL');
  } catch (e) {
    const reasons = ['INVALID_FORMAT','UNSUPPORTED_ENVELOPE','RUNTIME_UNAVAILABLE',
      'KEY_MISMATCH','DECRYPT_FAILED','HELPER_PROTOCOL'];
    reply({ok: false, reason: reasons.includes(e.reason) ? e.reason : 'HELPER_PROTOCOL'});
  }
});
"""


class AuthError(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(AUTH_REASONS.get(reason, "本地未找到有效登录会话，请先登录客户端"))


def _valid_token(token):
    return (isinstance(token, str) and 0 < len(token) <= TOKEN_LIMIT
            and re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token) is not None)


def find_workbuddy_runtime():
    """定位 WorkBuddy.exe：优先 WORKBUDDY_EXE 环境变量，其次常见安装路径。"""
    override = os.environ.get("WORKBUDDY_EXE")
    candidates = []
    if override:
        candidates.append(os.path.abspath(os.path.expanduser(override)))
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or ""
        candidates += [
            os.path.join(local, "Programs", "WorkBuddy", "WorkBuddy.exe"),
            r"D:\WorkBuddy\WorkBuddy.exe",
            r"D:\Program Files\WorkBuddy\WorkBuddy.exe",
        ]
        for name in ("ProgramFiles", "ProgramFiles(x86)"):
            if os.environ.get(name):
                candidates.append(os.path.join(os.environ[name], "WorkBuddy", "WorkBuddy.exe"))
    elif sys.platform == "darwin":
        candidates += [
            "/Applications/WorkBuddy.app/Contents/MacOS/WorkBuddy",
            os.path.join(os.path.expanduser("~"), "Applications", "WorkBuddy.app", "Contents", "MacOS", "WorkBuddy"),
        ]
    for path in candidates:
        if path and os.path.isfile(path) and (os.name != "nt" or os.access(path, os.X_OK)):
            return path
    return None


def run_auth_helper(exe, request):
    """向 WorkBuddy 运行时发起 helper 请求（probe / decrypt），返回 (reply, err)。"""
    data = json.dumps(dict(request, version=1), ensure_ascii=True).encode("ascii")
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("NODE_", "ELECTRON_", "WORKBUDDY_"))}
    env["ELECTRON_RUN_AS_NODE"] = "1"
    try:
        proc = subprocess.Popen(
            [exe, "-e", AUTH_HELPER_JS],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError:
        return None, "RUNTIME_UNAVAILABLE"
    try:
        out, _err = proc.communicate(data, timeout=HELPER_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        return None, "HELPER_TIMEOUT"
    try:
        reply = json.loads(out.decode("utf-8"))
    except (ValueError, UnicodeError):
        return None, "HELPER_PROTOCOL"
    if not isinstance(reply, dict) or reply.get("version") != 1:
        return None, "HELPER_PROTOCOL"
    if reply.get("ok") is not True:
        reason = reply.get("reason")
        return None, reason if isinstance(reason, str) and reason in AUTH_REASONS else "HELPER_PROTOCOL"
    return reply, None


def load_login():
    """读取本机 WorkBuddy 登录态文件，返回 (token_path, token, uid, err)。"""
    local = os.environ.get("LOCALAPPDATA") or ""
    path = os.path.join(local, "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info")
    if not os.path.isfile(path):
        return None, None, None, "NO_SESSION_FILE"
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None, None, None, "NO_SESSION_FILE"
    token = (d.get("auth") or {}).get("accessToken")
    uid = (d.get("account") or {}).get("uid") or ""
    if not token:
        return None, None, None, "NO_SESSION_FILE"
    return path, token, uid, None


def resolve_token(token):
    """明文 token 直接用；$wbEncrypted 加密信封走 WorkBuddy 运行时解密。返回 (token, err)。"""
    if isinstance(token, str):
        if not _valid_token(token):
            return None, "INVALID_FORMAT"
        return token, None
    if not isinstance(token, dict):
        return None, "INVALID_FORMAT"
    if set(token) != {"$wbEncrypted", "envelope"} or token.get("$wbEncrypted") != 1:
        return None, "UNSUPPORTED_ENVELOPE"
    exe = find_workbuddy_runtime()
    if not exe:
        return None, "RUNTIME_NOT_FOUND"
    reply, err = run_auth_helper(exe, {"operation": "decrypt", "value": token})
    if err:
        return None, err
    tok = (reply or {}).get("accessToken")
    if not _valid_token(tok):
        return None, "HELPER_PROTOCOL"
    return tok, None


def fetch_user_resource(token, uid):
    """调用官方积分接口，返回 (accounts, err)。"""
    req = urllib.request.Request(API_URL, data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("X-User-Id", uid)
    # 接口会拒绝非浏览器 UA（403 code=10085"请求不合法"），必须带浏览器 UA
    req.add_header("User-Agent", DEFAULT_UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            js = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return None, "AUTH_REJECTED"
        if e.code == 403:
            return None, "FORBIDDEN"
        return None, "NETWORK"
    except (OSError, ValueError):
        return None, "NETWORK"
    if js.get("code") != 0:
        return None, "API_ERROR:" + str(js.get("msg", ""))
    data = js.get("data") or {}
    accounts = ((data.get("Response") or {}).get("Data") or {}).get("Accounts") or []
    return accounts, None


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _cycle_end_date(cycle_end):
    """CycleEndTime 可能是 'YYYY-MM-DD HH:MM:SS' 或毫秒时间戳，统一返回日期字符串 YYYY-MM-DD。"""
    if isinstance(cycle_end, (int, float)):
        try:
            return datetime.fromtimestamp(cycle_end / 1000).strftime("%Y-%m-%d")
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(cycle_end, str):
        m = re.match(r"(\d{4}-\d{2}-\d{2})", cycle_end.strip())
        if m:
            return m.group(1)
    return None


def build_expiring(accounts):
    """解析 Accounts：仅纳入权益赠送包（CapacityType != 4）且剩余 > 0 的未来到期项。

    体验版（CapacityType=4）为周期额度重置，不计入"积分过期"日历。
    返回 (expiring, summary)。
    """
    today = date.today().isoformat()
    packages = []
    for a in accounts:
        capacity_type = a.get("CapacityType")
        try:
            is_trial = int(capacity_type) == 4
        except (TypeError, ValueError):
            is_trial = False
        if is_trial:
            # 体验版为周期额度（按月重置），官方 plans-usage 页按周期口径展示；
            # Capacity* 字段为总池口径（remain=500 但周期内已扣完），须用 Cycle* 字段。
            remain = _as_float(a.get("CycleCapacityRemainPrecise") if a.get("CycleCapacityRemainPrecise") is not None
                               else a.get("CycleCapacityRemain"))
            size = _as_float(a.get("CycleCapacitySizePrecise") if a.get("CycleCapacitySizePrecise") is not None
                             else a.get("CycleCapacitySize"))
            used = _as_float(a.get("CycleCapacityUsedPrecise") if a.get("CycleCapacityUsedPrecise") is not None
                             else a.get("CycleCapacityUsed"))
        else:
            remain = _as_float(a.get("CapacityRemainPrecise") if a.get("CapacityRemainPrecise") is not None
                               else a.get("CapacityRemain"))
            size = _as_float(a.get("CapacitySizePrecise") if a.get("CapacitySizePrecise") is not None
                             else a.get("CapacitySize"))
            used = _as_float(a.get("CapacityUsedPrecise") if a.get("CapacityUsedPrecise") is not None
                             else a.get("CapacityUsed"))
        end = _cycle_end_date(a.get("CycleEndTime"))
        packages.append({
            "name": a.get("PackageName") or ("体验版" if is_trial else "权益赠送包"),
            "is_trial": is_trial,
            "remain": remain,
            "size": size,
            "used": used,
            "cycle_end": end,
        })

    expiring = []
    for p in packages:
        if p["is_trial"] or p["remain"] <= 0 or not p["cycle_end"] or p["cycle_end"] < today:
            continue
        expiring.append({
            "date": p["cycle_end"],
            "amount": int(math.ceil(p["remain"])),
            "source": p["name"],
        })
    expiring.sort(key=lambda e: e["date"])

    gift_remain = sum(p["remain"] for p in packages if not p["is_trial"])
    gift_size = sum(p["size"] for p in packages if not p["is_trial"])
    trial_remain = sum(p["remain"] for p in packages if p["is_trial"])
    trial_end = next((p["cycle_end"] for p in packages if p["is_trial"] and p["cycle_end"]), None)
    summary = {
        "gift_remain": gift_remain,
        "gift_size": gift_size,
        "trial_remain": trial_remain,
        "trial_end": trial_end,
        "expiring_total": sum(e["amount"] for e in expiring),
        "expiring_count": len(expiring),
    }
    return expiring, summary


def emit(result, report, total_credits=0, expiring=None):
    """按 ClawCheckin 契约输出末行 JSON。"""
    print(json.dumps({
        "result": result,
        "report": report,
        "today_credit": 0,
        "streak_days": 0,
        "total_credits": total_credits,
        "expiring": expiring or [],
    }, ensure_ascii=False))


def main():
    _path, token, uid, load_err = load_login()
    if load_err:
        emit("NO_SESSION", "未找到 WorkBuddy 登录态文件，请先登录 WorkBuddy 客户端")
        return 2

    tok, derr = resolve_token(token)
    if derr:
        emit("NO_AUTH", "WorkBuddy 凭据处理失败：" + AUTH_REASONS.get(derr, derr))
        return 2

    accounts, ferr = fetch_user_resource(tok, uid)
    if ferr:
        if ferr == "AUTH_REJECTED":
            emit("NO_AUTH", "服务端拒绝认证（HTTP 401），请重新登录 WorkBuddy")
        elif ferr == "FORBIDDEN":
            emit("NO_AUTH", "服务端拒绝访问（HTTP 403），请检查登录状态")
        elif ferr.startswith("API_ERROR:"):
            emit("ERROR", "积分接口返回异常：" + ferr.split(":", 1)[1])
        else:
            emit("NETWORK", "查询积分接口失败（网络异常），请稍后重试")
        return 2

    expiring, summary = build_expiring(accounts)
    # 账户总积分仅统计权益赠送包；体验版为周期重置额度（非持有积分），不计入
    grand = summary["gift_remain"]
    report = (
        f"WorkBuddy 积分检查完成：权益赠送包剩余 {math.ceil(summary['gift_remain'])} / "
        f"{math.ceil(summary['gift_size'])} 分"
    )
    if summary["expiring_count"]:
        report += (f"，其中 {summary['expiring_count']} 个包将在未来到期 "
                   f"（合计 {summary['expiring_total']} 分，最早 {expiring[0]['date']}）")
    else:
        report += "，近期无到期权益包"
    if summary["trial_remain"] > 0:
        report += (f"；体验版剩余 {math.ceil(summary['trial_remain'])} 分"
                   + (f"（{summary['trial_end']} 周期重置，不计入过期）" if summary["trial_end"] else ""))
    emit("ALREADY", report, total_credits=int(math.ceil(grand)), expiring=expiring)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # 面板不允许崩
        emit("ERROR", "脚本异常：" + str(e))
        sys.exit(1)
