"""库库AI（百度 GenFlow Pro）每日签到 · ClawCheckin 接入脚本

协议参考：https://github.com/xinxinshuhao-create/kuku2api
（网页端 freepoint 任务链，2026-09 实测；本仓接入时按真实账号复核）

── 库库的每日积分长什么样 ───────────────────────────────────
没有传统「签到接口」。每日积分是「免费领积分」活动里的任务：

  POST /api/genflowpro/freepoint/taskComplete  body form: task_type=LOGIN
   → {complete_status: "SUCCESS", reward_point: 50}    ← 每日登录（=签到）
  POST 同上 task_type=CHAT
   → 「完成一次对话」奖励（当天在网页/客户端聊过一句就能领）

判「真领到」只看 complete_status=SUCCESS 且 reward_point>0。已领过的任务
重复领取幂等（BADGE_ALREADY_RECEIVED / reward_point=0），不会重复加分。

── token 自动续期（本平台的核心设计）───────────────────────
鉴权分两层，续期全自动，无人工干预窗口：
  · Cookie（BDUSS 等百度通行证）＝根凭据，长期有效（年）；
  · bdstoken/uinfo/uk 三件套＝短时效会话凭据，每次签到前用 userreport
    现换一把 —— 换不动（Cookie 失效/被顶下线）才报 NO_AUTH 提示重导。
Cookie 从哪来：`python scripts/kuku.py --login` 起本地引导页照着做，
或 `--cookie "BDUSS=...; STOKEN=..."` 直接写入；**本机装了库库客户端并登录过
的话什么都不用做**——客户端退出后脚本直接从它的数据目录读登录态（运行中
被锁则如实提示，下次客户端退出后自动恢复）。Cookie 永不写日志。

── 为什么把 CHAT 一起领 ────────────────────────────────────
「每日登录 +50 / 完成一次对话 +50」同属每日任务，且 CHAT 的领取不触发
对话（只是把当天已经发生过的对话领奖），幂等无副作用。领不到也不算失败
（当天没聊过天就没有这笔）——报告里如实分开写。

输出契约（末行 JSON，与其余平台一致）：
  {
    "result": "CLAIMED|ALREADY|NETWORK|ERROR|NO_AUTH",
    "report": "可读汇报",
    "today_credit": 50,
    "streak_days": 0,
    "total_credits": 0
  }

`streak_days`：上游没有连续签到天数字段（任务面板只有当日状态），天数在
本地累计（与 AutoClaw / Qoder / 小浣熊同口径）：每确认一天领取 +1、同日
不双计。口径是「本地有记录的领取天数」——在网页/客户端领的日子，只有
脚本当天读到 SUCCESS/已领状态才会计入。

`--status`：只读查询任务面板看 LOGIN 是否已完成（task_status FINISHED），
不发领取请求、不领积分。面板打开/切回时用它承认「你在网页端已经领过」。
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _kuku_sync as kd  # noqa: E402  同目录共享模块：凭据 / 请求层 / 任务链
import _syncio  # noqa: E402

STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "kuku_state.json")


def _out(result: str, report: str, today: int = 0, total: int | None = 0,
         streak: int = 0, signed_today: bool | None = None) -> dict:
    out = {
        "result": result,
        "report": report,
        "today_credit": int(today),
        "streak_days": int(streak),
        # 余额读不到给 null：面板回退上一次已知值，不写 0 冒充「没有积分」
        "total_credits": int(total) if total is not None else None,
    }
    if signed_today is not None:
        out["signed_today"] = bool(signed_today)
    return out


def _load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as handle:
            doc = json.load(handle)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        _syncio.atomic_write_json(STATE_PATH, state)
    except Exception:
        pass


def _streak_days(state: dict) -> int:
    """本地累计领取天数（上游没有该字段，与 AutoClaw/Qoder/小浣熊同口径）。

    首次从运行日志回填历史（platform=kuku 且 CLAIMED 的日期），去掉「今天」
    的部分（今天那份由确认逻辑记，避免双计）。之后只增不减。
    """
    if "streak_days" in state:
        return int(state["streak_days"] or 0)
    days: set[str] = set()
    try:
        ledger_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "data", "ledger.json")
        with open(ledger_path, encoding="utf-8") as handle:
            doc = json.load(handle)
        for row in doc.get("checkin_log") or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("platform") or "") != "kuku":
                continue
            if str(row.get("result") or "") != "CLAIMED":
                continue
            day = str(row.get("ts") or "")[:10]
            if len(day) == 10 and day[4] == "-" and day[7] == "-":
                days.add(day)
    except Exception:
        pass  # 台账读不到就从 0 起数，不影响签到本身
    streak = len(days)
    if time.strftime("%Y-%m-%d") in days:
        streak -= 1  # 今天那份由下面的确认逻辑记，不在这里预支
    if state.get("last_date") == time.strftime("%Y-%m-%d"):
        streak += 1
    state["streak_days"] = streak
    return streak


def _confirm_day(state: dict) -> int:
    """确认今天确有领取（自己领到 / 上游确认今天已领）：累计 +1，每天只记一次。"""
    _streak_days(state)  # 确保历史已回填
    today = time.strftime("%Y-%m-%d")
    if state.get("last_date") != today:
        state["streak_days"] = int(state.get("streak_days") or 0) + 1
        state["last_date"] = today
        _save_state(state)
    return int(state.get("streak_days") or 0)


def _session(creds: dict) -> tuple[dict | None, dict]:
    """换取三件套。返回 (trio, 契约行)；trio=None 时契约行已带失败结论。"""
    trio, status, err = kd.fetch_trio(creds)
    if trio is None:
        if kd.is_auth_reject(status, err):
            return None, _out("NO_AUTH",
                              "库库登录态已失效（Cookie 过期或账号在别处重新登录），"
                              "运行 `python scripts/kuku.py --login` 重新导一次 Cookie")
        if status in (-1, 429, 502, 503, 504):
            return None, _out("NETWORK", f"会话刷新失败：{err or status}")
        return None, _out("ERROR", f"会话刷新失败：{err or status}")
    return trio, {}


def _login_done(panel: dict | None) -> bool | None:
    """任务面板里 LOGIN 任务今天是否已完成。面板读不到返回 None（不猜）。

    task_status 的实测口径：FINISHED=已完成可提交、可领取=已完成待领取。
    只把「明确完成」当 True；其余（含面板缺这块数据）一律 None。
    """
    if not isinstance(panel, dict):
        return None
    for act in panel.get("activities") or []:
        if not isinstance(act, dict):
            continue
        for tab in act.get("tabs") or []:
            if not isinstance(tab, dict):
                continue
            for task in tab.get("tasks") or []:
                if not isinstance(task, dict):
                    continue
                if str(task.get("task_type") or "") != kd.TASK_LOGIN:
                    continue
                status = str(task.get("task_status") or "").upper()
                if status in ("FINISHED", "COMPLETED", "REWARD_GET", "REWARDED"):
                    return True
                if status in ("DOING", "PROCESSING", "UNFINISHED", "NOT_START"):
                    return False
    return None


def _run() -> dict:
    creds = kd.resolve_credentials()
    if creds is None:
        # 无文件凭据时给客户端链路的精确原因（在跑/没装/没登录），比笼统
        # 「未找到」更有用——面板上用户照着做就能通
        _client_creds, client_reason = kd.read_cookies_from_client()
        if client_reason:
            return _out("NO_AUTH", f"库库登录态不可用：{client_reason}。"
                        "也可以在已登录 kuku.baidu.com 的浏览器里导 Cookie："
                        "python scripts/kuku.py --login")
        where = "、".join(label for label, _path in kd.credential_sources())
        return _out("NO_AUTH",
                    f"未找到库库 Cookie（{where}、库库客户端、环境变量均无）。"
                    "运行一次 `python scripts/kuku.py --login` 按引导导出即可")

    state = _load_state()
    streak = _streak_days(state)
    _save_state(state)  # 首次回填的天数落盘，之后只增不减

    trio, blocked = _session(creds)
    if trio is None:
        blocked["streak_days"] = streak
        return blocked

    # ── 主签到：30 天连签活动（kukuPcSignAct，2026-11 真机打通）─────────
    # 反爬两层都可纯 HTTP 复刻：jsToken（活动页内联注入）+ jt 降级签名（HTJ
    # SDK 的合法降级路径，服务端照常收）。活动实例 tid 由 kukuSignConf 下发。
    activity_report, activity_claimed = _signin_activity(creds, trio, state)
    if activity_claimed is True:
        streak = _confirm_day(state)
        return _out("CLAIMED", activity_report, today=0,
                    total=_read_balance(creds), streak=streak)
    if activity_claimed == "already":
        # 上游确认今天已签（homepage 格子/幂等码）：如实报 ALREADY
        streak = _confirm_day(state)
        return _out("ALREADY", activity_report, total=_read_balance(creds),
                    streak=streak, signed_today=True)
    if activity_claimed is False and activity_report:
        # 明确失败（网络/上游变动）：如实报错，不再走 freepoint 兜底混淆视听
        return _out("ERROR", activity_report, total=None, streak=streak)

    # ── 兜底：freepoint 每日任务（旧活动家族，活动期才有数据；幂等）────
    freepoint_report, freepoint_total = _signin_freepoint(creds, trio)
    if freepoint_report:
        if freepoint_total > 0:
            streak = _confirm_day(state)
            return _out("CLAIMED", freepoint_report, today=freepoint_total,
                        total=None, streak=streak)
        return _out("ALREADY", freepoint_report, total=None, streak=streak)

    # 两条链都没给出结论（当前无活动 + 连签配置拿不到）：中性报告
    return _out("ALREADY", "库库当前没有可领取的签到活动", total=None, streak=streak)


def _read_balance(creds: dict) -> int | None:
    """签到链顺带读一次积分余额（bizapi，与积分源同源）。读不到返回 None。"""
    try:
        data, _status, _err = kd.point_balance(creds)
        balance = kd.parse_balance(data)
        return balance["total"] if balance else None
    except Exception:
        return None


def _signin_activity(creds: dict, trio: dict, state: dict) -> tuple[str, str | None]:
    """连签活动链：jsToken → conf(tid) → homepage 判状态 → signin → 顺手抽奖。

    返回 (报告, 结论)：True=本次签到成功；"already"=今天已签；False=明确失败
    （报告含原因）；None=活动不在期/拿不到配置（静默转 freepoint 兜底）。
    抽奖在任何签到结论后都尝试（签到 +1 次、任务 +N 次，攒着浪费；抽完为止）。
    """
    js_token, status, err = kd.fetch_js_token(creds)
    if js_token is None:
        return f"连签活动页获取失败：{err or status}", False

    conf, status, err = kd.fetch_sign_conf(creds, trio, js_token)
    if conf is None:
        # 活动不在期时 conf 可能返回空值：静默转兜底，不算失败
        return "", None
    tid = str(conf.get("activityTid") or "").strip()
    if not tid:
        return "", None

    homepage = kd.sign_homepage(creds, trio, js_token, tid)
    done = kd.sign_today_status(homepage)
    if done is True:
        report = "库库连签：今日已签到"
        lottery_report = _do_lottery(creds, trio, js_token, str(conf.get("lotteryTid") or ""))
        if lottery_report:
            report += "；" + lottery_report
        return report, "already"
    if done is None:
        return "库库连签：活动状态读取失败（日历没有今天格子）", False

    data, status, err = kd.activity_signin(creds, trio, js_token, tid)
    if data is None:
        # 幂等码 10152 = already signed：实际是已签（并发/时序边缘），按已签口径
        if f"errno={kd.ALREADY_SIGNED_ERRNO}" in (err or ""):
            report = "库库连签：今日已签到（上游报重复，实为已签）"
            lottery_report = _do_lottery(creds, trio, js_token, str(conf.get("lotteryTid") or ""))
            if lottery_report:
                report += "；" + lottery_report
            return report, "already"
        return f"库库连签签到失败：{err or status}", False

    continuous = int(data.get("continuous") or 0)
    end_time = str(conf.get("endTime") or "").strip()
    report = f"库库连签签到成功（连续 {continuous} 天）"
    lottery_report = _do_lottery(creds, trio, js_token, str(conf.get("lotteryTid") or ""))
    if lottery_report:
        report += "；" + lottery_report
    if end_time:
        report += f"，活动 {end_time} 结束"
    return report, True


def _do_lottery(creds: dict, trio: dict, js_token: str, lottery_tid: str) -> str:
    """把当前抽奖次数抽完（每次一发，之间小睡防频控）。返回可读报告，空=没抽。

    抽奖不产生积分，奖品（网盘空间/会员卡等）直接发放；次数为 0 或读不到
    时不报错——抽奖只是签到的顺手动作，失败不值得打断主结论。
    """
    if not lottery_tid:
        return ""
    count = kd.lottery_count(creds, trio, js_token, lottery_tid)
    if not count or count <= 0:
        return ""
    prizes: list[str] = []
    import time as _time
    for _ in range(count):
        titles, _status, _err = kd.lottery_commit(creds, trio, js_token, lottery_tid)
        if titles:
            prizes.extend(titles)
        else:
            break             # 一次失败（频控/次数耗尽）就停，不硬刷
        if count > 1:
            _time.sleep(1.0)
    if not prizes:
        return ""
    return "抽奖 " + "、".join(prizes)


def _signin_freepoint(creds: dict, trio: dict) -> tuple[str, int]:
    """freepoint 每日任务链（旧活动家族）。返回 (报告, 本次入账)。"""
    claimed_total = 0
    fresh_tasks: list[str] = []
    already_tasks: list[str] = []
    errors: list[str] = []
    for task_type in (kd.TASK_LOGIN, kd.TASK_CHAT):
        data, status, err = kd.complete_task(creds, trio, task_type)
        if data is None:
            if kd.is_auth_reject(status, err):
                return "", 0
            errors.append(f"{kd.TASK_NAMES.get(task_type, task_type)}：{err or status}")
            continue
        status_text, reward = kd.parse_claim(data)
        if status_text == kd.STATUS_SUCCESS and reward > 0:
            claimed_total += reward
            fresh_tasks.append(f"{kd.TASK_NAMES.get(task_type, task_type)} +{reward}")
        elif status_text == kd.STATUS_ALREADY or (
                status_text == kd.STATUS_SUCCESS and reward <= 0):
            already_tasks.append(kd.TASK_NAMES.get(task_type, task_type))

    if errors and not fresh_tasks and not already_tasks:
        return "", 0
    parts = []
    if fresh_tasks:
        parts.append("领到 " + "、".join(fresh_tasks))
    elif already_tasks:
        parts.append("今日已领取过（" + "、".join(already_tasks) + "）")
    if errors:
        parts.append("；".join(errors[:1]) + "（不重试，明日自动再试）")
    report = ("；".join(parts) if parts else "")
    return report, claimed_total


def _status() -> dict:
    """只读查询今日签到状态，不领取。

    主口径是连签活动日历（homepage 今天格子）；连签配置拿不到时退回
    freepoint 任务面板。signed_today 只在「明确已签」时置 true。
    """
    creds = kd.resolve_credentials()
    if creds is None:
        _client_creds, client_reason = kd.read_cookies_from_client()
        if client_reason:
            return _out("NO_AUTH", f"库库登录态不可用：{client_reason}")
        where = "、".join(label for label, _path in kd.credential_sources())
        return _out("NO_AUTH", f"未找到库库 Cookie（{where}、库库客户端、环境变量均无）")

    state = _load_state()
    streak = _streak_days(state)

    trio, blocked = _session(creds)
    if trio is None:
        blocked["streak_days"] = streak
        return blocked

    # 连签活动（主口径）
    js_token, _st, _err = kd.fetch_js_token(creds)
    if js_token:
        conf, _st, _err = kd.fetch_sign_conf(creds, trio, js_token)
        tid = str((conf or {}).get("activityTid") or "").strip()
        if tid:
            homepage = kd.sign_homepage(creds, trio, js_token, tid)
            done = kd.sign_today_status(homepage)
            if done is True:
                streak = _confirm_day(state)
                return _out("ALREADY", "库库连签：今日已签到", streak=streak,
                            signed_today=True)
            if done is False:
                return _out("ALREADY", "库库连签：今日未签到，点签到即可领取",
                            streak=streak, signed_today=False)

    # freepoint 面板（兜底口径）
    panel = kd.point_panel(creds, trio)
    done = _login_done(panel)
    if done is True:
        streak = _confirm_day(state)
        return _out("ALREADY", "库库：今日每日登录已领取", streak=streak,
                    signed_today=True)
    if done is False:
        return _out("ALREADY", "库库：今日每日登录待领取，点签到即可领取",
                    streak=streak, signed_today=False)
    return _out("ALREADY", "库库：当前没有可查询的签到活动", streak=streak)


def _write_cookie(argv: list[str]) -> int:
    """--cookie "BDUSS=...; STOKEN=..." 写入凭据。"""
    text = ""
    index = 0
    while index < len(argv):
        if argv[index] == "--cookie" and index + 1 < len(argv):
            text = argv[index + 1].strip()
            index += 2
            continue
        index += 1
    if not text:
        print(json.dumps(_out("ERROR", "--cookie 缺少 Cookie 串"), ensure_ascii=False))
        return 1
    creds = kd.cookie_from_string(text)
    if creds is None:
        print(json.dumps(_out("ERROR",
                              "Cookie 串里没有找到 BDUSS（签到必须）。"
                              "请在已登录 kuku.baidu.com 的浏览器里复制完整 Cookie"),
                         ensure_ascii=False))
        return 1
    if not kd.save_credentials(creds):
        print(json.dumps(_out("ERROR", "凭据写入 auths/ 失败，请检查目录权限"),
                         ensure_ascii=False))
        return 1
    print(json.dumps(_out("ALREADY",
                          "库库 Cookie 已写入 auths/kuku.json（bdstoken 三件套会在"
                          "每次签到时自动换新，无需再管 token）"),
                     ensure_ascii=False))
    return 0


_LOGIN_GUIDE = """
库库AI（kuku.baidu.com）凭据导出引导
=====================================

