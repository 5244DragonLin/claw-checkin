# -*- coding: utf-8 -*-
"""验证脚本：WorkBuddy 累计积分含体验版周期额度（不碰真实接口，用构造数据）

用户口径：「累计积分」= 真正能用的额度。日常消耗优先扣赠送包，赠送包用尽后
才动用每月 500 体验额度，故体验版剩余必须计入 total_credits。

验证要点：
- total_credits = 赠送包剩余 + 体验版周期剩余
- 体验版走Cycle* 周期口径，不走 Capacity* 总池口径
- 体验版仍不进入 expiring（按月重置≠到期作废）
- 赠送包用尽时，总积分 = 体验版剩余（不为负、不为 0）
- 体验版已用尽时，total_credits 只算赠送包
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("wbc", SCRIPTS / "workbuddy_credits.py")
wbc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wbc)


def acct(capacity_type=1, remain=1000, size=3000, used=None, name="裂变包",
         cycle_end="2026-12-31 23:59:59", c_remain=None, c_size=None, c_used=None):
    """构造一个 Accounts 条目。c_* 用于构造体验版的周期口径。"""
    used = (size - remain) if used is None else used
    a = {
        "CapacityType": capacity_type,
        "PackageName": name,
        "CapacityRemain": remain, "CapacityRemainPrecise": remain,
        "CapacitySize": size, "CapacitySizePrecise": size,
        "CapacityUsed": used, "CapacityUsedPrecise": used,
        "CycleCapacityRemain": remain, "CycleCapacityRemainPrecise": remain,
        "CycleCapacitySize": size, "CycleCapacitySizePrecise": size,
        "CycleCapacityUsed": used, "CycleCapacityUsedPrecise": used,
        "CycleEndTime": cycle_end,
        "CycleStartTime": "2026-01-01 00:00:00",
    }
    if c_remain is not None:      #体验版：总池仍有500，但周期内已用完
        a["CycleCapacityRemain"] = c_remain
        a["CycleCapacityRemainPrecise"] = c_remain
    if c_size is not None:
        a["CycleCapacitySize"] = c_size
        a["CycleCapacitySizePrecise"] = c_size
    if c_used is not None:
        a["CycleCapacityUsedPrecise"] = c_used
    return a


def total_of(accounts):
    """复刻 main 的合计口径：赠送包 + 体验版。"""
    expiring, summary = wbc.build_expiring(accounts)
    return math_ceil(summary["gift_remain"] + summary["trial_remain"]), expiring, summary


def math_ceil(v):
    import math
    return int(math.ceil(v))


# ---- P1 典型场景：赠送包 7205 + 体验版 500 = 7705 ----
accounts = [acct(remain=7205, size=8000, name="裂变包")]
total, expiring, summary = total_of(accounts)
assert summary["gift_remain"] == 7205, f"FAIL: 赠送包应为 7205，实际 {summary['gift_remain']}"
assert summary["trial_remain"] == 0, "FAIL: 本例无体验版"
assert total == 7205, f"FAIL: 无体验版时 total 应 7205，实际 {total}"
print("[P1] 无体验版：total = 赠送包剩余7205  PASS")

# ---- P2 体验版全额未用：应计入 ----
accounts = [
    acct(remain=7205, size=8000, name="裂变包"),
    acct(capacity_type=4, remain=500, size=500, used=0, name="CodeBuddy个人体验版",
         cycle_end="2026-10-31 23:59:59", c_remain=500, c_size=500, c_used=0),
]
total, expiring, summary = total_of(accounts)
assert summary["trial_remain"] == 500, f"FAIL: 体验版周期剩余应为 500，实际 {summary['trial_remain']}"
assert total == 7705, f"FAIL: total 应为 7205+500=7705，实际 {total}"
print("[P2] 体验版未用：total = 7205 + 500 = 7705  PASS")

# ---- P3 赠送包用尽、只剩体验版：total = 体验版剩余，不为 0 ----
accounts = [
    acct(remain=0, size=8000, used=8000, name="裂变包"),
    acct(capacity_type=4, remain=500, size=500, used=0, name="CodeBuddy个人体验版",
         cycle_end="2026-10-31 23:59:59", c_remain=500, c_size=500, c_used=0),
]
total, expiring, summary = total_of(accounts)
assert total == 500, f"FAIL: 赠送包用尽时 total 应等于体验版 500，实际 {total}"
assert all(e["source"] != "CodeBuddy个人体验版" for e in expiring), \
    "FAIL: 体验版按月重置，不应进入 expiring"
print("[P3] 赠送包用尽：total = 500（体验版），且不进expiring  PASS")

# ---- P4 体验版已用尽：只算赠送包 ----
accounts = [
    acct(remain=7205, size=8000, name="裂变包"),
    acct(capacity_type=4, remain=500, size=500, used=500, name="CodeBuddy个人体验版",
         cycle_end="2026-10-31 23:59:59", c_remain=0, c_size=500, c_used=500),
]
total, expiring, summary = total_of(accounts)
assert summary["trial_remain"] == 0, f"FAIL: 体验版周期剩余应为 0，实际 {summary['trial_remain']}"
assert total == 7205, f"FAIL: 体验版用尽时 total 应为 7205，实际 {total}"
print("[P4] 体验版用尽：total = 7205（不虚增）  PASS")

# ---- P5 体验版必须走 Cycle* 周期口径（总池 500 但周期内扣完 →计 0） ----
accounts = [
    acct(capacity_type=4, remain=500, size=500, used=0, name="CodeBuddy个人体验版",
         cycle_end="2026-10-31 23:59:59", c_remain=0, c_size=500, c_used=500),
]
total, expiring, summary = total_of(accounts)
assert summary["trial_remain"] == 0, \
    f"FAIL: 周期内已扣完应计 0，实际 {summary['trial_remain']}（疑似误用 Capacity* 总池口径）"
assert total == 0, f"FAIL: total 应为 0，实际 {total}"
print("[P5] 体验版取周期口径（总池 500 / 周期 0 → 计 0）  PASS")

# ---- P6 无任何额度：total 为 0，不报错 ----
total, expiring, summary = total_of([])
assert total == 0, f"FAIL: 空账号 total 应为 0，实际 {total}"
assert expiring == [], "FAIL: 空账号不应有 expiring"
print("[P6] 空账号：total = 0，流程不报错  PASS")

# ---- P7 多个赠送包 + 体验版：合计正确 ----
accounts = [
    acct(remain=3000, size=5000, used=2000, name="裂变包A"),
    acct(remain=4205, size=5000, used=795, name="裂变包B"),
    acct(capacity_type=4, remain=500, size=500, used=0, name="体验版",
         cycle_end="2026-10-31 23:59:59", c_remain=500, c_size=500, c_used=0),
]
total, expiring, summary = total_of(accounts)
assert total == 3000 + 4205 + 500, f"FAIL: 应为 7705，实际 {total}"
assert len([e for e in expiring if e["source"] != "体验版"]) == 2, "FAIL: 两个赠送包都应进 expiring"
print("[P7] 多包合计：3000 + 4205 + 500 = 7705  PASS")

print("== 全部通过 ==")
