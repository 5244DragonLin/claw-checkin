"""示例脚本：模拟一个「龙虾」平台的每日签到

演示输出契约的完整用法：
- 首次运行返回 CLAIMED + expiring 字段（参与过期统计）
- 当天再次运行返回 ALREADY，且 total_credits 不变（幂等）
- 状态文件写在 data/mock_state/ 下，模拟「当天已签」

把本文件复制进 scripts/ 目录即可在面板中看到效果。
"""

import json
import sys
from datetime import date, timedelta
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent.parent / "data" / "mock_state"
PLATFORM = "示例平台A"


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    marker = STATE_DIR / "example_a.json"
    today = date.today().isoformat()

    state = {}
    if marker.exists():
        try:
            state = json.loads(marker.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            state = {}

    if state.get("last_claim_day") == today:
        # 今天已签 → ALREADY，积分不变
        print(json.dumps({
            "result": "ALREADY",
            "report": "今日签到积分已到账，跳过",
            "today_credit": 0,
            "streak_days": state.get("streak_days", 1),
            "total_credits": state.get("total_credits", 100),
        }, ensure_ascii=False))
        return

    # 首签：累计 +100，连续 +1
    streak = state.get("streak_days", 0) + 1
    total = state.get("total_credits", 0) + 100
    marker.write_text(json.dumps({
        "last_claim_day": today, "streak_days": streak, "total_credits": total,
    }, ensure_ascii=False), encoding="utf-8")

    # 临期积分示例：昨天签的 100 分 3 天后过期，今日签的 7 天后过期
    expiring = [
        {"date": (date.today() + timedelta(days=3)).isoformat(), "amount": 100, "source": "每日签到"},
        {"date": (date.today() + timedelta(days=6)).isoformat(), "amount": 50, "source": "连续签到奖励"},
    ]
    print(json.dumps({
        "result": "CLAIMED",
        "report": f"成功领取 100 积分（连续 {streak} 天）",
        "today_credit": 100,
        "streak_days": streak,
        "total_credits": total,
        "expiring": expiring,
    }, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