库库网页版用百度通行证 Cookie 登录，脚本需要拿到你浏览器的登录 Cookie
（核心是 BDUSS，长期有效；bdstoken 三件套脚本会自动续期，不用管）。

两种方式：

方式一（推荐）：从浏览器 DevTools 复制
  1. 用浏览器打开 https://kuku.baidu.com 并登录
  2. 按 F12 打开开发者工具 → Network（网络）标签
  3. 刷新页面，随便点一个 kuku.baidu.com 的请求
  4. 在 Request Headers（请求标头）里找到 Cookie: 那一行，整串复制
     （至少要包含 BDUSS=...；有 STOKEN 更稳）
  5. 运行：
       python scripts/kuku.py --cookie "把刚才整串粘到这里"

方式二：只要 BDUSS
  DevTools → Application（应用）→ Cookies → https://kuku.baidu.com
  → 找到名为 BDUSS 的行，复制它的 Value，然后：
       python scripts/kuku.py --cookie "BDUSS=粘贴的值"

隐私说明：Cookie 只写入本项目 auths/kuku.json（含同步盘漫游镜像），
不会出现在日志或输出里；Cookie 等同账号登录态，请勿共享给他人。
"""


def main() -> int:
    argv = sys.argv[1:]
    if "--help" in argv or "-h" in argv:
        print(__doc__)
        return 0
    if "--login" in argv or "--guide" in argv:
        print(_LOGIN_GUIDE)
        return 0
    if "--cookie" in argv:
        return _write_cookie(argv)

    out = _status() if "--status" in argv else _run()
    print(json.dumps(out, ensure_ascii=False))
    if out["result"] in ("CLAIMED", "ALREADY"):
        return 0
    return 2 if out["result"] == "NO_AUTH" else 1


if __name__ == "__main__":
    sys.exit(main())
