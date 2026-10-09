# -*- coding: utf-8 -*-
"""验证脚本：过期日历「当日积分」与平台实时余额一致（不触碰真实 data/ledger.json）

复现真实故障：某笔积分昨天上报时到期日=今天，今天平台接口已不再回报它
（当天被消耗掉/已过期待清理），但旧口径把该行原样保留，导致日历「今日到期」
比平台客户端显示的多（如 AutoClaw 实际余额 813，日历当天却挂 4965）。

本脚本验证新口径：
- 今天行里本次未回报的 source → 打 live=0（当日格子不再显示），数据不删
- 失效行跨天后自动计入 void30，不丢作废统计
- 未来行与总览口径不受影响
- 数据本身在同一天内随接口扣减而更新（当日格子=实时）
"""
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

from claw_checkin.config import AppConfig
from claw_checkin.ledger import Ledger
from claw_checkin.main import create_app

tmp = Path(tempfile.mkdtemp(prefix="clawcheckin_verify_todaylive_"))
cfg = AppConfig()
cfg.scripts.dir = str(tmp / "scripts")
cfg.scripts_path.mkdir(parents=True, exist_ok=True)
(cfg.scripts_path / "p1.py").write_text('print("{}")', encoding="utf-8")
cfg.ledger.db = str(tmp / "ledger.json")
cfg.schedule.enabled = False

ledger = Ledger(cfg.db_path)

T0 = date.today()
D = lambda n: (T0 + timedelta(days=n)).strftime("%Y-%m-%d")   # noqa: E731
TODAY, TM1, TP1, TP2 = D(0), D(-1), D(1), D(2)


def make_rr(result="ALREADY", expiring=()):
    class RR:
        pass
    rr = RR()
    rr.platform = "p1_credits"
    rr.script = "p1_credits.py"
    rr.result = result
    rr.today_credit = 0
    rr.total_credits = 100
    rr.streak_days = 0
    rr.report = "ok"
    rr.raw = "{}"
    rr.parse_ok = True
    rr.expiring = [{"date": d, "amount": a, "source": s} for d, a, s in expiring]
    return rr


def snap(**kw):
    return [r for r in ledger._snap
            if all(str(r[k]) == str(v) for k, v in kw.items())]


def today_cell(client):
    """日历「今天」格子的合计金额。"""
    j = client.get("/api/expiring").json()
    return next(x["amount"] for x in j["days"] if x["date"] == TODAY)


# ---- P0 首次回报：今天有一笔 4965 + 一笔未来113 ----
ledger.insert_run(make_rr(expiring=[(TODAY, 4965, "活动奖励"), (TP1, 113, "每日签到")]))
app = create_app(cfg, ledger=ledger)
client = TestClient(app, raise_server_exceptions=True)
with client:
    assert today_cell(client) == 4965, f"FAIL: 首次回报当天格应4965，实际 {today_cell(client)}"
    print("[P0] 首次回报：日历当天 = 4965（与平台一致）  PASS")

    # ---- P1 核心回归：该笔当天被消耗，接口不再回报它 → 日历当天必须同步归零 ----
    #接口现在只剩113+200（复刻真实场景：余额 813，日历当天却是 4965）
    ledger.insert_run(make_rr(expiring=[(TP1, 113, "每日签到"), (TP2, 200, "每日签到")]))
    got = today_cell(client)
    assert got == 0, f"FAIL: 未回报的当天行应实时失效，日历当天应为 0，实际 {got}"
    # 关键：数据不能删——还要供跨天后的 void30 统计
    assert len(snap(date=TODAY, amount=4965)) == 1, "FAIL: 失效行数据应保留（供作废统计）"
    assert int(snap(date=TODAY, amount=4965)[0]["live"]) == 0, "FAIL: 该行应标记 live=0"
    print("[P1] 当天未回报的积分实时失效：日历当天 4965 → 0，数据保留  PASS")

    # ---- P2 余额口径总览：当日到期/最早到期不把失效行算进去 ----
    ov = client.get("/api/overview").json()
    p1 = next(p for p in ov["platforms"] if p["name"] == "p1")
    assert p1["expiring_14d"] == 313, f"FAIL: 14 天内应只含 113+200=313，实际 {p1['expiring_14d']}"
    assert p1["earliest_expiring"] == TP1, f"FAIL: 最早到期应为 {TP1}，实际 {p1['earliest_expiring']}"
    print("[P2] 总览口径同步：14 天内 313、最早到期不含失效行  PASS")

    # ---- P3 当日同source 余额扣减 → 当天格实时跟随，不双计 ----
    ledger.insert_run(make_rr(expiring=[(TODAY, 60, "每日签到"), (TP1, 113, "每日签到")]))
    assert today_cell(client) == 60, f"FAIL: 当天同来源余额应为 60，实际 {today_cell(client)}"
    ledger.insert_run(make_rr(expiring=[(TODAY, 20, "每日签到"), (TP1, 113, "每日签到")]))
    assert today_cell(client) == 20, f"FAIL: 当天余额再扣减应为 20，实际 {today_cell(client)}"
    assert len(snap(date=TODAY, source="每日签到")) == 1, "FAIL: 同来源不应翻倍"
    print("[P3] 当天同来源余额扣减：日历当天实时跟随且不双计  PASS")

    # ---- P4 失效行跨天后计入 void30（作废统计不丢） ----
    j = client.get("/api/expiring").json()
    void_before = j["void30"]
    assert void_before == 0, f"FAIL: 跨天前今天不算作废，实际 {void_before}"
    # 模拟跨天：把剩余的今天行日期整体推到昨天
    for r in ledger._snap:
        if r["date"] == TODAY:
            r["date"] = TM1
    ledger._save_locked()
    j = client.get("/api/expiring").json()
    # 4965(失效) + 20(跨天前为当天) = 4985
    assert j["void30"] == 4985, f"FAIL: 跨天后 void30 应 4985，实际 {j['void30']}"
    assert j["days"][0]["amount"] == 0, "FAIL: 昨天的行不该出现在今天的格子里"
    print("[P4] 失效行跨天计入 void30 = 4985，未来日历不受污染  PASS")

print("== 全部通过 ==")