"""同步盘友好的文件写入原语（供 scripts/ 下各脚本共享）。

项目随同步盘在两台电脑间轮流使用，同步盘随时可能抓取文件做上传/下载；
「先清空再写入」式的普通写法会让同步盘在写入窗口拿到半截内容，落到另一台
电脑就是损坏文件（data/ 下的 *_冲突文件_* 就是这么来的）。

本模块统一用「同目录临时文件 + os.replace」原子替换：任一时刻磁盘上的完整
文件都可解析；目标被同步盘短暂占用时自动重试几次，仍失败则放弃本次写盘
（内存态不丢，等下一次写机会），绝不抛异常打断签到主流程。
"""

from __future__ import annotations

import json
import os
import tempfile
import time

# 同步盘占用目标文件时 os.replace 会报 PermissionError，重试窗口内基本都能等到释放
_REPLACE_ATTEMPTS = 4
_REPLACE_RETRY_SECONDS = 0.25


def atomic_write_text(path: str, text: str) -> bool:
    """原子写入文本（UTF-8），成功返回 True；任何失败都不抛异常。"""
    try:
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=os.path.basename(path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            for attempt in range(_REPLACE_ATTEMPTS):
                try:
                    os.replace(tmp, path)
                    return True
                except PermissionError:
                    if attempt == _REPLACE_ATTEMPTS - 1:
                        raise
                    time.sleep(_REPLACE_RETRY_SECONDS)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return True
    except OSError:
        return False


def atomic_write_json(path: str, data) -> bool:
    """原子写入 JSON（ensure_ascii=False，缩进 2，便于人肉查看与 diff）。"""
    return atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def load_json(path: str, default=None):
    """宽容读取 JSON：文件缺失/被占用/损坏一律返回 default。"""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default
