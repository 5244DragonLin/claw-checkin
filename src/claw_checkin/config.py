"""配置加载：内置默认值 → config.example.yaml → config.yaml → 命令行参数"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8317


@dataclass
class ScriptsConfig:
    dir: str = "scripts"          # 签到脚本目录，相对项目根
    timeout: int = 120            # 单脚本执行超时（秒）


@dataclass
class ScheduleConfig:
    enabled: bool = True
    time: str = "00:05"           # 每日自动签到时间 HH:MM


@dataclass
class CreditsConfig:
    refresh_interval: int = 300   # 积分余额自动刷新间隔（秒），0 关闭；仅补跑隐藏 *_credits 积分源
    page_load_debounce: int = 30  # 页面加载触发刷新的防抖窗口（秒），防 F5 连点；0 关闭防抖


@dataclass
class LedgerConfig:
    db: str = "data/ledger.json"  # 账本（单文件 JSON，同步盘友好；旧 .db 配置自动兼容）
    retention_days: int = 90      # 日志本地保留天数
    snapshot_retention_days: int = 31  # 临期积分快照保留天数


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = ""


@dataclass
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    scripts: ScriptsConfig = field(default_factory=ScriptsConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    credits: CreditsConfig = field(default_factory=CreditsConfig)
    ledger: LedgerConfig = field(default_factory=LedgerConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    @property
    def scripts_path(self) -> Path:
        p = Path(self.scripts.dir)
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def db_path(self) -> Path:
        p = Path(self.ledger.db)
        return p if p.is_absolute() else PROJECT_ROOT / p


_DEFAULTS = {
    "server": {"host": "127.0.0.1", "port": 8317},
    "scripts": {"dir": "scripts", "timeout": 120},
    "schedule": {"enabled": True, "time": "00:05"},
    "credits": {"refresh_interval": 300, "page_load_debounce": 30},
    "ledger": {"db": "data/ledger.json", "retention_days": 90,
               "snapshot_retention_days": 31},
    "logging": {"level": "INFO", "file": ""},
}


def _apply(dc, section: dict):
    for k, v in section.items():
        if hasattr(dc, k) and v is not None:
            setattr(dc, k, v)


def load_config(config_file: str | None = None) -> AppConfig:
    cfg = AppConfig()
    candidates = []
    if config_file:
        candidates.append(Path(config_file))
        if not Path(config_file).is_absolute():
            candidates.insert(0, PROJECT_ROOT / config_file)
    candidates.append(PROJECT_ROOT / "config.yaml")
    candidates.append(PROJECT_ROOT / "config.example.yaml")

    merged: dict = {}
    for path in candidates:
        if path.is_file():
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            for section, values in data.items():
                if isinstance(values, dict):
                    merged.setdefault(section, {}).update(values)
                else:
                    merged[section] = values

    for section, values in merged.items():
        dc = getattr(cfg, section, None)
        if dc is not None and isinstance(values, dict):
            _apply(dc, values)

    # 环境变量兜底（CLAWDESK_*），便于容器化部署；与 --reload 路径共用同一套名字
    if os.environ.get("CLAWDESK_HOST"):
        cfg.server.host = os.environ["CLAWDESK_HOST"]
    if os.environ.get("CLAWDESK_PORT"):
        cfg.server.port = int(os.environ["CLAWDESK_PORT"])
    if os.environ.get("CLAWDESK_SCRIPTS_DIR"):
        cfg.scripts.dir = os.environ["CLAWDESK_SCRIPTS_DIR"]
    elif os.environ.get("CLAWDESK_SCRIPTS"):      # 旧名兼容
        cfg.scripts.dir = os.environ["CLAWDESK_SCRIPTS"]
    if os.environ.get("CLAWDESK_NO_SCHEDULE") == "1":
        cfg.schedule.enabled = False

    # 确保运行时目录存在
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.scripts_path.mkdir(parents=True, exist_ok=True)
    return cfg
