# assettoclaudecorsa

Assetto Corsa tools.

## t248-screen-bridge
Shows Assetto Corsa telemetry (gear, revs, speed, lap times, race info) on the built-in screen of a
Thrustmaster T248, through Thrustmaster's own SDK. Can also serve a live web dashboard and send UDP.

    python t248_screen_bridge.py --selftest      # logic tests, no hardware
    python t248_screen_bridge.py --probe         # one test frame on the wheel
    python t248_screen_bridge.py                 # normal use: follow Assetto Corsa

Needs Windows, 64-bit Python 3.8+, the Thrustmaster driver package, and the original Assetto Corsa
(AC EVO uses a different shared-memory layout). Run the script with --help for all options.
Its log is written to t248_bridge.log next to the script.

**Testers wanted** (Ferrari SF1000 rim, T248R, other T248s): see [t248-screen-bridge/TESTING.md](t248-screen-bridge/TESTING.md).
The test is `run_report.bat` (or `python t248_screen_bridge.py --report`).

## Licence
Copyright (c) 2026 hommedenoir. Free for personal, non-commercial use under the
[PolyForm Noncommercial License 1.0.0](LICENSE.md). Commercial use needs written permission
from the author. Provided as is, without warranty.

## Disclaimer
This is an independent project. It is not affiliated with, endorsed by or supported by
Thrustmaster / Guillemot Corporation or Kunos Simulazioni. Thrustmaster, T248, T248R and SF1000 are
trademarks of their respective owners and are used here only to describe compatibility.
No Thrustmaster software is included: the tool uses the driver already installed on your PC.
