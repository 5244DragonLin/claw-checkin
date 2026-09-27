"""ClawCheckin · 龙虾签到台 —— 启动入口

用法：
    python main.py                 # 读取 config.yaml（缺省用 config.example.yaml 默认值）
    python main.py --port 9000     # 命令行覆盖端口
    python main.py --scripts DIR   # 指定脚本目录
    python main.py --no-schedule   # 本次启动关闭定时调度
    python main.py --run-once      # 纯签到模式：跑完即退（配合任务计划）
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from claw_checkin.config import load_config  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser(description="ClawCheckin · 龙虾签到台")
    p.add_argument("--config", default=None, help="配置文件路径（默认自动读取 config.yaml）")
    p.add_argument("--host", default=None, help="监听地址")
    p.add_argument("--port", type=int, default=None, help="监听端口")
    p.add_argument("--scripts", default=None, help="签到脚本目录")
    p.add_argument("--no-schedule", action="store_true", help="关闭每日定时调度")
    p.add_argument("--run-once", action="store_true",
                   help="纯签到模式：签到所有平台并补跑积分源后退出（配合任务计划使用）")
    p.add_argument("--reload", action="store_true",
                   help="开发模式：源码变更自动热重载（需安装 watchfiles）")
    return p.parse_args()


def _ensure_watchfiles():
    try:
        import watchfiles  # noqa: F401
    except ImportError:
        print("[ClawCheckin] --reload 需要 watchfiles，正在安装…")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install",
                                   "-i", "https://pypi.org/simple/",
                                   "--trusted-host", "pypi.org", "watchfiles"])
        except subprocess.CalledProcessError:
            print("[ClawCheckin] watchfiles 安装失败，请手动执行：python -m pip install watchfiles")
            sys.exit(1)


def _start_reload(cfg):
    """uvicorn 原生热重载：必须用 import 字符串 + factory 模式（传实例不支持 reload）。

    命令行参数（host/port/scripts/no-schedule）通过环境变量透传给 factory，
    create_app() 无参调用时由 _load_config_with_env() 读取恢复。
    """
    import os
    _ensure_watchfiles()
    os.environ["CLAWDESK_HOST"] = str(cfg.server.host)
    os.environ["CLAWDESK_PORT"] = str(cfg.server.port)
    os.environ["CLAWDESK_SCRIPTS_DIR"] = str(cfg.scripts.dir)
    if not cfg.schedule.enabled:
        os.environ["CLAWDESK_NO_SCHEDULE"] = "1"
    import uvicorn
    uvicorn.run("claw_checkin.main:create_app", factory=True, reload=True,
                host=cfg.server.host, port=cfg.server.port, log_level="warning")


def _run_once(cfg):
    """纯签到模式：不启面板，跑完所有平台与积分源即退出（供任务计划调用）。

    trigger 记为 schedule：面板内置调度器按当日 schedule 记录判重，
    即使二者都启用也不会重复签到。
    """
    from claw_checkin.ledger import Ledger
    from claw_checkin.runner import run_script
    from claw_checkin.scanner import build_command, scan_scripts

    ledger = Ledger(cfg.db_path)
    scripts = scan_scripts(cfg.scripts_path)
    visible = [s for s in scripts if not s.hidden]
    print(f"[ClawCheckin] 纯签到模式：{len(visible)} 个平台")
    failed = []
    queue = visible + [x for x in scripts if x.hidden]
    for idx, s in enumerate(queue):
        if visible and idx == len(visible):
            print()  # 平台签到与积分源检查之间空一行分隔
        rr = run_script(s.name, s.path, build_command(s), cfg.scripts.timeout)
        ledger.insert_run(rr, trigger="schedule")
        print(f"[ClawCheckin] {s.name} → {rr.result} {rr.report}")
        if not rr.ok:
            failed.append(s.name)
    ledger.cleanup(cfg.ledger.retention_days, cfg.ledger.snapshot_retention_days)
    if failed:
        print(f"[ClawCheckin] 失败：{'、'.join(failed)}")
        return 1
    print("[ClawCheckin] 全部完成")
    return 0


def main():
    args = parse_args()
    cfg = load_config(args.config)
    if args.host:
        cfg.server.host = args.host
    if args.port:
        cfg.server.port = args.port
    if args.scripts:
        cfg.scripts.dir = args.scripts
    if args.no_schedule:
        cfg.schedule.enabled = False

    if args.run_once:
        return _run_once(cfg)

    if args.reload:
        _start_reload(cfg)
        return

    import threading
    import webbrowser
    import uvicorn
    from claw_checkin.main import create_app

    app = create_app(cfg)
    url = f"http://{cfg.server.host}:{cfg.server.port}"
    print(
        f"\n🦞 龙虾签到台已启动：{url}\n"
        f"   脚本目录：{cfg.scripts_path}（把签到脚本丢进来即被发现）\n"
        f"   每日自动签到：{'开启 ' + cfg.schedule.time if cfg.schedule.enabled else '关闭'}\n"
    )
    # 等服务完成监听后自动打开浏览器
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level="warning")


if __name__ == "__main__":
    sys.exit(main())
