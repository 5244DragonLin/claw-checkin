"""示例脚本：不返回 expiring 的平台

演示「缺失即隐藏」规则：平台不返回积分有效期时，
该平台自动排除在过期统计之外，相关板块不报错、不占位。
"""

import json
import sys
from datetime import date
from pathlib import Path

STATE_DIR = Path(__file__).resolve().parent.parent / "data" / "mock_state"


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    marker = STATE_DIR / "example_b.json"
    today = date.today().isoformat()

    state = {}
    if marker.exists():
        try:
            state = json.loads(marker.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            state = {}

    if state.get("last_claim_day") == today:
        print(json.dumps({
            "result": "ALREADY",
            "report": "今日已签到，跳过",
            "today_credit": 0,
            "streak_days": state.get("streak_days", 1),
            "total_credits": state.get("total_credits", 50),
        }, ensure_ascii=False))
        return

    streak = state.get("streak_days", 0) + 1
    total = state.get("total_credits", 0) + 50
    marker.write_text(json.dumps({
        "last_claim_day": today, "streak_days": streak, "total_credits": total,
    }, ensure_ascii=False), encoding="utf-8")

    print(json.dumps({
        "result": "CLAIMED",
        "report": f"签到成功 +50（连续 {streak} 天）",
        "today_credit": 50,
        "streak_days": streak,
        "total_credits": total,
        # 注意：没有 expiring 字段 → 不参与过期统计
    }, ensure_ascii=False))


if __name__ == "__main__":
    sys.exit(main())
