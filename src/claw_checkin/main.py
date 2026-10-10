"""ClawCheckin 服务：FastAPI 应用 + 每日定时调度"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from claw_checkin.config import AppConfig, PROJECT_ROOT, load_config
from claw_checkin.ledger import Ledger
from claw_checkin.runner import health_check, run_script
from claw_checkin.scanner import build_command, scan_scripts
from claw_checkin.scheduler import DailyScheduler

log = logging.getLogger("claw_checkin")
WEB_DIR = PROJECT_ROOT / "web"


def _load_config_with_env() -> AppConfig:
    """加载配置并应用环境变量覆盖（--reload 模式下 uvicorn factory 无参调用时使用）。"""
    cfg = load_config()
    if os.environ.get("CLAWDESK_HOST"):
        cfg.server.host = os.environ["CLAWDESK_HOST"]
    if os.environ.get("CLAWDESK_PORT"):
        try:
            cfg.server.port = int(os.environ["CLAWDESK_PORT"])
        except ValueError:
            pass
    if os.environ.get("CLAWDESK_SCRIPTS_DIR"):
        cfg.scripts.dir = os.environ["CLAWDESK_SCRIPTS_DIR"]
    elif os.environ.get("CLAWDESK_SCRIPTS"):      # 旧名兼容
        cfg.scripts.dir = os.environ["CLAWDESK_SCRIPTS"]
    if os.environ.get("CLAWDESK_NO_SCHEDULE") == "1":
        cfg.schedule.enabled = False
    return cfg


def create_app(config: AppConfig | None = None, ledger: Ledger | None = None) -> FastAPI:
    cfg = config or _load_config_with_env()
    ledger = ledger or Ledger(cfg.db_path)
    ledger.cleanup(cfg.ledger.retention_days,
                   cfg.ledger.snapshot_retention_days)

    state = {
        "health": {},       # platform → 体检结果
    }

    # 积分源刷新串行化：HTTP 请求、启动线程、周期线程、签到补跑都汇到这里。
    # 周期/页面加载竞争时直接跳过而不是排队——脚本跑一次只需几秒，跳过方下次
    # 轮询/页面刷新自然拿到新值，也避免并发跑同一批脚本（小浣熊发放链路对
    # 并发敏感）；签到后的补跑属「拿到最新余额」的关键路径，改为排队等待
    _refresh_lock = threading.Lock()
    _last_refresh = [0.0]        # 上次实际跑脚本的时刻（monotonic），页面加载防抖用

    def _refresh_credits_once(trigger: str) -> list[dict] | None:
        """补跑全部隐藏积分源（纯查询、不签到），返回每个积分源的结果。

        返回 None 表示本次被跳过（已有刷新在跑，或页面加载防抖窗口内），
        与「没有积分源脚本」的空列表区分——前端据此决定要不要重拉数据。
        刷新只在「有变化」时写入账本（结果状态翻转 / 余额变化 / 临期集合变化）：
        周期刷新若每次都落盘，账本单文件会被高频重写，同步盘流量也扛不住。
        """
        if not _refresh_lock.acquire(blocking=False):
            return None
        try:
            now = time.monotonic()
            if (trigger == "page_load"
                    and now - _last_refresh[0] < cfg.credits.page_load_debounce):
                return None      # 浏览器 F5 连点：防抖窗口内不重复跑脚本
            _last_refresh[0] = now
            results = []
            latest = ledger.latest_per_platform()
            snap_now: dict[str, list] = {}
            # 用实时口径（今天及以后 + 接口仍持有）比对：失效行不算变化，
            # 否则每次刷新都会因「历史行集合」差异反复落盘
            for e in ledger.expiring_snapshots(live_only=True):
                snap_now.setdefault(e["platform"], []).append(
                    (e["date"], e["amount"], e["source"]))
            for s in scan_scripts(cfg.scripts_path):
                if not (s.hidden and s.name.endswith("_credits")):
                    continue
                try:
                    rr = run_script(s.name, s.path, build_command(s), cfg.scripts.timeout)
                    exp_new = sorted((str(e.get("date", "")), int(e.get("amount", 0)),
                                      str(e.get("source", ""))) for e in rr.expiring)
                    prev = latest.get(s.name)
                    changed = (prev is None
                               or rr.result != prev["result"]
                               or rr.total_credits != prev["total_credits"]
                               or exp_new != sorted(snap_now.get(s.name, [])))
                    if changed:
                        ledger.insert_run(rr, trigger=trigger)
                        log.info("积分刷新(%s) %s → %s", trigger, s.name, rr.result)
                    results.append({"platform": s.name, "result": rr.result,
                                    "report": rr.report, "ok": rr.ok})
                except Exception as e:
                    log.exception("积分刷新(%s)失败：%s", trigger, s.name)
                    results.append({"platform": s.name, "result": "ERROR",
                                    "report": str(e), "ok": False})
            return results
        finally:
            _refresh_lock.release()

    class _CreditRefresher:
        """按固定间隔后台刷新积分余额（消耗积分后无需重启面板即可见最新值）。"""

        def __init__(self, interval: int):
            self.interval = max(0, int(interval))
            self._stop = threading.Event()

        def start(self):
            if self.interval <= 0:
                log.info("积分余额自动刷新未启用（credits.refresh_interval=0）")
                return
            def _loop():
                while not self._stop.wait(self.interval):
                    try:
                        _refresh_credits_once("refresh")
                    except Exception:
                        log.exception("积分余额周期刷新失败")
            threading.Thread(target=_loop, daemon=True,
                             name="credit-refresh-loop").start()

        def stop(self):
            self._stop.set()

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        threading.Thread(target=_refresh_credits_once, args=("refresh",),
                         daemon=True, name="credit-refresh").start()
        refresher = _CreditRefresher(cfg.credits.refresh_interval)
        refresher.start()
        scheduler = DailyScheduler(cfg, ledger, state)
        scheduler.start()
        yield
        refresher.stop()
        scheduler.stop()

    app = FastAPI(title="ClawCheckin · 龙虾签到台", version="1.0.0", lifespan=_lifespan)

    # ---------- 内部工具 ----------

    def _discovered():
        return scan_scripts(cfg.scripts_path)

    def _visible():
        """仅可见平台：hidden 的子功能平台不进入接入页/概览页展示。"""
        return [s for s in _discovered() if not s.hidden]

    def _health_of(name: str) -> dict | None:
        return state["health"].get(name)

    def _credit_sources_for(name: str) -> list:
        """某可见平台对应的隐藏积分源脚本（workbuddy → workbuddy_credits）。"""
        return [s for s in _discovered()
                if s.hidden and s.name.endswith("_credits")
                and s.name.removesuffix("_credits") == name]

    def _run_credit_sources(name: str, trigger: str = "manual") -> list:
        """补跑隐藏积分源：签到脚本回报的口径有时不是面板要的「总积分」。

        与积分刷新共用一把锁：签到刚入账，这里必须等到在跑的刷新结束再查询，
        才能拿到本次签到后的最新余额（排队最多等一个脚本超时，等不到就跳过，
        交给周期刷新兜底）。
        """
        out = []
        for cs in _credit_sources_for(name):
            if not _refresh_lock.acquire(timeout=cfg.scripts.timeout):
                out.append({"platform": cs.name, "source_for": name, "ok": True,
                            "result": "SKIP", "total_credits": None,
                            "report": "已有积分刷新在跑，本轮跳过（周期刷新会补上）"})
                continue
            try:
                rr = _run_one(cs, trigger=trigger)
            except Exception as e:      # 积分源失败不影响签到结果本身
                out.append({"platform": cs.name, "source_for": name, "ok": False,
                            "result": "ERROR", "total_credits": None, "report": str(e)})
                continue
            finally:
                _refresh_lock.release()
            out.append({"platform": cs.name, "source_for": name, "ok": rr.ok,
                        "result": rr.result, "total_credits": rr.total_credits,
                        "report": rr.report})
        return out

    def _run_one(info, trigger: str = "manual"):
        rr = run_script(info.name, info.path, build_command(info), cfg.scripts.timeout)
        ledger.insert_run(rr, trigger=trigger)
        return rr

    def _health_check(info) -> dict:
        """体检：首跑若是真实签到（CLAIMED），连同 expiring 一起入账，避免丢快照。"""
        h = health_check(info, cfg.scripts.timeout)
        first = h.get("first_run") or {}
        if first.get("result") in ("CLAIMED", "ALREADY"):
            from claw_checkin.runner import RunResult
            fields = {k: v for k, v in first.items() if k in RunResult.__dataclass_fields__}
            ledger.insert_run(RunResult(**fields), trigger="health")
        state["health"][info.name] = h
        return h

    def _expiring_rows(live_only: bool = True):
        """临期快照。live_only=True 只给「今天及以后、接口仍持有」的行——日历与
        总览用它，保证当日格子的数字与平台客户端实时一致；void30 用False 取全量。"""
        return ledger.expiring_snapshots(live_only=live_only)

    def _expiring_for(name: str) -> list:
        """某可见平台的 expiring 记录：按归属组聚合（平台与隐藏积分源同组）。"""
        return [e for e in _expiring_rows() if _expiry_group(e["platform"]) == name]

    def _credit_display(name: str) -> str:
        """expiring 记录归属展示名：隐藏 *_credits → 对应可见平台名。"""
        base = name.removesuffix("_credits")
        return base if any(v.name == base for v in _visible()) else name

    def _expiry_group(name: str) -> str:
        """快照归属组：平台与其隐藏积分源同组，同一笔临期积分只归属一个组。

        _credit_display 回答「显示成谁」，本函数回答「算进谁头上」；
        组名取该组内最新一次写入者，与覆盖式快照更新保持同一口径。
        """
        base = name.removesuffix("_credits")
        return base

    def _overview():
        today = datetime.now().strftime("%Y-%m-%d")
        all_scripts = _discovered()
        scripts = _visible()   # 平台列表只展示可见平台（隐藏子功能不展示）
        latest = ledger.latest_per_platform()
        # 隐藏的积分源平台（如 workbuddy_credits）为对应签到平台提供"总积分"展示口径
        credit_source = {}
        visible_names = {s.name for s in scripts}
        for s in all_scripts:
            if s.hidden and s.name in latest:
                r = latest[s.name]
                if r["result"] in ("CLAIMED", "ALREADY") and r["total_credits"] is not None:
                    base = s.name.removesuffix("_credits")
                    if base in visible_names:
                        credit_source[base] = r["total_credits"]
        platforms = []
        total_credits = 0
        max_streak = 0
        covered = 0          # 返回了 expiring 的平台数（含隐藏平台）
        today_d = datetime.now().date()
        limit_day = (today_d + timedelta(days=14)).strftime("%Y-%m-%d")
        for s in scripts:
            row = latest.get(s.name)
            h = _health_of(s.name)
            status = row["result"] if row else "PENDING"   # 今日未跑
            # 今日已签：今天有该平台的成功记录
            today_ok = bool(row and row["ts"].startswith(today)
                            and row["result"] in ("CLAIMED", "ALREADY"))
            # 积分源余额优先；无积分源时回退签到脚本自己的 total_credits；
            # 两者都没有（如刚清空日志但积分源已刷新）才置 0
            if s.name in credit_source:
                credits = credit_source[s.name]
            elif row and row["total_credits"] is not None:
                credits = row["total_credits"]
            else:
                credits = 0
            streak = row["streak_days"] if row and row["streak_days"] is not None else 0
            total_credits += credits
            max_streak = max(max_streak, streak)
            # 仅统计未来 14 天内到期的积分（与 expiring_detail / 日历窗口口径一致）
            exp_rows = _expiring_for(s.name)
            exp_total = sum(
                e["amount"] for e in exp_rows
                if today <= e["date"] <= limit_day
            )
            # 过期历史行也常驻快照，「即将过期」只看今天及以后
            has_expiring = any(e["date"] >= today for e in exp_rows)
            # 最早到期只看未过期记录：快照可能残留过去日期，不能当作「即将到期」提示
            earliest_expiring = min((e["date"] for e in exp_rows if e["date"] >= today), default="")
            platforms.append({
                **s.to_dict(),
                "status": status,
                "today_ok": today_ok,
                "credits": credits,
                "streak": streak,
                "report": row["report"] if row else "",
                "health": h["overall"] if h else "未体检",
                "expiring_14d": exp_total,
                "has_expiring": has_expiring,
                "earliest_expiring": earliest_expiring,
            })
        # covered 基于可见平台的 expiring 归属（隐藏 *_credits 合并到对应平台）；
        # 只看今天及以后的行——过期历史行不代表平台仍在提供临期数据
        covered = sum(
            1 for s in scripts
            if any(e["date"] >= today for e in _expiring_for(s.name))
        )

        # 14 天临期明细（按归属组去重：平台与其积分源只算一次）
        expiring_detail = []
        week_total = 0
        for e in _expiring_rows():
            if _expiry_group(e["platform"]) not in visible_names:
                continue
            disp_platform = _credit_display(e["platform"])
            if today <= e["date"] <= limit_day:
                days = (datetime.strptime(e["date"], "%Y-%m-%d").date() - today_d).days
                expiring_detail.append({
                    "platform": disp_platform, "date": e["date"],
                    "amount": e["amount"], "source": e["source"] or "签到奖励",
                    "days_left": days,
                })
                week_total += e["amount"]
        expiring_detail.sort(key=lambda x: (x["date"], x["platform"]))

        return {
            "today": today,
            "weekday": "周" + "一二三四五六日"[datetime.now().weekday()],
            "schedule_enabled": cfg.schedule.enabled,
            "schedule_time": cfg.schedule.time,
            "platform_count": len(scripts),
            "done_count": sum(1 for p in platforms if p["today_ok"]),
            "pending_count": len(scripts) - sum(1 for p in platforms if p["today_ok"]),
            "total_credits": total_credits,
            "expiring_14d": week_total,
            "expiring_covered": covered,
            "max_streak": max_streak,
            "platforms": platforms,
            "expiring_detail": expiring_detail,
        }

    # ---------- 页面 ----------

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (WEB_DIR / "index.html").read_text(encoding="utf-8")

    # ---------- API ----------

    @app.get("/api/overview")
    def api_overview():
        try:
            return _overview()
        except Exception as e:
            log.exception("overview 失败")
            raise HTTPException(500, str(e))

    @app.post("/api/scan")
    def api_scan():
        scripts = _visible()
        # 未体检过的新脚本自动跑一次体检
        for s in scripts:
            if s.name not in state["health"]:
                _health_check(s)
        return {"found": len(scripts),
                "platforms": [{**s.to_dict(), "health": state["health"].get(s.name)}
                              for s in scripts]}

    @app.get("/api/platforms")
    def api_platforms():
        scripts = _visible()
        return [{
            **s.to_dict(),
            "health": state["health"].get(s.name),
        } for s in scripts]

    @app.post("/api/health/{name}")
    def api_health(name: str):
        for s in _discovered():
            if s.name == name:
                return _health_check(s)
        raise HTTPException(404, f"未发现平台脚本：{name}")

    @app.post("/api/run/{name}")
    def api_run(name: str):
        for s in _discovered():
            if s.name == name:
                rr = _run_one(s, trigger="manual")
                # 签到后补跑对应积分源，卡片上的总积分跟着刷新
                extra = _run_credit_sources(s.name, trigger="manual")
                return {**rr.to_dict(), "credit_sources": extra}
        raise HTTPException(404, f"未发现平台脚本：{name}")

    @app.post("/api/run_all")
    def api_run_all():
        # 一键签到只统计可见平台：hidden 的积分源子功能（如 workbuddy_credits）
        # 不展示在平台接入页，不计入签到统计；但要在跑完可见脚本之后补跑，
        # 否则「当前累计积分」会停在昨天的值
        results = []
        for s in _visible():
            results.append(_run_one(s, trigger="manual").to_dict())
        credit_sources = []
        for s in _visible():
            credit_sources.extend(_run_credit_sources(s.name, trigger="manual"))
        return {"ran": len(results), "results": results, "credit_sources": credit_sources}

    @app.get("/api/expiring")
    def api_expiring():
        """未来 28 天过期日历。"""
        today = datetime.now()
        days = []
        exp_map: dict[str, list] = {}
        visible_names = {s.name for s in _visible()}
        for e in _expiring_rows():
            group = _expiry_group(e["platform"])
            if group not in visible_names:
                continue
            exp_map.setdefault(e["date"], []).append(
                {"platform": _credit_display(e["platform"]), "amount": e["amount"], "source": e["source"] or "签到奖励"}
            )
        weeks = [0, 0, 0, 0]      # 第 1~4 周到期合计
        void30 = 0
        for i in range(28):
            d = today + timedelta(days=i)
            ds = d.strftime("%Y-%m-%d")
            items = exp_map.get(ds, [])
            amount = sum(x["amount"] for x in items)
            weeks[min(i // 7, 3)] += amount
            days.append({
                "date": ds,
                "weekday": "周" + "一二三四五六日"[d.weekday()],
                "amount": amount,
                "items": items,
            })
        # 过去 30 天已作废（与未来日历分开：取全量历史行，但只计「到期日结束时
        # 平台仍持有」的行，即存储 live=1；live=0 是到期前被花掉的消耗流水，
        # 接口当天不再回报即判为已用，不计入作废）
        cutoff = (today - timedelta(days=30)).strftime("%Y-%m-%d")
        for e in _expiring_rows(live_only=False):
            if _expiry_group(e["platform"]) not in visible_names:
                continue
            if cutoff <= e["date"] < today.strftime("%Y-%m-%d") \
                    and int(e.get("live", 1) or 0) == 1:
                void30 += e["amount"]
        return {
            "weeks": weeks, "void30": void30,
            "days": days,
        }

    @app.get("/api/logs")
    def api_logs(filter_name: str = "all", limit: int = 10, offset: int = 0):
        total = ledger.logs_count(filter_name)
        rows = ledger.logs(filter_name, limit, offset)
        return {"total": total, "count": len(rows), "logs": [dict(r) for r in rows]}

    @app.get("/api/logs/export")
    def api_logs_export():
        csv_text = ledger.logs_csv()
        return PlainTextResponse(
            "\ufeff" + csv_text,        # BOM 便于 Excel 识别中文
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=claw-checkin_logs.csv"},
        )

    @app.post("/api/logs/clear")
    def api_logs_clear():
        """清空运行日志（仅 checkin_log 表；过期日历快照不受影响）。"""
        deleted = ledger.clear_logs()
        log.info("运行日志已清空，共删除 %s 条", deleted)
        return {"ok": True, "deleted": deleted}

    @app.post("/api/refresh_credits")
    def api_refresh_credits(source: str = "button"):
        """补跑全部积分源（纯查询、不签到），刷新卡片上的积分余额。

        source=page_load：前端整页加载时自动触发，防抖窗口内（以及已有刷新
        在跑时）跳过并如实告知，前端保持当前数据、交给轮询兜底；
        source=button（右上角按钮）：用户显式动作，只做并发保护，不防抖。
        """
        trigger = "page_load" if source == "page_load" else "manual"
        results = _refresh_credits_once(trigger)
        if results is None:
            return {"ran": 0, "ok": 0, "results": [], "skipped": True}
        return {"ran": len(results), "ok": sum(1 for r in results if r["ok"]),
                "results": results, "skipped": False}

    @app.post("/api/refresh/{name}")
    def api_refresh(name: str):
        """刷新平台的 Token（如 AutoClaw 从 openclaw.json 提取 JWT）。"""
        import subprocess, json as _json
        for s in _discovered():
            if s.name == name:
                cmd = ["python", str(s.path), "--refresh"]
                try:
                    proc = subprocess.run(cmd, capture_output=True, text=True,
                                          timeout=30, encoding="utf-8", errors="replace",
                                          cwd=str(s.path.parent))
                    data = _json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
                    return {"ok": proc.returncode == 0, "report": data.get("report", "刷新完成")}
                except Exception as e:
                    return {"ok": False, "report": str(e)}
        raise HTTPException(404, f"未发现平台脚本：{name}")

    # ---------- 静态资源与启动 ----------

    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    return app


def main():
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    cfg = load_config()
    app = create_app(cfg)
    print(f"\n🦞 龙虾签到台已启动：http://{cfg.server.host}:{cfg.server.port}\n"
          f"   脚本目录：{cfg.scripts_path}\n"
          f"   每日自动签到：{'开启 ' + cfg.schedule.time if cfg.schedule.enabled else '关闭'}\n")
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level="warning")


if __name__ == "__main__":
    main()
