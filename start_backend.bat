@echo off
echo ==========================================
echo  PaarakhMetric - Starting Backend Server
echo ==========================================
cd /d "%~dp0backend"
if not exist ".venv" (
    echo [ERROR] Virtual environment not found!
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat
echo Starting FastAPI backend on http://localhost:8000
echo API docs: http://localhost:8000/docs
python run.py
pause
