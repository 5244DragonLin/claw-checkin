"""JSON 账本：checkin_log 签到日志 + expiring_snapshot 临期积分快照

面板只读不写：数据来源 = 各脚本 stdout 的一行 JSON + data/ledger.json。

快照行的 live 字段（过期日历的真实实时口径，2026-10 起）：
- live=1：最近一次接口回报里确实存在、平台此刻还持有的积分；
- live=0：接口已不再回报它（当天被消耗掉、或已过期待清理）——属于历史/失效行，
  不再出现在未来日历的「即将过期」格子里（展示会让当日数字与平台客户端对不上），
  也不计入「过去 30 天已作废」（作废只统计到期日结束时仍持有的积分，live=1）；
  行本身保留，作为消耗流水供追溯。
- 未来日历只看「date >= 今天且 live=1」；date < 今天 的行进不了日历，「过去 30 天
  已作废」按存储的 live 读取：live=1 ＝ 到期日结束时仍持有＝真作废，live=0 ＝ 已花掉。

为什么是单文件 JSON 而不是 SQLite：账本随同步盘在两台电脑间漫游，SQLite 是
多文件（-journal/-wal）且对「拷贝中的半写状态」零容忍——同步盘在写入窗口
抓到的库文件落到另一台电脑就是损坏库；两边各改一版还会留下冲突副本
（实测出现 `ledger_冲突文件_*.db`）。单文件 JSON + 临时文件原子替换保证同步盘
任意时刻读到的都是完整可解析的文件；万一两台都改过，启动时按行并集自动合并
冲突副本后清理。

首次启动自动从旧 SQLite（data/ledger.db，含其冲突副本）迁移，迁移完把旧库
改名为 *.migrated 保留备查。
"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

LEDGER_VERSION = 1

# 凭据未就绪/网络抖动类失败：调度器当日自动补跑（ERROR 等脚本缺陷不重试）
RETRYABLE_RESULTS = ("NO_AUTH", "NETWORK")

# 快照保鲜期（天）：平台最后一次回报 expiring 距今超过该天数，其全部快照
# 视为过期数据，不再计入过期统计——脚本连续失败或已移除时，旧快照不应
# 长期占据过期日历（宁可显示为无数据，也不显示作废数据）。
STALE_SNAPSHOT_DAYS = 3

# 同步盘冲突副本命名：<stem>_冲突文件_<账号>_<时间戳>.<ext>（百度网盘实测）
_CONFLICT_MARK = "冲突文件"


def _atomic_write_text(path: Path, text: str) -> bool:
    """同目录临时文件 + os.replace 原子替换；被同步盘短暂占用时自动重试。"""
    directory = path.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".tmp-", suffix=path.name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            for attempt in range(4):
                try:
                    os.replace(tmp, str(path))
                    return True
                except PermissionError:
                    if attempt == 3:
                        raise
                    time.sleep(0.25)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return True
    except OSError:
        return False


def _log_key(row: dict) -> tuple:
    """签到日志的「同一笔运行」判重键（不含自增 id 与 raw 原文）。"""
    return (row["ts"], row["platform"], row["script"], row["result"],
            row["credit"], row["total_credits"], row["streak_days"],
            row["report"], row["trigger"], row["parse_ok"])


def _snap_key(row: dict) -> tuple:
    """快照判重键，与 insert_run 的覆盖口径一致。"""
    return (row["platform"], row["date"], row["amount"], row["source"])


def _amount_of(entry: dict) -> int:
    """expiring 金额转整数，非法值按 0 处理（脚本字段脏不能拖垮整次入账）。"""
    try:
        return int(entry.get("amount", 0) or 0)
    except (TypeError, ValueError):
        return 0


def aggregate_expiring(expiring) -> list[dict]:
    """把一次回报的 expiring 按 (date, source) 合并求和。

    同一来源同一天可能对应多笔互相独立的权益包（实测 WorkBuddy 在 2026-11-15
    就有 3 笔各 100 分的裂变包），而快照表按 (platform, date, amount, source) 判重：
    不先合并就会把 3 笔并成 1 笔，日历少算 200 分。合并后同一份回报的集合是稳定的，
    重复回报也能逐字段比对出来「有没有真的变」，不会每次都误判为有变化。
    """
    totals: dict[tuple[str, str], int] = {}
    for e in expiring:
        if not isinstance(e, dict) or not e.get("date"):
            continue
        key = (str(e["date"]), str(e.get("source", "")))
        totals[key] = totals.get(key, 0) + _amount_of(e)
    return [{"date": d, "amount": a, "source": s}
            for (d, s), a in sorted(totals.items())]


# 哪些触发方式留下的记录算「今天真实跑过签到」。
# 体检（health）是接入校验、积分刷新（refresh/page_load）只查余额，两者都会跑
# 脚本拿到 ALREADY，但那是「账号此刻的现状」，不是今天的签到动作。若把它们计入，
# 早上打开面板（后台刚刷新过积分）就会把四个平台全显示成「已签到」；反过来
# 昨天的签到留在账本里，也会因为「最近一条是 ALREADY」被误判成今天的成绩。
CHECKIN_TRIGGERS = ("schedule", "manual", "retry")

# 状态核查（probe）：只读查询，不算签到动作，也不参与临期快照重建
# （--status 不带 expiring，若按常规口径入账会把当天快照全标成失效）
PROBE_TRIGGER = "probe"


def _signed_flag(rr) -> int:
    """把脚本对「今天签没签」的表态落成三态整数。

      1 = 明确已签（signed_today: true）
      0 = 没表态（未实现该字段/查不到）
     -1 = 明确**未**签（signed_today: false，例如「还没到开窗时刻，读到的是昨天的
          成绩」）

    只有布尔 false 才算明确否定；字符串 "false"、0 之类脏值一律按 0 处理，
    与 runner 的宽容口径保持一致。存成整数是为了和既有账本行兼容（旧行只有
    0/1，读出来自然按「没表态」/「已签」解释）。
    """
    if getattr(rr, "signed_today_known", False) and not getattr(rr, "signed_today", True):
        return -1
    return 1 if getattr(rr, "signed_today", False) else 0


class Ledger:
    def __init__(self, db_path: Path):
        db_path = Path(db_path)
        # 兼容旧配置里的 data/ledger.db：数据文件已是 JSON，仅沿用配置节名
        if db_path.suffix.lower() == ".db":
            db_path = db_path.with_suffix(".json")
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._log: list[dict] = []   # checkin_log 行（含自增 id）
        self._snap: list[dict] = []  # expiring_snapshot 行（含自增 id）
        self._next_id = 1
        self._load()

    # ---------- 装载：JSON 为主，旧 SQLite 迁移 + 冲突副本合并 ----------

    def _load(self) -> None:
        doc = None
        if self.db_path.is_file():
            try:
                with open(self.db_path, encoding="utf-8") as f:
                    doc = json.load(f)
            except (OSError, ValueError):
                # 主文件损坏（同步半途落地等）：留档排查，冲突副本/旧库还能救回数据
                keep = self.db_path.with_name(
                    f"{self.db_path.name}.corrupt-{time.strftime('%Y%m%d%H%M%S')}")
                try:
                    os.replace(self.db_path, keep)
                except OSError:
                    pass
                doc = None

        if isinstance(doc, dict):
            self._log = [r for r in doc.get("checkin_log", []) if isinstance(r, dict)]
            self._snap = [r for r in doc.get("expiring_snapshot", []) if isinstance(r, dict)]
            dirty = False
        else:
            # 主文件缺失（首次建立/尚未同步过来）或损坏留档：确保落一份主文件
            self._log, self._snap = [], []
            dirty = True
            self._import_legacy_sqlite()

        dirty = self._merge_conflict_copies() or dirty
        self._renumber_and_save(force=dirty)

    def _import_legacy_sqlite(self) -> None:
        """从旧 SQLite（含其同步冲突副本）导入历史数据，导完改名 *.migrated。"""
        stem = self.db_path.stem
        legacy = [p for p in self.db_path.parent.iterdir()
                  if p.is_file() and p.suffix.lower() == ".db"
                  and (p.stem == stem or _CONFLICT_MARK in p.stem)]
        for path in legacy:
            try:
                conn = sqlite3.connect(str(path))
                try:
                    conn.row_factory = sqlite3.Row
                    # 两张表独立读取：旧版本库可能缺其中一张，缺表不拖累另一张的数据
                    try:
                        log_rows = conn.execute("SELECT * FROM checkin_log").fetchall()
                    except sqlite3.Error:
                        log_rows = []
                    try:
                        snap_rows = conn.execute("SELECT * FROM expiring_snapshot").fetchall()
                    except sqlite3.Error:
                        snap_rows = []
                finally:
                    conn.close()
            except Exception:
                continue  # 库损坏/被占用：跳过该库，其余照常
            known = {_log_key(r) for r in self._log}
            known_snap = {_snap_key(r) for r in self._snap}
            for r in log_rows:
                row = {k: r[k] for k in ("ts", "platform", "script", "result", "credit",
                                         "total_credits", "streak_days", "report",
                                         "raw", "trigger", "parse_ok")}
                if _log_key(row) in known:
                    continue
                known.add(_log_key(row))
                self._log.append(row)
            for r in snap_rows:
                row = {k: r[k] for k in ("platform", "date", "amount", "source", "fetched_at")}
                if _snap_key(row) in known_snap:
                    continue
                known_snap.add(_snap_key(row))
                self._snap.append(row)
            try:
                os.replace(str(path), str(path) + ".migrated")
            except OSError:
                pass  # 被同步盘占用等：留待下次启动重试，内容级判重保证不重复导入

    def _merge_conflict_copies(self) -> bool:
        """并集合并同步盘产生的 JSON 冲突副本后清理副本文件；有新行并入返回 True。"""
        conflicts = [p for p in self.db_path.parent.iterdir()
                     if p.is_file() and p.suffix.lower() == ".json"
                     and p.stem.startswith(self.db_path.stem) and _CONFLICT_MARK in p.stem]
        if not conflicts:
            return False
        merged = False
        known_log = {_log_key(r) for r in self._log}
        known_snap = {_snap_key(r) for r in self._snap}
        for path in conflicts:
            try:
                with open(path, encoding="utf-8") as f:
                    doc = json.load(f)
            except (OSError, ValueError):
                continue
            if not isinstance(doc, dict):
                continue
            for row in doc.get("checkin_log", []):
                if isinstance(row, dict) and _log_key(row) not in known_log:
                    known_log.add(_log_key(row))
                    self._log.append(row)
                    merged = True
            for row in doc.get("expiring_snapshot", []):
                if isinstance(row, dict) and _snap_key(row) not in known_snap:
                    known_snap.add(_snap_key(row))
                    self._snap.append(row)
                    merged = True
            try:
                os.remove(path)
            except OSError:
                pass  # 合并已生效，文件删不掉时留给下次启动重试
        return merged

    def _renumber_and_save(self, force: bool = False) -> None:
        """按时间重建自增 id（合并进来的行可能乱序）；force 或 id 不一致时落盘。"""
        with self._lock:
            self._log.sort(key=lambda r: (r.get("ts") or "", r.get("id") or 0))
            self._snap.sort(key=lambda r: (r.get("date") or "", r.get("platform") or "",
                                           r.get("fetched_at") or ""))
            # 存量行补live 标记：默认视为有效（活跃），下次刷新会按接口回报重算
            if any("live" not in r for r in self._snap):
                for r in self._snap:
                    r.setdefault("live", 1)
                force = True
            n_log, n_snap = len(self._log), len(self._snap)
            ids_ok = ([r.get("id") for r in self._log] == list(range(1, n_log + 1))
                      and [r.get("id") for r in self._snap]
                      == list(range(n_log + 1, n_log + n_snap + 1)))
            if ids_ok and not force:
                self._next_id = n_log + n_snap + 1
                return  # 内容与 id 均未变：不落盘，避免每次启动都触发同步盘上传
            self._next_id = 1
            for row in self._log:
                row["id"] = self._next_id
                self._next_id += 1
            for row in self._snap:
                row["id"] = self._next_id
                self._next_id += 1
            self._save_locked()

    # ---------- 落盘 ----------

    def _save_locked(self) -> None:
        doc = {
            "version": LEDGER_VERSION,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "checkin_log": self._log,
            "expiring_snapshot": self._snap,
        }
        _atomic_write_text(self.db_path,
                           json.dumps(doc, ensure_ascii=False, indent=1))

    # ---------- 写入 ----------

    def insert_run(self, rr, trigger: str = "manual") -> None:
        with self._lock:
            row = {
                "id": self._next_id,
                "ts": datetime.now().isoformat(timespec="seconds"),
                "platform": rr.platform,
                "script": rr.script,
                "result": rr.result,
                "credit": rr.today_credit,
                "total_credits": rr.total_credits,
                "streak_days": rr.streak_days,
                "report": rr.report,
                "signed_today": _signed_flag(rr),
                "raw": rr.raw,
                "trigger": trigger,
                "parse_ok": 1 if rr.parse_ok else 0,
            }
            self._next_id += 1
            self._log.append(row)
            # 状态核查只回答「今天签没签」、不回报余额：让它参与快照重建会把今天
            # 所有临期行标成失效（详见 _update_snapshots_locked）。但它若真领到了
            # 积分（脚本没实现 --status、忽略参数照常签到），那就必须按真实签到
            # 记账、连同 expiring 一起入账——丢一笔真领的分比多跑一次查询糟得多
            if trigger != PROBE_TRIGGER or rr.result == "CLAIMED":
                self._update_snapshots_locked(rr)
            self._save_locked()

    def _update_snapshots_locked(self, rr) -> None:
        """快照更新口径：

        - 成功回报（或带 expiring 数据）才动快照，失败运行不清旧数据
        - 未来行（date > 今天）每次全量重建，日历如实反映最新回报
        - 今天行按来源替换：本次回报里出现的 source，先删掉该平台当天的同
          source 旧行再写入（AutoClaw/WorkBuddy 等「余额口径」平台当日扣减
          后余额变小，直接追加会同源双计、越用越多）
        - 今天行里本次没再回报的 source：不删，而是打 live=0 标记。原因有两点：
          ① 接口当天不再回报某笔，通常是这笔积分当天被消耗掉或已过期待清理，
             继续挂在今天的格子里会让「当日即将过期」比平台客户端显示的多，
             与真实余额对不上；② 直接删掉，这笔金额就永远无法追溯。打标记
             让日历显示等于实时、消耗流水留底；「过去 30 天已作废」只统计
             live=1（到期日结束时仍持有）的行，花掉的不算作废
        - 更早的行保留为作废历史：重复回报按 (date, amount, source) 去重
        - 改名过渡：旧名 <base> 只清今天及以后避免双计，过期历史行保留供作废统计
        """
        if not rr.expiring and not (rr.parse_ok and rr.result in ("CLAIMED", "ALREADY")):
            return
        # 先按 (date, source) 合并同一来源的多笔权益包，避免被下面的三元组判重吃掉
        expiring = aggregate_expiring(rr.expiring)
        now = datetime.now().isoformat(timespec="seconds")
        today = datetime.now().strftime("%Y-%m-%d")
        base = rr.platform.removesuffix("_credits")
        if base != rr.platform:
            self._snap = [r for r in self._snap
                          if not (r["platform"] == base and r["date"] >= today)]
        self._snap = [r for r in self._snap
                      if not (r["platform"] == rr.platform and r["date"] > today)]
        today_sources = {e["source"] for e in expiring if e["date"] == today}
        if today_sources:
            self._snap = [r for r in self._snap
                          if not (r["platform"] == rr.platform and r["date"] == today
                                  and r["source"] in today_sources)]
        # 本次未回报的今天行：保留数据、仅标记失效（供 void30 统计）
        for r in self._snap:
            if r["platform"] == rr.platform and r["date"] == today \
                    and r["source"] not in today_sources:
                r["live"] = 0
        existing = {(r["date"], r["amount"], r["source"])
                    for r in self._snap if r["platform"] == rr.platform}
        for e in expiring:
            key = (e["date"], e["amount"], e["source"])
            if key in existing:
                continue
            self._snap.append({"id": self._next_id, "platform": rr.platform,
                               "date": key[0], "amount": key[1], "source": key[2],
                               "live": 1, "fetched_at": now})
            self._next_id += 1

    def cleanup(self, retention_days: int, snapshot_retention_days: int = 31) -> None:
        with self._lock:
            cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
            cutoff_d = (datetime.now() - timedelta(days=snapshot_retention_days)).strftime("%Y-%m-%d")
            log_keep = [r for r in self._log if r["ts"] >= cutoff]
            snap_keep = [r for r in self._snap if r["date"] >= cutoff_d]
            if len(log_keep) != len(self._log) or len(snap_keep) != len(self._snap):
                self._log, self._snap = log_keep, snap_keep
                self._save_locked()

    def clear_logs(self) -> int:
        """清空运行日志（checkin_log），返回删除条数。不触碰 expiring_snapshot。"""
        with self._lock:
            deleted = len(self._log)
            self._log = []
            self._save_locked()
            return deleted

    # ---------- 查询（行均为 dict，支持 row["col"] 访问） ----------

    def latest_per_platform(self, exclude_probe: bool = False) -> dict[str, dict]:
        """每个平台最近一条日志（即当前状态）。

        exclude_probe：跳过状态核查（probe）留下的记录。核查只回答「今天签没签」、
        不回报余额，拿它当余额/连续天数来源会显示成 0。唯一的例外是核查链路上真
        领到积分的 CLAIMED 行——那是实打实的签到（脚本没实现 --status 时会出现），
        它带着真实余额，排除掉反而丢数据。
        """
        with self._lock:
            latest: dict[str, dict] = {}
            for row in self._log:   # id 单调递增，后写即最新
                if (exclude_probe and row["trigger"] == PROBE_TRIGGER
                        and row["result"] != "CLAIMED"):
                    continue
                latest[row["platform"]] = row
            return latest

    def today_runs(self, today: str) -> list[dict]:
        with self._lock:
            rows = [r for r in self._log if r["ts"].startswith(today)]
            rows.sort(key=lambda r: r["ts"])
            return rows

    def today_checkin_rows(self, today: str) -> dict[str, dict]:
        """今天真正发生签到的平台 → 该平台今天最后一条签到记录（含失败）。

        判定「今天有没有签到这一动作」，看两类证据：
        - schedule/manual/retry 触发的运行（真实签到动作，含失败要示警）；
        - 任何触发方式下的 CLAIMED——体检首跑也可能真实签到（_health_check 对
          CLAIMED 连同 expiring 一起入账），这是真实领到了积分，应算已签；
        - 任何触发方式下 signed_today=1 的记录——脚本明确声明「平台侧确认今天
          已签」。用户在平台客户端/别的签到工具先签了，面板自己没跑过签到，但
          平台已经给出今天的成绩，这就是今天的签到事实。
        - 例外是 signed_today=-1（脚本明确说「今天确实没签」）：它**否决**同一
          平台今天更早的签到动作。典型场景是 Qoder 每天 10:00 才开窗，10 点前
          手动签到只能读到昨天那一窗的 CLAIMED——不算失败，但今天这 100
          credits 没到账，卡片不该显示「今日已完成」把按钮置灰。
        排除的是体检/积分刷新的 ALREADY（signed_today 未置位）：那只是「脚本
        读到的账号现状」，可能是昨天签的、也可能只是查了下余额，不代表今天有
        签到动作。计入它们正是「早上打开面板四个平台全显示已签到」故障的根源。
        """
        with self._lock:
            latest: dict[str, dict] = {}
            for row in self._log:
                if not row["ts"].startswith(today):
                    continue
                if (row["trigger"] in CHECKIN_TRIGGERS
                        or row["result"] == "CLAIMED"
                        or int(row.get("signed_today") or 0) != 0):
                    latest[row["platform"]] = row
            # 最新一条明确说「今天没签」→ 该平台今天不算已签。
            # 位置口径很重要：先核查（-1）后手动签到（也带 -1）应保持未签；
            # 反过来 10:00 开窗后核查给出 +1，则要能把先前的 -1 翻过来
            return {name: row for name, row in latest.items()
                    if int(row.get("signed_today") or 0) != -1}

    def platforms_answered_today(self, today: str, deny_ttl_minutes: int = 5) -> set[str]:
        """今天已由状态核查给出过结论的平台名（含「今天确实没签」的否定结论）。

        与 today_checkin_rows 的区别：signed_today=-1 也算「问过了」——不然
        每天 10 点前的每一次页面刷新都会为同一个「还没开窗」的平台重复打上游。

        但否定结论必须带保鲜期（deny_ttl_minutes）：Qoder 08:27 的「还没开窗」
        到 10:30 就已经过期，若一直锁着，之后打开面板永远不再复查，卡片会一直
        停在未签到（用户的签到动作虽能救回来，但漏了手动点一次就白等一天）。
        肯定结论（+1）不需要保鲜期：今天已领就是已领。
        """
        cutoff = (datetime.now() - timedelta(minutes=max(0, deny_ttl_minutes))
                  ).isoformat(timespec="seconds")
        with self._lock:
            latest: dict[str, dict] = {}
            for row in self._log:
                if not row["ts"].startswith(today):
                    continue
                if row.get("trigger") != PROBE_TRIGGER:
                    continue
                latest[row["platform"]] = row
        answered = set()
        for name, row in latest.items():
            flag = int(row.get("signed_today") or 0)
            if flag != -1 or row["ts"] >= cutoff:
                answered.add(name)
        return answered

    def has_schedule_run_today(self, today: str) -> bool:
        """今天是否已有调度触发的签到记录（供调度器判重）。"""
        with self._lock:
            return any(r["trigger"] == "schedule" and r["ts"].startswith(today)
                       for r in self._log)

    def retryable_failures_today(self, today: str) -> list[str]:
        """今天最后一次运行（任意触发方式）仍为凭据类失败的平台名。

        每平台取当日最后一次运行的结果：手动签到成功同样会终止补签，
        避免调度器对已就绪的平台空跑。
        """
        with self._lock:
            latest: dict[str, str] = {}
            for row in self._log:
                if row["ts"].startswith(today):
                    latest[row["platform"]] = row["result"]
        return [p for p, r in latest.items() if r in RETRYABLE_RESULTS]

    @staticmethod
    def _log_match(row: dict, filter_name: str) -> bool:
        if filter_name == "success":
            return row["result"] in ("CLAIMED", "ALREADY")
        if filter_name == "claimed":
            return row["result"] == "CLAIMED"
        if filter_name == "failed":
            return row["result"] in ("NO_SESSION", "NO_AUTH", "NETWORK", "ERROR")
        return True

    def logs(self, filter_name: str = "all", limit: int = 200, offset: int = 0) -> list[dict]:
        with self._lock:
            rows = [r for r in self._log if self._log_match(r, filter_name)]
            rows.sort(key=lambda r: r["id"], reverse=True)
            return rows[offset:offset + limit]

    def logs_count(self, filter_name: str = "all") -> int:
        """满足筛选条件的日志总条数（供分页）。"""
        with self._lock:
            return sum(1 for r in self._log if self._log_match(r, filter_name))

    def logs_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["时间", "平台", "脚本", "状态", "积分", "累计积分", "连续天数",
                    "返回明细", "触发"])
        for r in self.logs("all", limit=10000):
            w.writerow([r["ts"], r["platform"], r["script"], r["result"],
                        r["credit"] if r["credit"] is not None else "—",
                        r["total_credits"] if r["total_credits"] is not None else "—",
                        r["streak_days"] if r["streak_days"] is not None else "—",
                        r["report"], self._trigger_label(r)])
        return buf.getvalue()

    @staticmethod
    def _trigger_label(row: dict) -> str:
        """触发列展示文案：隐藏积分源（*_credits）只查余额不签到，定时队列里
        与签到脚本同记 trigger=schedule 无法区分，按平台名后缀先行分流；
        与 web/app.js 运行日志表格的口径保持一致。"""
        if row["platform"].endswith("_credits"):
            return "积分查询"
        trigger = row.get("trigger", "")
        if trigger == "schedule":
            return "定时签到"
        if trigger == "retry":
            return "自动补签"
        if trigger == "health":
            return "体检"
        if trigger == "refresh":
            return "积分刷新"
        if trigger == "probe":
            return "状态核查"
        return "手动签到"

    def expiring_snapshots(self, live_only: bool = False) -> list[dict]:
        """临期快照。live_only=True 时只返回「今天及以后、且最近一次回报仍存在」
        的行——过期日历用它，保证当日/未来格子的数字与平台客户端实时一致。"""
        with self._lock:
            cutoff = (datetime.now() - timedelta(days=STALE_SNAPSHOT_DAYS)).isoformat()
            today = datetime.now().strftime("%Y-%m-%d")
            fresh_platforms = {r["platform"] for r in self._snap if r["fetched_at"] >= cutoff}
            dedup: dict[tuple, dict] = {}
            for row in self._snap:
                key = _snap_key(row)
                cur = dedup.get(key)
                if cur is None or row["fetched_at"] >= cur["fetched_at"]:
                    dedup[key] = row
            rows = [r for r in dedup.values() if r["platform"] in fresh_platforms]
            if live_only:
                rows = [r for r in rows
                        if r["date"] >= today and int(r.get("live", 1) or 0) == 1]
        rows.sort(key=lambda r: (r["date"], r["platform"], r["amount"], r["source"]))
        return rows
