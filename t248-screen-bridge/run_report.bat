@echo off
rem T248 screen bridge - tester report. Needs 64-bit Python 3.8+.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 t248_screen_bridge.py --report
) else (
  python t248_screen_bridge.py --report
)
echo.
pause
