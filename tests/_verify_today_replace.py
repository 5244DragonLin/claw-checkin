# -*- coding: utf-8 -*-
"""临时验证脚本：今天行同源替换 + 小浣熊来源标签跨天稳定（不触碰真实 data/ledger.json）

- ledger：同一天同一 source 再次回报，用最新金额替换旧行——AutoClaw/WorkBuddy
  等「余额口径」平台当日扣减后余额变小，追加会让格子越用越多（本次修复）
- ledger：当天未再回报的 source 原样保留——已到期/耗尽的笔目接口不再回报，
  删除会让「过去 30 天已作废」漏计（既有口径，回归保护）
- _raccoon_expiry：同一笔每日积分在发放日与到期日两次运行得到相同标签，
  账本 (date, amount, source) 去重后到期日不会双计（本次修复）
- _raccoon_expiry：当日补发（发放日 == 到期日）标「当日 24:00 清零」；
  created_at 缺失时退回按运行日判断的老口径；同日不同模式不混算
"""
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from fastapi.testclient import TestClient

import _raccoon_expiry as rx
from claw_checkin.config import AppConfig
from claw_checkin.ledger import Ledger
from claw_checkin.main import create_app

tmp = Path(tempfile.mkdtemp(prefix="clawcheckin_verify_today_"))
cfg = AppConfig()
cfg.scripts.dir = str(tmp / "scripts")
cfg.scripts_path.mkdir(parents=True, exist_ok=True)
(cfg.scripts_path / "p1.py").write_text('print("{}")', encoding="utf-8")
cfg.ledger.db = str(tmp / "ledger.json")
cfg.schedule.enabled = False

ledger = Ledger(cfg.db_path)

T0 = date.today()
D = lambda n: (T0 + timedelta(days=n)).strftime("%Y-%m-%d")   # noqa: E731
TODAY, TM1 = D(0), D(-1)
TP5, TP7, TP8, TP9 = D(5), D(7), D(8), D(9)


def make_rr(platform="p1", result="CLAIMED", parse_ok=True, expiring=()):
    class RR:
        pass
    rr = RR()
    rr.platform = platform
    rr.script = f"{platform}.py"
    rr.result = result
    rr.today_credit = 0
    rr.total_credits = 100
    rr.streak_days = 3
    rr.report = "ok"
    rr.raw = "{}"
    rr.parse_ok = parse_ok
    rr.expiring = [{"date": d, "amount": a, "source": s} for d, a, s in expiring]
    return rr


def count(**kw):
    return sum(1 for r in ledger._snap
               if all(str(r[k]) == str(v) for k, v in kw.items()))


def set_snap_date(old: str, new: str) -> None:
    """模拟跨天：把某个到期日整体平移。"""
    for r in ledger._snap:
        if r["date"] == old:
            r["date"] = new
    ledger._save_locked()


# ---- P1 余额口径平台：今天行同源再回报 → 替换而非追加 ----
ledger.insert_run(make_rr(platform="autoclaw_credits",
                          expiring=[(TODAY, 300, "每日签到"), (TP7, 200, "每日签到")]))
assert count(platform="autoclaw_credits", date=TODAY, amount=300) == 1
ledger.insert_run(make_rr(platform="autoclaw_credits",
                          expiring=[(TODAY, 250, "每日签到"), (TP7, 172, "每日签到")]))
assert count(platform="autoclaw_credits", date=TODAY, amount=300) == 0, \
    "FAIL: 今天同源再回报应替换旧行（否则格子 300+250 越用越多）"
assert count(platform="autoclaw_credits", date=TODAY, amount=250) == 1
assert count(platform="autoclaw_credits", date=TP7, amount=172) == 1, "FAIL: 未来行应重建为新余额"
assert count(platform="autoclaw_credits", date=TP7, amount=200) == 0
print("[P1] 今天行同源替换：300 → 250，无追加  PASS")

# ---- P2 当天未再回报的 source 保留（耗尽/已到期 → 作废统计口径） ----
ledger.insert_run(make_rr(platform="autoclaw_credits", expiring=[(TP8, 100, "签到奖励")]))
assert count(platform="autoclaw_credits", date=TODAY, amount=250) == 1, \
    "FAIL: 未再回报的今天行不应被删除"
