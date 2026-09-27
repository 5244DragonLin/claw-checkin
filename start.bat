@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
rem ClawCheckin - startup script
cd /d %~dp0
where python >nul 2>nul
if errorlevel 1 (
  echo [ClawCheckin] Python not found. Please install Python 3.10+ and add to PATH.
  pause
  exit /b 1
)
python -c "import fastapi, uvicorn" >nul 2>nul
if errorlevel 1 (
  echo [ClawCheckin] First run - installing dependencies...
  python -m pip install -i https://pypi.org/simple/ --trusted-host pypi.org -r requirements.txt
)
python main.py
pause