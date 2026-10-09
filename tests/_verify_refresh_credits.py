# -*- coding: utf-8 -*-
"""临时验证脚本：积分余额刷新（页面加载自动刷新 + 右上角按钮 + 并发保护）

- POST /api/refresh_credits?source=page_load：页面加载触发，真实跑 mock 积分源，
  /api/overview 的卡片余额随之更新
- 防抖：按钮刷新后 30 秒内的 page_load 请求跳过（skipped=True），不重复跑脚本
- 并发保护：刷新进行中时第二个请求立即返回 skipped=True，不排队、不并发跑脚本
- 签到补跑（/api/run）与刷新共用一把锁：排队等待，超时跳过并如实报告 SKIP
- 全程无 lifespan（不建 TestClient 上下文），避免启动线程干扰计数
"""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fastapi.testclient import TestClient

from claw_checkin.config import AppConfig
from claw_checkin.ledger import Ledger
from claw_checkin.main import create_app

tmp = Path(tempfile.mkdtemp(prefix="clawcheckin_verify_refresh_"))
cfg = AppConfig()
cfg.scripts.dir = str(tmp / "scripts")
cfg.scripts_path.mkdir(parents=True, exist_ok=True)
cfg.ledger.db = str(tmp / "ledger.json")
cfg.schedule.enabled = False
cfg.credits.refresh_interval = 0        # 关闭周期线程，只测 HTTP 触发路径
cfg.credits.page_load_debounce = 30     # 默认防抖窗口
cfg.scripts.timeout = 2                 # 签到补跑排队等待上限（秒），配合慢 mock 测 SKIP

control_path = tmp / "control.json"
control_path.write_text(json.dumps({"balance": 1000, "sleep": 0}), encoding="utf-8")

# 可见平台 mock（纯签到，输出最小 JSON）+ 两个隐藏积分源（余额读控制文件；
# 命名必须是 <base>_credits——刷新循环只认这个后缀。两个源是为了让一次刷新
# 的总时长超过签到补跑的排队上限，能测到 SKIP 路径）
(cfg.scripts_path / "mock.py").write_text('print("{}")', encoding="utf-8")
TEMPLATE = '''# -*- coding: utf-8 -*-
import json, time
CONTROL = {control!r}
c = json.loads(open(CONTROL, encoding="utf-8").read())
if c.get("sleep"):
    time.sleep(c["sleep"])
print(json.dumps({{"result": "ALREADY", "report": "余额 {{}} 分".format(c["balance"]),
                   "today_credit": 0, "streak_days": 1,
                   "total_credits": c["balance"], "expiring": []}}, ensure_ascii=False))
'''
for src in ("mock_credits", "mock2_credits"):
    (cfg.scripts_path / f"{src}.py").write_text(
        TEMPLATE.format(control=str(control_path)), encoding="utf-8")
(cfg.scripts_path / "_meta.json").write_text(
    json.dumps({"hidden": ["mock_credits", "mock2_credits"]}), encoding="utf-8")


def set_control(**kw) -> None:
    c = json.loads(control_path.read_text(encoding="utf-8"))
    c.update(kw)
    control_path.write_text(json.dumps(c), encoding="utf-8")


ledger = Ledger(cfg.db_path)
app = create_app(cfg, ledger=ledger)
client = TestClient(app, raise_server_exceptions=True)
client2 = TestClient(app, raise_server_exceptions=True)   # 并发用第二个连接


def post(path: str):
    r = client.post(path)
    assert r.status_code == 200, f"FAIL: POST {path} → {r.status_code} {r.text}"
    return r.json()


def overview_credits() -> int:
    ov = client.get("/api/overview").json()
    p = next(x for x in ov["platforms"] if x["name"] == "mock")
    return p["credits"]


# ---- P1 页面加载触发刷新：真实跑积分源，overview 余额更新 ----
set_control(balance=1000)
j = post("/api/refresh_credits?source=page_load")
assert j["skipped"] is False and j["ran"] == 2 and j["ok"] == 2, f"FAIL P1: {j}"
assert overview_credits() == 1000, f"FAIL P1: overview 应为 1000，实际 {overview_credits()}"
print("[P1] 页面加载刷新：mock 余额 1000 进卡片  PASS")

# ---- P2 按钮刷新拿到最新余额 ----
set_control(balance=950)
j = post("/api/refresh_credits")
assert j["skipped"] is False and j["ok"] == 2, f"FAIL P2: {j}"
assert overview_credits() == 950, f"FAIL P2: overview 应为 950，实际 {overview_credits()}"
print("[P2] 右上角按钮刷新：余额 1000 → 950  PASS")

# ---- P3 防抖：按钮刚刷新过，30 秒内的 page_load 跳过 ----
set_control(balance=1234)     # 若误跑脚本，余额会变成 1234，断言能抓住
j = post("/api/refresh_credits?source=page_load")
assert j["skipped"] is True and j["ran"] == 0, f"FAIL P3: {j}"
assert overview_credits() == 950, "FAIL P3: 防抖窗口内不应真的跑脚本"
print("[P3] 页面加载防抖：30 秒窗口内跳过  PASS")

# ---- P4 并发保护：刷新进行中，第二个请求立即跳过 ----
set_control(balance=800, sleep=1.5)
box = {}
t = threading.Thread(target=lambda: box.update(
    r=client2.post("/api/refresh_credits").json()), daemon=True)
t.start()
time.sleep(0.4)               # 让第一个请求进入脚本执行段（持锁）
j = post("/api/refresh_credits")
assert j["skipped"] is True, f"FAIL P4: 刷新进行中应跳过 {j}"
t.join(timeout=15)
assert box["r"]["skipped"] is False and box["r"]["ok"] == 2, f"FAIL P4: {box}"
assert overview_credits() == 800, f"FAIL P4: overview 应为 800，实际 {overview_credits()}"
print("[P4] 并发保护：进行中跳过，完成后余额 800  PASS")

# ---- P5 签到补跑与刷新共用锁：排队等待，超时如实报告 SKIP ----
# 两个积分源各睡 2.5s（单脚本 2s 超时也不到？不会：2.5 > 2 → 首个源即被杀，
# 但锁在整个刷新函数手里，仍持到约 2s+；补跑排队上限 2s → SKIP）
set_control(balance=700, sleep=1.2)
t = threading.Thread(target=lambda: box.update(
    r2=client2.post("/api/refresh_credits").json()), daemon=True)
t.start()
time.sleep(0.4)
j = post("/api/run/mock")     # 补跑等待 acquire(timeout=2) < 两个源共约 2.4s+ → SKIP
cs = j["credit_sources"]
assert len(cs) == 1 and cs[0]["result"] == "SKIP" and cs[0]["ok"] is True, f"FAIL P5: {cs}"
t.join(timeout=20)
assert box["r2"]["skipped"] is False, f"FAIL P5: {box}"
print("[P5] 签到补跑排队超时 → SKIP（ok=True，不误报失败）  PASS")

# ---- P6 锁释放后一切恢复正常 ----
set_control(balance=600, sleep=0)
j = post("/api/refresh_credits")
assert j["skipped"] is False and j["ok"] == 2, f"FAIL P6: {j}"
assert overview_credits() == 600, f"FAIL P6: {overview_credits()}"
print("[P6] 锁释放后刷新恢复正常  PASS")

print("== 全部通过 ==")
