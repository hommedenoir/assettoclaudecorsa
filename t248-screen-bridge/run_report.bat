@echo off
rem T248 screen bridge - tester report. Needs 64-bit Python 3.8+.
cd /d "%~dp0"
py -3 t248_screen_bridge.py --report 2>nul || python t248_screen_bridge.py --report
echo.
pause
