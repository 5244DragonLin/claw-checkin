"""AutoClaw 凭据的跨机漫游（同步盘双机轮流使用场景）。

背景：AutoClaw 2 的 accessToken 有效期 24 小时，refreshToken 由服务端强制轮换
（一次一用）；桌面端凭据库 %APPDATA%/AutoClaw-official 是 DPAPI 加密的，绑死
Windows 用户，凭据文件拷到另一台电脑解不开。

两个文件的分工：
  auths/autoclaw.json       —— 当前可用的 accessToken（bearer JWT，本身不绑机器）
  auths/autoclaw_sync.json  —— 会话漫游镜像：access + refreshToken + deviceId

漫游规则（见各签到脚本 _refresh_from_autoclaw2 的接入）：
  1. 本地凭据库可用：照旧走本地库，同时把本轮凭据记入镜像（monotonic 守卫，
     exp 更旧的一轮不覆盖镜像——那可能是另一台电脑刚轮换出的更新凭据）；
  2. 本地库缺失（未登录/未安装/非 Windows）或续期失败：改用镜像里的
     refreshToken 向官方端点续期，轮换结果写回镜像；
  3. 本机有凭据库时把轮换结果也写回本地库——另一台电脑用镜像轮换过会话后，
     本机的旧 refreshToken 已失效，写回可让本机客户端免于重新登录。

客户端运行中（AutoClaw2.exe）一律不并发续期/写库：客户端内存里持有凭据缓存，
并发写会导致其下次刷新失败而要求重新登录。
"""

from __future__ import annotations

import os
import time

import _syncio

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIRROR_PATH = os.path.join(PROJECT_ROOT, "auths", "autoclaw_sync.json")
STATE_PATH = os.path.join(PROJECT_ROOT, "data", "autoclaw_state.json")


def load() -> dict | None:
    """读取会话镜像，缺字段/损坏返回 None。"""
    doc = _syncio.load_json(MIRROR_PATH)
    if not isinstance(doc, dict):
        return None
    access = str(doc.get("accessToken") or "").strip()
    refresh = str(doc.get("refreshToken") or "").strip()
    try:
        exp = float(doc.get("exp") or 0)
    except (TypeError, ValueError):
        exp = 0.0
    if not access or not refresh:
        return None
    return {"access": access, "refresh": refresh, "exp": exp,
            "device_id": str(doc.get("deviceId") or ""),
            "region": str(doc.get("region") or "cn")}


def note(access: str, refresh: str, exp: float, device_id: str, region: str = "cn") -> bool:
    """把一轮凭据记入镜像（单调守卫：exp 明显更旧则不覆盖）。"""
    if not access or not refresh or exp <= 0:
        return False
    cur = load()
    if cur and exp < cur["exp"] - 60:
        return False
    doc = {"accessToken": access, "refreshToken": refresh, "exp": exp,
           "deviceId": device_id, "region": region,
           "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S")}
    return _syncio.atomic_write_json(MIRROR_PATH, doc)


def fallback_refresh(auth_dir: str, *, upsert, remote_refresh, jwt_exp,
                     vault: dict | None = None, vault_writer=None,
                     client_running=None) -> bool:
    """本地凭据库走不通时的镜像续期路径，返回是否为 auths/ 备好了有效 token。

    upsert(auth_dir, access, exp) / remote_refresh(refresh, device_id, old_access)
    / jwt_exp(token) / vault_writer(vault, access, refresh) / client_running()
    由调用脚本注入，保持与各自的既有实现一致。
    """
    mirror = load()
    if not mirror:
        return False
    running = bool(client_running()) if client_running else False
    if mirror["exp"] > time.time() + 300:
        return upsert(auth_dir, mirror["access"], mirror["exp"])
    if running:
        return False  # 客户端运行中会自行轮换，不并发续期
    renewed = remote_refresh(mirror["refresh"], mirror["device_id"], mirror["access"])
    if not renewed:
        return False
    exp = jwt_exp(renewed["access"].split(" ", 1)[-1])
    note(renewed["access"], renewed["refresh"], exp, mirror["device_id"], mirror["region"])
    if vault and vault_writer:
        vault_writer(vault, renewed["access"], renewed["refresh"])
    return upsert(auth_dir, renewed["access"], exp)


# ---------- data/autoclaw_state.json 的同步盘冲突治理 ----------

def _state_candidates() -> list[str]:
    """主状态文件 + 同步盘冲突副本（<name>_冲突文件_<user>_<时间戳>.json）。"""
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
    return paths


def load_state(default: dict | None = None) -> dict:
    """读取积分状态，自动合并同步盘冲突副本（取 last_date 最新的记录）。

    state 只有三个数（total_credits/streak_days/last_date），轮流使用下取
    「最新签到日」的那份即是权威；合并后删除冲突副本，避免无限堆积。
    """
    base = {"total_credits": 0, "streak_days": 0, "last_date": "", "last_daily": 0}
    if isinstance(default, dict):
        base.update(default)
    best, best_key = None, ("", -1, -1)
    for path in _state_candidates():
        doc = _syncio.load_json(path)
        if not isinstance(doc, dict):
            continue
        key = (str(doc.get("last_date") or ""),
               float(doc.get("total_credits") or 0),
               float(doc.get("streak_days") or 0))
        if key > best_key:
            best, best_key = doc, key
    if best is None:
        return dict(base)
    merged = dict(base)
    merged.update({k: v for k, v in best.items() if k in base})
    # 冲突副本已并入主文件则清理（主文件写入由调用方完成，这里只删副本）
    for path in _state_candidates():
        if os.path.abspath(path) != os.path.abspath(STATE_PATH):
            try:
                os.remove(path)
            except OSError:
                pass
    return merged


def save_state(state: dict) -> bool:
    return _syncio.atomic_write_json(STATE_PATH, state)
