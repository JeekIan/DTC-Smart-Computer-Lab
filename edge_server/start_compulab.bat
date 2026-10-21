@echo off
REM CompuLab lab server (edge) launcher.
REM Put this file in the SAME folder as manage.py. Double-click it, or run it at startup (Task Scheduler).
cd /d "%~dp0"

REM If you use a virtual environment, uncomment and fix the next line:
REM call venv\Scripts\activate

:loop
echo [%date% %time%] Starting CompuLab on 0.0.0.0:8000 ...
REM --noreload = one stable process (the sync worker needs this); --insecure serves CSS/JS when DEBUG=False
python manage.py runserver 0.0.0.0:8000 --noreload --insecure
echo [%date% %time%] Server stopped. Restarting in 5 seconds (close this window to stop) ...
timeout /t 5 /nobreak >nul
goto loop
