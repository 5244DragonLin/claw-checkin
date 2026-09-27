"""SQLite 账本：checkin_log 签到日志 + expiring_snapshot 临期积分快照

面板只读不写：数据来源 = 各脚本 stdout 的一行 JSON + ledger.db 的 checkin_log 表。
"""

from __future__ import annotations

import csv
import io
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS checkin_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,              -- 触发时间 ISO 格式
    platform TEXT NOT NULL,
    script TEXT NOT NULL,
    result TEXT NOT NULL,          -- CLAIMED/ALREADY/NO_SESSION/NO_AUTH/NETWORK/ERROR
    credit INTEGER,                -- 今日获得积分
    total_credits INTEGER,
    streak_days INTEGER,
    report TEXT DEFAULT '',
    raw TEXT DEFAULT '',           -- stdout 原文
    trigger TEXT DEFAULT 'manual', -- manual / schedule
    parse_ok INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_log_ts ON checkin_log(ts);
CREATE INDEX IF NOT EXISTS idx_log_platform ON checkin_log(platform, ts);

CREATE TABLE IF NOT EXISTS expiring_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    date TEXT NOT NULL,            -- 过期日 YYYY-MM-DD
    amount INTEGER NOT NULL,
    source TEXT DEFAULT '',
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exp_platform ON expiring_snapshot(platform, date);
"""


class Ledger:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        # 单连接 + 可重入锁：FastAPI 线程池、调度线程、体检双跑共用本对象，
        # 所有读写一律经 _lock 串行化，避免 SQLite "database is locked"
        self._lock = threading.RLock()

    # ---------- 写入 ----------

    def insert_run(self, rr, trigger: str = "manual") -> None:
        with self._lock:
            self._insert_run_locked(rr, trigger=trigger)

    def _insert_run_locked(self, rr, trigger: str = "manual") -> None:
        self._conn.execute(
            "INSERT INTO checkin_log (ts, platform, script, result, credit, total_credits,"
            " streak_days, report, raw, trigger, parse_ok)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                datetime.now().isoformat(timespec="seconds"),
                rr.platform, rr.script, rr.result,
                rr.today_credit, rr.total_credits, rr.streak_days,
                rr.report, rr.raw, trigger, 1 if rr.parse_ok else 0,
            ),
        )
        if rr.expiring:
            now = datetime.now().isoformat(timespec="seconds")
            # 覆盖式更新：清除本平台快照；若是积分源（<base>_credits），
            # 连同归属组旧名 <base> 的历史快照一并清除，避免过渡期双计
            base = rr.platform.removesuffix("_credits")
            self._conn.execute(
                "DELETE FROM expiring_snapshot WHERE platform IN (?, ?)",
                (rr.platform, base),
            )
            for e in rr.expiring:
                self._conn.execute(
                    "INSERT INTO expiring_snapshot (platform, date, amount, source, fetched_at)"
                    " VALUES (?,?,?,?,?)",
                    (rr.platform, str(e["date"]), int(e.get("amount", 0)),
                     str(e.get("source", "")), now),
                )
        self._conn.commit()

    def cleanup(self, retention_days: int, snapshot_retention_days: int = 31) -> None:
        with self._lock:
            cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
            self._conn.execute("DELETE FROM checkin_log WHERE ts < ?", (cutoff,))
            cutoff_d = (datetime.now() - timedelta(days=snapshot_retention_days)).strftime("%Y-%m-%d")
            self._conn.execute("DELETE FROM expiring_snapshot WHERE date < ?", (cutoff_d,))
            self._conn.commit()

    # ---------- 查询 ----------

    def latest_per_platform(self) -> dict[str, sqlite3.Row]:
        """每个平台最近一条日志（即当前状态）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT l.* FROM checkin_log l"
                " JOIN (SELECT platform, MAX(id) AS mid FROM checkin_log GROUP BY platform) m"
                " ON l.platform = m.platform AND l.id = m.mid"
            ).fetchall()
        return {r["platform"]: r for r in rows}

    def today_runs(self, today: str) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM checkin_log WHERE ts LIKE ? ORDER BY ts", (f"{today}%",)
            ).fetchall()

    def has_schedule_run_today(self, today: str) -> bool:
        """今天是否已有调度触发的签到记录（供调度器判重，替代跨模块访问内部连接）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM checkin_log"
                " WHERE trigger='schedule' AND ts LIKE ?",
                (f"{today}%",),
            ).fetchone()
        return (row["n"] or 0) > 0

    # 凭据未就绪/网络抖动类失败：调度器当日自动补跑（ERROR 等脚本缺陷不重试）
    RETRYABLE_RESULTS = ("NO_AUTH", "NETWORK")

    def retryable_failures_today(self, today: str) -> list[str]:
        """今天最后一次运行（任意触发方式）仍为凭据类失败的平台名。

        每平台取当日最后一次运行的结果：手动签到成功同样会终止补签，
        避免调度器对已就绪的平台空跑。
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT platform, result FROM checkin_log"
                " WHERE ts LIKE ? ORDER BY id",
                (f"{today}%",),
            ).fetchall()
        latest: dict[str, str] = {}
        for row in rows:
            latest[row["platform"]] = row["result"]
        return [p for p, r in latest.items() if r in self.RETRYABLE_RESULTS]

    @staticmethod
    def _log_cond(filter_name: str) -> tuple[str, list]:
        """筛选条件：返回 (WHERE 子句, 参数列表)。"""
        if filter_name == "success":      # CLAIMED + ALREADY
            return " WHERE result IN ('CLAIMED','ALREADY')", []
        if filter_name == "claimed":
            return " WHERE result = 'CLAIMED'", []
        if filter_name == "failed":       # 四种失败态
            return " WHERE result IN ('NO_SESSION','NO_AUTH','NETWORK','ERROR')", []
        return "", []

    def logs(self, filter_name: str = "all", limit: int = 200, offset: int = 0) -> list[sqlite3.Row]:
        cond, _ = self._log_cond(filter_name)
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM checkin_log" + cond + " ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return rows

    def logs_count(self, filter_name: str = "all") -> int:
        """满足筛选条件的日志总条数（供分页）。"""
        cond, _ = self._log_cond(filter_name)
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM checkin_log" + cond
            ).fetchone()
        return row["n"]

    def logs_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["时间", "平台", "脚本", "状态", "积分", "累计积分", "连续天数", "返回明细", "触发方式"])
        for r in self.logs("all", limit=10000):
            w.writerow([r["ts"], r["platform"], r["script"], r["result"],
                        r["credit"] if r["credit"] is not None else "—",
                        r["total_credits"] if r["total_credits"] is not None else "—",
                        r["streak_days"] if r["streak_days"] is not None else "—",
                        r["report"], r["trigger"]])
        return buf.getvalue()

    # 快照保鲜期（天）：平台最后一次回报 expiring 距今超过该天数，其全部快照
    # 视为过期数据，不再计入过期统计——脚本连续失败或已移除时，旧快照不应
    # 长期占据过期日历（宁可显示为无数据，也不显示作废数据）。
    STALE_SNAPSHOT_DAYS = 3

    def expiring_snapshots(self) -> list[sqlite3.Row]:
        with self._lock:
            cutoff = (datetime.now() - timedelta(days=self.STALE_SNAPSHOT_DAYS)).isoformat()
            fresh = self._conn.execute(
                "SELECT platform FROM expiring_snapshot"
                " GROUP BY platform HAVING MAX(fetched_at) >= ?",
                (cutoff,),
            ).fetchall()
            fresh_platforms = {r["platform"] for r in fresh}
            rows = self._conn.execute(
                "SELECT platform, date, amount, source, MAX(fetched_at) AS fetched_at"
                " FROM expiring_snapshot GROUP BY platform, date, amount, source"
                " ORDER BY date"
            ).fetchall()
        return [r for r in rows if r["platform"] in fresh_platforms]


