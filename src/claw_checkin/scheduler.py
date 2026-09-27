"""每日定时调度：到点把 scripts/ 下所有平台各跑一遍；凭据类失败当日每小时自动补签"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from claw_checkin.config import AppConfig
from claw_checkin.ledger import Ledger
from claw_checkin.runner import run_script
from claw_checkin.scanner import build_command, scan_scripts

log = logging.getLogger("claw_checkin.scheduler")


class DailyScheduler:
    """轻量线程调度器：每 30 秒检查一次是否到达每日签到时刻，当日未触发则执行。

    不引入重量级调度依赖；错过时刻（如机器关机）会在当日下次检查时补跑一次。
    当日已跑但存在凭据类失败（NO_AUTH/NETWORK，如 AutoClaw token 尚未随客户端
    使用而轮换）时，每小时自动补跑这些平台，成功即止。
    """

    def __init__(self, cfg: AppConfig, ledger: Ledger, state: dict):
        self.cfg = cfg
        self.ledger = ledger
        self.state = state
        self._timer: threading.Timer | None = None
        self._running = False
        self._last_trigger_date: str | None = None
        self._last_retry_at = 0.0

    def start(self):
        if not self.cfg.schedule.enabled:
            log.info("定时签到未启用")
            return
        self._running = True
        self._tick()

    def stop(self):
        self._running = False
        if self._timer:
            self._timer.cancel()

    def _tick(self):
        if not self._running:
            return
        try:
            self._check_and_run()
        except Exception:
            log.exception("调度检查失败")
        finally:
            self._timer = threading.Timer(30, self._tick)
            self._timer.daemon = True
            self._timer.start()

    def _check_and_run(self):
        now = datetime.now()
        target = self.cfg.schedule.time
        try:
            hh, mm = (int(x) for x in target.split(":"))
        except ValueError:
            log.error("schedule.time 格式错误：%s（应为 HH:MM）", target)
            return
        today = now.strftime("%Y-%m-%d")
        if now.hour * 60 + now.minute < hh * 60 + mm:
            return
        if self._last_trigger_date == today:
            return
        # 今天到点后已由调度跑过则不重复（按 ledger 中 schedule 触发记录判断）
        if self.ledger.has_schedule_run_today(today):
            self._last_trigger_date = today
            self._maybe_retry(today)
            return
        log.info("到达每日签到时刻 %s，开始调度", target)
        self._last_trigger_date = today
        self._last_retry_at = time.time()
        threading.Thread(target=self._run_all, daemon=True).start()

    def _maybe_retry(self, today: str) -> None:
        """当日存在凭据类失败（NO_AUTH/NETWORK）时每小时自动补跑一次。

        典型场景：AutoClaw token 随客户端使用轮换（24 小时有效），签到时刻
        token 可能尚未轮换；用户当天任何一次使用客户端后即可补签成功。
        """
        if time.time() - self._last_retry_at < 3600:
            return
        names = self.ledger.retryable_failures_today(today)
        if not names:
            return
        self._last_retry_at = time.time()
        log.info("当日存在凭据类失败（%s），开始自动补签", "、".join(names))
        threading.Thread(target=self._rerun_names, args=(names,), daemon=True).start()

    def _rerun_names(self, names: list[str]) -> None:
        scripts = {s.name: s for s in scan_scripts(self.cfg.scripts_path)}
        for name in names:
            s = scripts.get(name)
            if not s:
                continue
            rr = run_script(s.name, s.path, build_command(s), self.cfg.scripts.timeout)
            self.ledger.insert_run(rr, trigger="retry")
            log.info("自动补签 %s → %s", s.name, rr.result)

    def _run_all(self):
        scripts = scan_scripts(self.cfg.scripts_path)
        # 先跑可见平台，再跑隐藏的积分源子功能（如 workbuddy_credits）：
        # 保证「总积分」取的是签到之后的值
        visible = [s for s in scripts if not s.hidden]
        hidden = [s for s in scripts if s.hidden]
        for s in visible + hidden:
            rr = run_script(s.name, s.path, build_command(s), self.cfg.scripts.timeout)
            self.ledger.insert_run(rr, trigger="schedule")
            log.info("调度签到 %s → %s", s.name, rr.result)
        # 首次进入面板也能看到体检状态
        for s in scripts:
            if s.name not in self.state.get("health", {}):
                from claw_checkin.runner import health_check
                self.state.setdefault("health", {})[s.name] = health_check(s, self.cfg.scripts.timeout)
