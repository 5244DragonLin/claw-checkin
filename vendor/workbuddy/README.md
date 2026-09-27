# vendor/workbuddy —— 内置的 WorkBuddy 签到内核

本目录是上游仓库 [88lin/workbuddy-auto-signin](https://github.com/88lin/workbuddy-auto-signin)
中 `signin.py` 的**原样副本**，目的是让本项目克隆后无需再手动 clone 上游仓库即可使用 WorkBuddy 签到。

- 来源提交：`e4c176c`（2026-09-25）
- 许可证：MIT（见同目录 `LICENSE`，版权所有 88lin）
- 副本不做任何修改；运行日志由 `scripts/workbuddy.py` 通过 `WORKBUDDY_SIGNIN_LOG` 重定向到项目 `data/` 目录，保持本目录干净

## 更新副本

WorkBuddy 客户端升级常伴随凭据解密协议变化，上游修复后按以下任一方式同步：

```bash
# 方式一：直接覆盖内置副本
curl -L https://raw.githubusercontent.com/88lin/workbuddy-auto-signin/main/signin.py -o vendor/workbuddy/signin.py

# 方式二：clone 到 _ref_workbuddy/（查找优先级高于本目录，脚本自动采用）
git clone https://github.com/88lin/workbuddy-auto-signin.git _ref_workbuddy
```

查找优先级详见 `scripts/workbuddy.py` 的 `SIGNIN_CANDIDATES`：
环境变量 `WORKBUDDY_SIGNIN_DIR` > `_ref_workbuddy/` > 本目录 > `scripts/signin.py`。
