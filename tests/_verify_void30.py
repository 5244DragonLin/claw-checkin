# -*- coding: utf-8 -*-
"""临时验证脚本：过期快照历史保留 + 「过去 30 天已作废」统计（不触碰真实 data/ledger.json）

- 快照更新不再整平台覆盖删除：未来行（date > 今天）重建，今天及以前的行保留为作废历史
- 当天到期的积分在当天后续运行（接口已不再回报）中不被误删
- 重复回报按 (date, amount, source) 去重，且单次回报内的重复笔数保留
- 失败运行（expiring 为空且非成功态）不清快照；成功空回报清未来行
- 改名过渡：旧名 <base> 只清未来行，过期历史行保留
- GET /api/expiring 的 void30 汇总可见归属组的过期历史行
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

tmp = Path(tempfile.mkdtemp(prefix="clawcheckin_verify_void30_"))
cfg = AppConfig()
cfg.scripts.dir = str(tmp / "scripts")
cfg.scripts_path.mkdir(parents=True, exist_ok=True)
# 最小合法脚本：让 p1 成为可见平台（void30 按可见归属组过滤），验证中不真实执行它
(cfg.scripts_path / "p1.py").write_text('print("{}")', encoding="utf-8")
cfg.ledger.db = str(tmp / "ledger.json")
cfg.schedule.enabled = False

ledger = Ledger(cfg.db_path)

T0 = date.today()
D = lambda n: (T0 + timedelta(days=n)).strftime("%Y-%m-%d")   # noqa: E731
TODAY, TM1, TM2 = D(0), D(-1), D(-2)
TP1, TP3, TP5, TP7, TP10 = D(1), D(3), D(5), D(7), D(10)


def make_rr(platform="p1", result="CLAIMED", parse_ok=True, expiring=()):
    class RR:
        pass
    rr = RR()
    rr.platform = platform
    rr.script = f"{platform}.py"
    rr.result = result
    rr.today_credit = 5
    rr.total_credits = 100
    rr.streak_days = 3
    rr.report = "ok"
    rr.raw = "{}"
    rr.parse_ok = parse_ok
    rr.expiring = [{"date": d, "amount": a, "source": s} for d, a, s in expiring]
    return rr


def snap_rows(**kw):
    """快照行（新账本为单文件 JSON，直接读实例内部列表）。"""
    return [r for r in ledger._snap
            if all(str(r[k]) == str(v) for k, v in kw.items())]


def count(**kw):
    return len(snap_rows(**kw))


def set_snap_date(old: str, new: str) -> None:
    """模拟跨天：把某个到期日整体平移（旧 SQLite 版用裸 UPDATE + commit）。"""
    for r in ledger._snap:
        if r["date"] == old:
            r["date"] = new
    ledger._save_locked()


# ---- P0 首次写入：两笔未来积分 ----
ledger.insert_run(make_rr(expiring=[(TP1, 100, "签到"), (TP10, 50, "版本")]))
assert count(date=TP1) == 1 and count(date=TP10) == 1, "FAIL: 首次写入应 2 行"
print("[P0] 首次写入 2 行未来快照  PASS")

# ---- P1 次日运行：Tp1 的积分已过期（模拟：该行日期变为昨天），接口不再回报它 ----
# 注：脚本侧与 create_app 通过 ledger 参数共享同一实例，写入对 API 立即可见
set_snap_date(TP1, TM1)
ledger.insert_run(make_rr(expiring=[(TP10, 50, "版本")]))
assert count(date=TM1, amount=100) == 1, "FAIL: 过期历史行应保留"
assert count(date=TP10, amount=50) == 1, "FAIL: 未来行应重建为 1 行（不翻倍）"
assert len(snap_rows()) == 2, f"FAIL: 总行数应 2，实际 {len(snap_rows())}"
print("[P1] 次日运行：过期历史行保留，未来行重建  PASS")

# ---- P2 全栈 void30：过去 30 天已作废 = 100 ----
app = create_app(cfg, ledger=ledger)
client = TestClient(app, raise_server_exceptions=True)
with client:
    j = client.get("/api/expiring").json()
    assert j["void30"] == 100, f"FAIL: void30 应 100，实际 {j['void30']}"
    print("[P2] /api/expiring void30 = 100  PASS")

    # ---- P3 当天到期的积分：当天晚些的运行接口已不再回报，不得误删 ----
    ledger.insert_run(make_rr(expiring=[(TODAY, 60, "签到"), (TP10, 50, "版本")]))
    assert count(date=TODAY, amount=60) == 1, "FAIL: 当天到期行应写入"
    ledger.insert_run(make_rr(expiring=[(TP10, 50, "版本")]))   # 当天那笔不再回报
    assert count(date=TODAY, amount=60) == 1, "FAIL: 当天到期行不应被后续运行删除"
    set_snap_date(TODAY, TM1)
    j = client.get("/api/expiring").json()
    assert j["void30"] == 160, f"FAIL: 跨天后 void30 应 160，实际 {j['void30']}"
    print("[P3] 当天到期积分不被误删，跨天计入作废  PASS")

    # ---- P4 去重：单次回报重复笔保留；跨次重复回报不翻倍；已有当天行不重复插 ----
    ledger.insert_run(make_rr(expiring=[(TP5, 100, "包"), (TP5, 100, "包")]))
    assert count(date=TP5, amount=100) == 2, "FAIL: 单次回报 2 笔同形积分都应保留"
    ledger.insert_run(make_rr(expiring=[(TP5, 100, "包"), (TP5, 100, "包")]))
    assert count(date=TP5, amount=100) == 2, "FAIL: 跨次重复回报不应翻倍"
    ledger.insert_run(make_rr(expiring=[(TODAY, 60, "签到"), (TP5, 100, "包")]))
    assert count(date=TODAY, amount=60) == 1, "FAIL: 已存在的当天行不应重复插入"
    print("[P4] 去重口径：保留同报重复笔、跨次与当天行不翻倍  PASS")

    # ---- P5 失败运行不清快照 ----
    ledger.insert_run(make_rr(expiring=[(TP3, 20, "签到")]))
    assert count(date=TP3, amount=20) == 1, "FAIL: 前置失败"
    ledger.insert_run(make_rr(result="NO_SESSION", parse_ok=False, expiring=[]))
    assert count(date=TP3, amount=20) == 1, "FAIL: 失败运行不应清未来行"
    assert count(date=TM1, amount=100) == 1, "FAIL: 失败运行不应清历史行"
    print("[P5] 失败运行（NO_SESSION）快照原样保留  PASS")

    # ---- P6 成功空回报：清未来行，历史行保留 ----
    ledger.insert_run(make_rr(result="ALREADY", expiring=[]))
    assert count(date=TP3, amount=20) == 0, "FAIL: 成功空回报应清未来行"
    assert count(date=TP5, amount=100) == 0, "FAIL: 成功空回报应清未来行"
    assert count(date=TM1, amount=100) == 1 and count(date=TM1, amount=60) == 1, \
        "FAIL: 成功空回报不应清历史行"
    print("[P6] 成功空回报清未来行、历史行保留  PASS")

    # ---- P7 改名过渡：旧名 p1 → 新名 p1_credits ----
    ledger._snap.append({"id": ledger._next_id, "platform": "p1", "date": TM2,
                         "amount": 77, "source": "签到", "fetched_at": TM2})
    ledger._next_id += 1
    ledger._save_locked()
    ledger.insert_run(make_rr(platform="p1", expiring=[(TP7, 33, "签到")]))
    ledger.insert_run(make_rr(platform="p1_credits", expiring=[(TP7, 44, "签到")]))
    assert count(platform="p1", date=TP7) == 0, "FAIL: 旧名未来行应被清除（避免双计）"
    assert count(platform="p1", date=TM2, amount=77) == 1, "FAIL: 旧名历史行应保留"
    assert count(platform="p1_credits", date=TP7, amount=44) == 1, "FAIL: 新名未来行应写入"
    print("[P7] 改名过渡：旧名只清未来行，历史行保留  PASS")

    # ---- P8 全栈终检：void30 汇总 + 未来日历不受污染 ----
    j = client.get("/api/expiring").json()
    assert j["void30"] == 237, f"FAIL: void30 应 237（100+60+77），实际 {j['void30']}"
    day_tp7 = next(x for x in j["days"] if x["date"] == TP7)
    assert day_tp7["amount"] == 44 and day_tp7["items"][0]["platform"] == "p1", \
        f"FAIL: 未来日历应含 p1_credits 的新行（归属展示为 p1）{day_tp7}"
    future_total = sum(x["amount"] for x in j["days"])
    assert future_total == 44, f"FAIL: 未来日历应只剩 44，实际 {future_total}"
    ov = client.get("/api/overview").json()
    p1 = next(p for p in ov["platforms"] if p["name"] == "p1")
    assert p1["has_expiring"] is True, "FAIL: 有未来行时 has_expiring 应为 True"
    print("[P8] void30=237，未来日历与总览不受历史行污染  PASS")

print("== 全部通过 ==")
