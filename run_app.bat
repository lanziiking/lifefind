@echo off
rem Double-click this file to start the Lifefind app from source.
rem It uses the project's own .venv, so no Python install is needed.
cd /d "%~dp0"
echo Starting Lifefind ...
".venv\Scripts\python.exe" "app\main.py"
if errorlevel 1 (
  echo.
  echo Program exited with an error. See the messages above.
  pause
)