print("[P2] 今天行未被回报 → 保留（作废统计不漏计）  PASS")

# ---- P3 同 source 同日多笔：整组替换（WorkBuddy 同名包部分用尽） ----
ledger.insert_run(make_rr(platform="workbuddy_credits",
                          expiring=[(TODAY, 100, "包"), (TODAY, 8, "包"),
                                    (TODAY, 50, "包"), (TODAY, 10, "包")]))
ledger.insert_run(make_rr(platform="workbuddy_credits",
                          expiring=[(TODAY, 100, "包"), (TODAY, 8, "包"), (TODAY, 50, "包")]))
assert count(platform="workbuddy_credits", date=TODAY, source="包") == 3, \
    "FAIL: 同源多笔应整组替换为本次回报"
assert count(platform="workbuddy_credits", date=TODAY, amount=10) == 0
print("[P3] 同源同日多笔整组替换  PASS")

# ---- P4 小浣熊标签跨天稳定：发放日与到期日两次运行同一标签 ----
def bill(created: str, expire: str, points: int = 300) -> dict:
    return {"biz_type": "daily_grant", "points": points,
            "created_at": f"{created}T08:05:00+08:00",
            "expire_at": f"{expire}T23:59:59+08:00"}


grant_day, expiry_day = D(-6), TODAY
bills_login = {"items": [bill(grant_day, expiry_day)]}
at_grant = rx.daily_expiring_from_bills(bills_login, today=grant_day)
at_expiry = rx.daily_expiring_from_bills(bills_login, today=expiry_day)
assert at_grant == at_expiry == \
    [{"date": expiry_day, "amount": 300, "source": "每日积分（当天登录，7 天有效）"}], \
    f"FAIL: 标签应跨天稳定：{at_grant} vs {at_expiry}"
bills_makeup = {"items": [bill(TODAY, TODAY)]}
assert rx.daily_expiring_from_bills(bills_makeup, today=TODAY) == \
    [{"date": TODAY, "amount": 300, "source": "每日积分（当日 24:00 清零）"}]
no_created = {"items": [{"biz_type": "daily_grant", "points": 300,
                         "expire_at": f"{TODAY}T23:59:59+08:00"}]}
assert rx.daily_expiring_from_bills(no_created, today=TODAY)[0]["source"] == \
    "每日积分（当日 24:00 清零）", "FAIL: created_at 缺失应退回按运行日判断"
mix = {"items": [bill(grant_day, expiry_day), bill(TODAY, TODAY, points=200)]}
mixed = rx.daily_expiring_from_bills(mix, today=TODAY)
assert len(mixed) == 2 and all(x["date"] == TODAY for x in mixed) and \
    {x["source"] for x in mixed} == {"每日积分（当天登录，7 天有效）",
                                     "每日积分（当日 24:00 清零）"}, \
    f"FAIL: 同日不同模式不应混算 {mixed}"
print("[P4] 小浣熊标签按发放日判定，跨天稳定  PASS")

# ---- P5 全栈：发放日 → 到期日两次运行，日历与作废统计不双计 ----
app = create_app(cfg, ledger=ledger)
client = TestClient(app, raise_server_exceptions=True)
with client:
    label = "每日积分（当天登录，7 天有效）"
    ledger.insert_run(make_rr(platform="p1_credits", expiring=[(TP9, 300, label)]))
    set_snap_date(TP9, TODAY)                      # 时间跨到到期日
    ledger.insert_run(make_rr(platform="p1_credits", expiring=[(TODAY, 300, label)]))
    j = client.get("/api/expiring").json()
    day = next(x for x in j["days"] if x["date"] == TODAY)
    assert day["amount"] == 300, f"FAIL: 到期日格子应 300，实际 {day['amount']}（标签漂移会变 600）"
    set_snap_date(TODAY, TM1)                      # 再跨一天 → 进入作废统计
    j = client.get("/api/expiring").json()
    assert j["void30"] == 300, f"FAIL: 作废统计应 300，实际 {j['void30']}"
    print("[P5] 到期日格子 300、作废统计 300，无双计  PASS")

print("== 全部通过 ==")
