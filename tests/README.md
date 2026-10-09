# tests —— 开发期验证脚本

这些脚本**不属于产品代码**，是每次改动后用来验证「没把别的弄坏」的回归网。
不随安装包分发，也不被主代码 import。

## 约定

- 一律从**项目根目录**运行，不要 `cd tests` 再跑（脚本内部用 `__file__` 反推项目根，在哪跑都对，但统一在根目录跑日志更好读）
- 文件名前缀 `_verify_` 表示它是验证脚本，不是被测对象
- 端口默认 `8317`；`_verify_calendar.mjs` 的端口是命令行参数，必须显式传

## 怎么跑

```bash
# Python 验证（需要 fastapi + playwright；testclient 需要 httpx2）
python tests/_verify_today_live.py        # 当日积分与平台实时口径一致
python tests/_verify_void30.py            # 过期快照历史保留 + 过去 30 天作废
python tests/_verify_today_replace.py     # 今天行同源替换 + 小浣熊来源标签
python tests/_verify_refresh_credits.py   # 积分刷新防抖与串行化
python tests/_verify_workbuddy_trial.py   # 累计积分含体验版周期额度
python tests/_verify_ledger_sync.py       # JSON 账本与旧 SQLite 语义一致
python tests/_verify_clear_logs.py# 清空运行日志不影响过期日历
python tests/_verify_page_refresh.py      # 刷新页面不再误显示「刷新中…」、不二次重画
python tests/_verify_manual_refresh_btn.py# 手动点按钮仍要有刷新中反馈
python tests/_verify_scrollbar_shift.py   # 切页时内容不左右抖动（需有头 Chrome）
python tests/_verify_scrollbar_sidefx.py  # 滚动条占位的副作用（窄屏 / 暗色）
python tests/_verify_theme.py             # 明暗主题切换与对比度

# 前端配色验证（Node，端口必须传）
node tests/_verify_calendar.mjs 8317
```

## 环境注意

- **托管 venv 缺 `httpx2`**，`fastapi.testclient` 不可用 → 后端脚本请用项目自己的 `D:\Python312\python.exe`
- **`_verify_scrollbar_*.py` 必须有头浏览器**（`headless=False` 驱动系统 Chrome）：
  无头Chrome 用的是悬浮滚动条，不占布局宽度，量出来的「一致」是假的
- `_verify_calendar.mjs` 需设 `NODE_PATH` 指向装了 playwright 的 node_modules

## 已知非失败

`_verify_theme.py` 会报「暗色引入 2 项对比度问题」，那是暗色模式引入时就有
的**亮色主题原有**问题（`.pc-meta` 3.25:1、`.stat-gray` 3.71:1），非回归，
需用户决定是否加深 `--muted` 后才会消失。
