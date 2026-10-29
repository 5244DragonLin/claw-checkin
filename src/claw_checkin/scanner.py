"""脚本目录扫描：文件名即平台名，丢进 scripts/ 即被发现"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# 支持的脚本扩展名 → （展示名, 执行器）
EXT_EXECUTORS = {
    ".py": ("Python", "python"),
    ".js": ("Node", "node"),
    ".mjs": ("Node", "node"),
    ".sh": ("Shell", "bash"),
    ".bat": ("Batch", "cmd"),
    ".cmd": ("Batch", "cmd"),
    ".ps1": ("PowerShell", "powershell"),
}


@dataclass
class ScriptInfo:
    name: str            # 平台名 = 文件名（去扩展名）
    path: Path
    executor: str        # python / node / bash / cmd / powershell
    executor_label: str  # 展示用执行器名称
    ext: str
    hidden: bool = False # 隐藏平台（子功能），不出现在接入页/概览页
    probe: bool = False  # 支持 `--status` 只读查询今日是否已签

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "script": self.path.name,
            "executor": self.executor,
            "executor_label": self.executor_label,
            "hidden": self.hidden,
            "probe": self.probe,
        }


def _load_meta(scripts_dir: Path) -> dict:
    meta = scripts_dir / "_meta.json"
    if not meta.is_file():
        return {}
    try:
        doc = json.loads(meta.read_text(encoding="utf-8"))
    except Exception:
        return {}   # 解析失败按空配置处理，不阻断扫描
    return doc if isinstance(doc, dict) else {}


def scan_scripts(scripts_dir: Path) -> list[ScriptInfo]:
    """扫描目录，返回按平台名排序的脚本列表。子目录不递归。"""
    if not scripts_dir.is_dir():
        return []
    # 读取隐藏平台清单（下划线开头文件本身不会被注册为平台）
    meta = _load_meta(scripts_dir)
    hidden: set[str] = set(meta.get("hidden", []) or [])
    # 支持 --status 只读核查的平台清单。必须显式登记：脚本不认识 --status 时
    # 通常会把参数忽略掉、照常签到，自动探测等于替用户多点一次签到
    probe: set[str] = set(meta.get("status_probe", []) or [])
    found: list[ScriptInfo] = []
    for f in sorted(scripts_dir.iterdir()):
        if not f.is_file() or f.name.startswith((".", "_")):
            continue
        ext = f.suffix.lower()
        if ext not in EXT_EXECUTORS:
            continue
        label, executor = EXT_EXECUTORS[ext]
        found.append(ScriptInfo(
            name=f.stem,
            path=f,
            executor=executor,
            executor_label=f"{label} · {f.name}",
            ext=ext,
            hidden=f.stem in hidden,
            probe=f.stem in probe,
        ))
    found.sort(key=lambda s: s.name.lower())
    return found


def build_command(info: ScriptInfo, status: bool = False) -> list[str]:
    """构造执行命令。status=True 时追加 `--status`（只读查询，不签到）。"""
    ex = info.executor
    suffix = ["--status"] if status else []
    if ex == "python":
        return ["python", str(info.path)] + suffix
    if ex == "node":
        return ["node", str(info.path)] + suffix
    if ex == "bash":
        return ["bash", str(info.path)] + suffix
    if ex == "cmd":
        return ["cmd", "/c", str(info.path)] + suffix
    if ex == "powershell":
        return (["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(info.path)] + suffix)
    return [str(info.path)] + suffix
