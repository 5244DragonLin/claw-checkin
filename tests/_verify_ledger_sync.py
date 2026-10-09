#!/usr/bin/env python3
"""验证 JSON 版 Ledger：语义与旧 SQLite 版一致 + 迁移 + 同步冲突合并。"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sqlite3 as s3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from claw_checkin.ledger import Ledger  # noqa: E402

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def rr(platform="workbuddy", result="CLAIMED", credit=100, total=700, streak=7,
       report="成功领取 100 积分", raw="raw-line", parse_ok=True, expiring=None,
       script=None):
    return SimpleNamespace(
        platform=platform, script=script or (platform + ".py"), result=result,
        today_credit=credit, total_credits=total, streak_days=streak,
        report=report, raw=raw, parse_ok=parse_ok, expiring=expiring or [])


def test_basic_semantics(root: Path) -> None:
    print("[1] 基本语义：插入 / 查询 / 过滤 / 清理")
    db = root / "ledger.json"
    led = Ledger(db)
    led.insert_run(rr(), trigger="manual")
    led.insert_run(rr(result="ALREADY", credit=0, report="今日已签到"), trigger="schedule")
    led.insert_run(rr(platform="autoclaw", result="NO_AUTH", credit=0, parse_ok=False),
                   trigger="schedule")

    latest = led.latest_per_platform()
    check("latest_per_platform 每平台一条", set(latest) == {"workbuddy", "autoclaw"})
    check("latest 取最新一条", latest["workbuddy"]["result"] == "ALREADY")
    check("today_runs", len(led.today_runs(latest["workbuddy"]["ts"][:10])) == 3)
    check("has_schedule_run_today", led.has_schedule_run_today("2999-01-01") is False)
    today = latest["workbuddy"]["ts"][:10]
    check("has_schedule_run_today 命中", led.has_schedule_run_today(today) is True)
    check("retryable_failures_today",
          led.retryable_failures_today(today) == ["autoclaw"])
    check("logs failed 过滤", [r["platform"] for r in led.logs("failed")] == ["autoclaw"])
    check("logs claimed 过滤", [r["result"] for r in led.logs("claimed")] == ["CLAIMED"])
    check("logs_count", led.logs_count("success") == 2)
    rows = led.logs("all", limit=1, offset=1)
    check("logs 分页倒序", rows[0]["result"] == "ALREADY")
    check("logs_csv 表头", led.logs_csv().splitlines()[0].startswith("时间"))
    check("行是 dict 且可下标", rows[0]["platform"] in ("workbuddy", "autoclaw"))

    # 快照：带 expiring 的成功运行写入；未来行重建、今天行保留、同名去重
    exp = [{"date": "2999-01-01", "amount": 50, "source": "签到奖励"},
           {"date": "2999-01-02", "amount": 30, "source": "活动"}]
    led.insert_run(rr(platform="autoclaw", result="CLAIMED", expiring=exp))
    snap = led.expiring_snapshots()
    check("expiring 写入快照", {(s["date"], s["amount"]) for s in snap} ==
          {("2999-01-01", 50), ("2999-01-02", 30)})
    # 未来行全量重建：第二次回报只剩一笔 → 旧的未来行消失
    led.insert_run(rr(platform="autoclaw", result="CLAIMED",
                      expiring=[{"date": "2999-01-01", "amount": 50, "source": "签到奖励"}]))
    snap = led.expiring_snapshots()
    check("未来行全量重建", [(s["date"], s["amount"]) for s in snap] == [("2999-01-01", 50)])
    # 重复回报按 (date, amount, source) 去重
    led.insert_run(rr(platform="autoclaw", result="ALREADY", credit=0,
                      expiring=[{"date": "2999-01-01", "amount": 50, "source": "签到奖励"}]))
    check("重复回报去重", len(led.expiring_snapshots()) == 1)
    # 已存在的历史行（今天及更早）被再次回报时同样不重复插入
    yesterday = (__import__("datetime").date.today()
                 - __import__("datetime").timedelta(days=1)).isoformat()
    led.insert_run(rr(platform="autoclaw", result="ALREADY", credit=0,
                      expiring=[{"date": yesterday, "amount": 40, "source": "签到奖励"}]))
    led.insert_run(rr(platform="autoclaw", result="ALREADY", credit=0,
                      expiring=[{"date": yesterday, "amount": 40, "source": "签到奖励"}]))
    n_hist = sum(1 for s in led.expiring_snapshots()
                 if s["platform"] == "autoclaw" and s["date"] == yesterday)
    check("历史行重复回报去重", n_hist == 1)
    # 失败运行不动快照
    led.insert_run(rr(platform="autoclaw", result="ERROR", parse_ok=False))
    check("失败运行不清快照", len(led.expiring_snapshots()) == 1)
    # 隐藏积分源：平台名 _credits 后缀，清 base 未来行避免双计
    led.insert_run(rr(platform="workbuddy_credits", result="ALREADY", credit=0,
                      expiring=[{"date": "2999-02-01", "amount": 500, "source": "权益包"}]))
    snap = led.expiring_snapshots()
    check("_credits 归属独立", any(s["platform"] == "workbuddy_credits" for s in snap))
    led.insert_run(rr(platform="workbuddy_credits", result="ALREADY", credit=0,
                      expiring=[{"date": "2999-02-02", "amount": 400, "source": "权益包"}]))
    dates = {s["date"] for s in led.expiring_snapshots()
             if s["platform"] == "workbuddy_credits"}
    check("base 未来行随积分源重建", dates == {"2999-02-02"})

    led.clear_logs()
    check("clear_logs 只清日志", led.logs_count("all") == 0
          and len(led.expiring_snapshots()) >= 1)

    led.cleanup(retention_days=90, snapshot_retention_days=31)
    check("cleanup 保留未来快照", len(led.expiring_snapshots()) >= 1)


def test_legacy_migration(root: Path) -> None:
    print("[2] 旧 SQLite 迁移（含冲突副本库）")
    data = root / "mig"
    data.mkdir(exist_ok=True)
    legacy = data / "ledger.db"
    conn = s3.connect(str(legacy))
    conn.executescript("""
    CREATE TABLE checkin_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
        platform TEXT NOT NULL, script TEXT NOT NULL, result TEXT NOT NULL,
        credit INTEGER, total_credits INTEGER, streak_days INTEGER,
        report TEXT DEFAULT '', raw TEXT DEFAULT '', trigger TEXT DEFAULT 'manual',
        parse_ok INTEGER DEFAULT 0);
    CREATE TABLE expiring_snapshot (id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL, date TEXT NOT NULL, amount INTEGER NOT NULL,
        source TEXT DEFAULT '', fetched_at TEXT NOT NULL);
    """)
    conn.execute("INSERT INTO checkin_log (ts,platform,script,result,credit,trigger,parse_ok)"
                 " VALUES ('2026-10-01T08:00:00','workbuddy','workbuddy.py','CLAIMED',100,'manual',1)")
    conn.execute("INSERT INTO expiring_snapshot (platform,date,amount,source,fetched_at)"
                 f" VALUES ('workbuddy','2999-03-01',200,'老包',"
                 f"'{__import__('datetime').datetime.now().isoformat(timespec='seconds')}')")
    conn.commit()
    conn.close()
    conflict = data / "ledger_冲突文件_yanghaolin_20261006164841.db"
    shutil.copy(legacy, conflict)
    conn = s3.connect(str(conflict))
    conn.execute("INSERT INTO checkin_log (ts,platform,script,result,credit,trigger,parse_ok)"
                 " VALUES ('2026-10-06T16:40:00','autoclaw','autoclaw.py','ALREADY',0,'schedule',1)")
    conn.commit()
    conn.close()

    led = Ledger(data / "ledger.json")
    check("迁移：主库行进入", any(r["ts"] == "2026-10-01T08:00:00" for r in led.logs("all")))
    check("迁移：冲突副本库行进入", any(r["ts"] == "2026-10-06T16:40:00" for r in led.logs("all")))
    # 注：expiring_snapshots 有 3 天保鲜期过滤， fetched_at 太旧会被视为过期数据剔除
    fresh_snap = any(s["amount"] == 200 and s["fetched_at"] >= "2026-10-05"
                     for s in led.expiring_snapshots())
    check("迁移：快照进入（fetched_at 新鲜时）", fresh_snap)
    check("迁移：旧库改名 .migrated", (data / "ledger.db.migrated").is_file())
    check("迁移：冲突库改名", not conflict.is_file()
          and Path(str(conflict) + ".migrated").is_file())
    # 再开一次：不应重复导入
    led2 = Ledger(data / "ledger.json")
    check("迁移幂等", led2.logs_count("all") == 2)
    again = list(data.glob("*.db"))
    check("不再有活动 .db 文件", again == [])


def test_conflict_merge(root: Path) -> None:
    print("[3] 同步盘冲突副本合并（JSON）")
    data = root / "conf"
    data.mkdir(exist_ok=True)
    main = data / "ledger.json"

    # A 机写入两行
    a = Ledger(main)
    a.insert_run(rr(platform="workbuddy", report="A-1"))
    a.insert_run(rr(platform="autoclaw", result="ALREADY", credit=0, report="A-2"))
    # B 机基于更早状态写一行，同步盘把 B 的版本留成冲突副本
    b_doc = json.loads(main.read_text(encoding="utf-8"))
    b_doc["checkin_log"] = b_doc["checkin_log"][:1]
    b_doc["checkin_log"].append({
        "id": 99, "ts": "2026-10-07T09:00:00", "platform": "workbuddy_credits",
        "script": "workbuddy_credits.py", "result": "ALREADY", "credit": 0,
        "total_credits": 7184, "streak_days": 0, "report": "B-新增",
        "raw": "", "trigger": "manual", "parse_ok": 1})
    b_doc["expiring_snapshot"] = b_doc["expiring_snapshot"] + [{
        "id": 100, "platform": "workbuddy_credits", "date": "2999-04-01",
        "amount": 300, "source": "B包", "fetched_at": "2026-10-07T09:00:00"}]
    conflict = data / "ledger_冲突文件_yanghaolin_20261007101010.json"
    conflict.write_text(json.dumps(b_doc, ensure_ascii=False), encoding="utf-8")

    led = Ledger(main)  # 同一台 A 机上重新打开：应合并 B 的独有行并删除副本
    reports = [r["report"] for r in led.logs("all")]
    check("合并：A 独有行保留", "A-2" in reports)
    check("合并：B 独有行进入", "B-新增" in reports)
    check("合并：共同行不重复", reports.count("A-1") == 1)
    check("合并：B 快照进入", any(s["amount"] == 300 for s in led.expiring_snapshots()))
    check("合并：副本已清理", not conflict.is_file())
    ids = [r["id"] for r in led.logs("all", limit=10000)]
    check("合并后 id 唯一", len(ids) == len(set(ids)))

    led2 = Ledger(main)
    check("重开不丢行", led2.logs_count("all") == len(reports))


def test_corrupt_recovery(root: Path) -> None:
    print("[4] 主文件损坏时的自愈")
    data = root / "corrupt"
    data.mkdir(exist_ok=True)
    main = data / "ledger.json"
    led = Ledger(main)
    led.insert_run(rr(platform="workbuddy", report="存活行"))
    good = json.loads(main.read_text(encoding="utf-8"))
    main.write_text('{"version":1, "checkin_log": [ 坏掉的 JSON', encoding="utf-8")
    led2 = Ledger(main)
    check("损坏主文件被留档", list(data.glob("*.corrupt-*")) != [])
    check("损坏后账本为空不崩", led2.logs_count("all") == 0)
    # 模拟同步盘随后把好版本作为冲突副本送过来
    conflict = data / "ledger_冲突文件_other_20261008000000.json"
    conflict.write_text(json.dumps(good, ensure_ascii=False), encoding="utf-8")
    led3 = Ledger(main)
    check("冲突副本救回数据", any(r["report"] == "存活行" for r in led3.logs("all")))


def test_config_compat(root: Path) -> None:
    print("[5] 旧配置 db: data/ledger.db 自动指向 JSON")
    data = root / "cfg"
    data.mkdir(exist_ok=True)
    old = data / "ledger.db"
    conn = s3.connect(str(old))
    conn.execute("CREATE TABLE checkin_log (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                 " ts TEXT NOT NULL, platform TEXT NOT NULL, script TEXT NOT NULL,"
                 " result TEXT NOT NULL, credit INTEGER, total_credits INTEGER,"
                 " streak_days INTEGER, report TEXT DEFAULT '', raw TEXT DEFAULT '',"
                 " trigger TEXT DEFAULT 'manual', parse_ok INTEGER DEFAULT 0)")
    conn.execute("INSERT INTO checkin_log (ts,platform,script,result,parse_ok)"
                 " VALUES ('2026-10-02T08:00:00','p','p.py','CLAIMED',1)")
    conn.commit()
    conn.close()
    led = Ledger(data / "ledger.db")
    check("后缀 .db 自动换成 .json", led.db_path.name == "ledger.json"
          and (data / "ledger.json").is_file())
    check("旧库数据已迁移", led.logs_count("all") == 1)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="clawledger-") as tmp:
        root = Path(tmp)
        test_basic_semantics(root)
        test_legacy_migration(root)
        test_conflict_merge(root)
        test_corrupt_recovery(root)
        test_config_compat(root)
    print()
    if FAILURES:
        print("FAILED:", "、".join(FAILURES))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
