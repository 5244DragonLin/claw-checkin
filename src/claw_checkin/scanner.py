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

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "script": self.path.name,
            "executor": self.executor,
            "executor_label": self.executor_label,
            "hidden": self.hidden,
        }


def scan_scripts(scripts_dir: Path) -> list[ScriptInfo]:
    """扫描目录，返回按平台名排序的脚本列表。子目录不递归。"""
    if not scripts_dir.is_dir():
        return []
    # 读取隐藏平台清单（下划线开头文件本身不会被注册为平台）
    hidden: set[str] = set()
    meta = scripts_dir / "_meta.json"
    if meta.is_file():
        try:
            hidden = set(json.loads(meta.read_text(encoding="utf-8")).get("hidden", []) or [])
        except Exception:
            hidden = set()  # 解析失败按空集处理，不阻断扫描
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
        ))
    found.sort(key=lambda s: s.name.lower())
    return found


def build_command(info: ScriptInfo) -> list[str]:
    """构造执行命令。"""
    ex = info.executor
    if ex == "python":
        return ["python", str(info.path)]
    if ex == "node":
        return ["node", str(info.path)]
    if ex == "bash":
        return ["bash", str(info.path)]
    if ex == "cmd":
        return ["cmd", "/c", str(info.path)]
    if ex == "powershell":
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(info.path)]
    return [str(info.path)]
