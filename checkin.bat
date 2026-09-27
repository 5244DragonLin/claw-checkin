@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
rem ClawCheckin - check-in runner
rem Double-click: show results live, wait for a key before closing
rem Task Scheduler: checkin.bat /task  (silent, output appended to data\checkin.log)
rem schtasks /Create /TN "ClawCheckin" /SC DAILY /ST 00:05 /TR "<full path>\checkin.bat /task"
cd /d %~dp0
where python >nul 2>nul
if errorlevel 1 (
  echo [ClawCheckin] Python not found.
  pause
  exit /b 1
)
if not exist data mkdir data

if /i "%~1"=="/task" goto task

rem ---- 手动模式：实时显示签到结果，按任意键关闭 ----
python main.py --run-once
set RC=%errorlevel%
echo.
echo [ClawCheckin] 结果已写入账本 data/ledger.db，运行 start.bat 可在面板查看
echo [ClawCheckin] 按任意键关闭窗口...
pause >nul
exit /b %RC%

:task
rem ---- 任务计划模式：静默执行，输出追加进日志 ----
python main.py --run-once >> data\checkin.log 2>&1
for %%F in ("data\checkin.log") do if %%~zF GTR 5242880 move /y "%%~F" "data\checkin.log.old" >nul
exit /b 0
