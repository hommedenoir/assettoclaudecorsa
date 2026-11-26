# Help test the T248 screen bridge (about 2 minutes)

This free tool puts Assetto Corsa telemetry (gear, revs, speed, lap times, position) on the
screen of a Thrustmaster wheel. It works on a T248. We are looking for people with:

- a **Ferrari SF1000 rim** (on any base: T300, TX, TS-XW, TS-PC, T-GT, T598, T818)
- a **T248R**
- another **T248** (to confirm it works on other PCs)

You do **not** need Assetto Corsa for the test.

## What you need
- Windows 10 or 11
- The normal Thrustmaster driver for your wheel (from thrustmaster.com)
- Python 3.8 or newer, **64-bit** (python.org, tick "Add python.exe to PATH" when installing)

## The test
1. Download `t248_screen_bridge.py` and `run_report.bat` into one folder.
2. Close Assetto Corsa, other racing games and the Thrustmaster Control Panel.
3. Double-click `run_report.bat`.
4. It asks which wheel and base you have, then runs three short tests (about 25 seconds in total).
   Watch the wheel's screen and LEDs during each test and answer the question afterwards:
   - **A:** gear 3 and 90 km/h should appear.
   - **B:** the revs sweep up three times: do the rev lights / LEDs follow?
   - **C:** gears, speed, lap times and position change.
5. It saves `t248_bridge_report.txt` in the same folder.

## Send back
`t248_bridge_report.txt` and `t248_bridge.log` (same folder), as a GitHub issue or however
you were asked. Photos or a short video of the screen during the test help a lot.

## Is it safe?
The tool only talks to your wheel through Thrustmaster's own driver (the same interface games
use). It does not change firmware or settings, and nothing is sent over the internet.
