@echo off
cd /d "%~dp0"
title HR Chatbot Backend - port 8000
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found in this folder. See RUN_LOCAL.md.
  pause
  exit /b 1
)
echo Starting backend on http://localhost:8000  ^(Ctrl+C to stop^)
".venv\Scripts\python.exe" -X utf8 -m uvicorn app.main:app --port 8000
echo.
echo Backend stopped.
pause
