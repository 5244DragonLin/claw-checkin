# -*- coding: utf-8 -*-
"""临时验证脚本：/api/logs/clear 清空运行日志功能（不触碰真实 data/ledger.json）

- Ledger.clear_logs：只删 checkin_log，expiring_snapshot 保留，返回删除条数
- POST /api/logs/clear：走 create_app 全栈验证（注入同一 Ledger 实例 + 临时脚本目录 + 关调度）
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

from claw_checkin.config import AppConfig
from claw_checkin.ledger import Ledger
from claw_checkin.main import create_app

tmp = Path(tempfile.mkdtemp(prefix="clawcheckin_verify_"))
cfg = AppConfig()
cfg.scripts.dir = str(tmp / "scripts")
cfg.scripts_path.mkdir(parents=True, exist_ok=True)
# 最小合法脚本：让 p1 成为可见平台（api_expiring 按可见性过滤），验证中不真实执行它
(cfg.scripts_path / "p1.py").write_text('print("{}")', encoding="utf-8")
cfg.ledger.db = str(tmp / "ledger.json")
cfg.schedule.enabled = False

ledger = Ledger(cfg.db_path)


def make_rr(platform="p1", result="CLAIMED", credit=5):
    class RR:
        pass
    rr = RR()
    rr.platform = platform
    rr.script = f"{platform}.py"
    rr.result = result
    rr.today_credit = credit
    rr.total_credits = 100
    rr.streak_days = 3
    rr.report = "ok"
    rr.raw = "{}"
    rr.parse_ok = True
    rr.expiring = [{"date": "2026-10-05", "amount": 30, "source": "签到奖励"},
                   {"date": "2026-10-20", "amount": 20, "source": "版本奖励"}] if platform == "p1" else []
    return rr


# 造数据：5 条 manual 运行日志（p1，带过期快照）+ 1 条 schedule 日志（p1，覆盖式更新快照仍 2 条）
for i in range(5):
    ledger.insert_run(make_rr(), trigger="manual")
ledger.insert_run(make_rr(result="ALREADY"), trigger="schedule")

before = ledger.logs_count("all")
snap_before = len(ledger._snap)
assert before == 6, f"前置失败：日志应 6 条，实际 {before}"
assert snap_before == 2, f"前置失败：快照应 2 条，实际 {snap_before}"
print(f"[P0] 造数完成：日志 {before} 条，过期快照 {snap_before} 条")

# ---- 1) Ledger.clear_logs 单元级 ----
deleted = ledger.clear_logs()
assert deleted == 6, f"FAIL: 应删 6 条，实际 {deleted}"
assert ledger.logs_count("all") == 0, "FAIL: 清空后日志应为 0"
snap_after = len(ledger._snap)
assert snap_after == 2, f"FAIL: 过期快照不应被删（清后 {snap_after} 条）"
print(f"[P1] Ledger.clear_logs: 删除 {deleted} 条日志，过期快照 {snap_after} 条原样保留  PASS")

# ---- 2) 清空后账本仍可正常写入（连接未坏）----
ledger.insert_run(make_rr(), trigger="manual")
assert ledger.logs_count("all") == 1, "FAIL: 清空后应能正常插入新日志"
print("[P2] 清空后账本写入正常  PASS")

# ---- 3) 全栈 API 验证（不走 /api/run，避免真的执行脚本；注入同一 Ledger 实例）----
app = create_app(cfg, ledger=ledger)
client = TestClient(app, raise_server_exceptions=True)
with client:
    # 3a) 再造 3 条（p2 无过期快照）
    for _ in range(3):
        ledger.insert_run(make_rr(platform="p2", result="ERROR"), trigger="manual")
    n = ledger.logs_count("all")
    assert n == 4, f"前置失败：应 4 条，实际 {n}"
    r = client.get("/api/logs?limit=50")
    assert r.status_code == 200 and r.json()["total"] == 4, "FAIL: /api/logs 应返回 4 条"
    print(f"[P3] API 造数 {n} 条，/api/logs 读取正常  PASS")

    # 3b) POST /api/logs/clear
    r = client.post("/api/logs/clear")
    assert r.status_code == 200, f"FAIL: 状态码 {r.status_code} {r.text}"
    body = r.json()
    assert body["ok"] is True and body["deleted"] == 4, f"FAIL: 返回体异常 {body}"
    assert client.get("/api/logs?limit=50").json()["total"] == 0, "FAIL: 清空后 /api/logs 应为 0"
    print(f"[P4] POST /api/logs/clear → ok, deleted={body['deleted']}，/api/logs 归零  PASS")

    # 3c) 过期日历数据不受影响
    r = client.get("/api/expiring")
    j = r.json()
    p1_days = [x for x in j["days"] if x["amount"] > 0]
    assert any(any(i["platform"] == "p1" for i in x["items"]) for x in p1_days), \
        f"FAIL: 清空日志后过期日历应仍有 p1 数据 {j}"
    print("[P5] 清空日志后 /api/expiring 过期日历数据不受影响  PASS")

    # 3d) CSV 导出在空表下不报错
    r = client.get("/api/logs/export")
    assert r.status_code == 200, "FAIL: 空表导出 CSV 应 200"
    print("[P6] 空表导出 CSV 正常  PASS")

print("== 全部通过 ==")
