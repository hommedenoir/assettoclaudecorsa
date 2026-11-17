#!/usr/bin/env python3
"""
T248 screen bridge for Assetto Corsa
====================================

Reads live telemetry from Assetto Corsa's shared memory and shows it on the
Thrustmaster T248's built-in screen (gear, RPM / rev bar, speed, lap times)
by calling Thrustmaster's own SDK (the tm_api_* functions).

Requirements
  * Windows, 64-bit Python 3.8 or newer (the SDK DLL is 64-bit).
  * The Thrustmaster driver package installed (it provides the SDK files).
  * Assetto Corsa (the original game). Assetto Corsa EVO uses a different
    shared-memory layout and is NOT read by this version.

Usage
  python t248_screen_bridge.py --selftest      # checks the logic, no hardware
  python t248_screen_bridge.py --list-devices  # which Thrustmaster devices the SDK sees and what each can do
  python t248_screen_bridge.py --probe         # init the SDK, open the wheel, send one frame
  python t248_screen_bridge.py --simulate      # sweep fake values on the wheel screen
  python t248_screen_bridge.py                 # normal use: follow Assetto Corsa
  python t248_screen_bridge.py --dry-run       # read AC, print what would be sent, touch no hardware

Sending telemetry elsewhere (any combination; the wheel keeps working alongside)
  python t248_screen_bridge.py --list-fields                      # every selectable value
  python t248_screen_bridge.py --serve 8080 --fields core,tyres   # live web dashboard on this PC
  python t248_screen_bridge.py --serve 0.0.0.0:8080 --fields all  # same, for a tablet or second PC on your network
  python t248_screen_bridge.py --udp 192.168.1.20:20777           # JSON datagrams for other software
  python t248_screen_bridge.py --no-wheel --serve 8080            # outputs only, no wheel or SDK needed
  python t248_screen_bridge.py --simulate --no-wheel --serve 8080 # try the dashboard without the game

Everything it learns is written to t248_bridge.log next to this script.

STATUS: written from reverse engineering of the SDK and of USB captures. The
record layout, parameter IDs and call order come from Thrustmaster's own test
app, but this program has not been run against a real wheel by its author.
Run --probe first and send back t248_bridge.log if anything fails.
"""

import argparse
import ctypes
import glob
import json
import logging
import os
import math
import re
import signal
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler

# --------------------------------------------------------------------------
# SDK constants (taken from Thrustmaster's SDK test app)
# --------------------------------------------------------------------------
CATEGORY_GAME_DATA = 0x2001000      # second argument of tm_api_set_properties
RECORD_SIZE = 56                    # the app writes 0x38 into the first field
TYPE_INT = 2                        # 32-bit integer value
TYPE_DOUBLE = 4                     # 64-bit floating point value

# Parameter IDs. Same numbering as the records seen on the USB wire.
ID_GEAR = 0x06          # ASCII character: '1'..'9', 'N', 'R'   (confirmed)
ID_RPM = 0x07           # engine RPM, integer                    (confirmed)
ID_RPM_PERCENT = 0x08   # 0..100, double; drives the rev bar     (confirmed)
ID_SPEED = 0x09         # km/h, double                           (probable units)
ID_BEST_LAP = 0x0A      # milliseconds                           (confirmed)
ID_CURRENT_LAP = 0x0B   # milliseconds                           (confirmed)
ID_LAST_LAP = 0x36      # milliseconds                           (confirmed)
ID_POSITION = 0x31      # race position                          (probable)
ID_CARS = 0x32          # number of drivers                      (confirmed)
ID_LAP_CURRENT = 0x33   # current lap number                     (probable)
ID_LAP_COUNT = 0x34     # total laps                             (confirmed)
ID_POWER = 0x49         # screen power state, 0..2: the test app steps 0,1,2,1,0 (meaning inferred, see --power-test)
POWER_STANDBY, POWER_WAKING, POWER_AWAKE = 0, 1, 2

log = logging.getLogger("t248")


# --------------------------------------------------------------------------
# Telemetry model and Assetto Corsa conversions
# --------------------------------------------------------------------------
@dataclass
class Telemetry:
    live: bool = False
    gear: str = "N"
    rpm: int = 0
    rpm_percent: float = 0.0
    speed_kmh: float = 0.0
    current_ms: int = 0
    last_ms: int = 0
    best_ms: int = 0
    position: int = 0
    cars: int = 0
    lap_current: int = 0
    lap_count: int = 0


def ac_gear_to_char(ac_gear):
    """AC numbers gears 0 = reverse, 1 = neutral, 2 = 1st, 3 = 2nd ..."""
    if ac_gear == 0:
        return "R"
    if ac_gear == 1:
        return "N"
    return str(max(1, min(9, ac_gear - 1)))


def rpm_to_percent(rpm, max_rpm):
    if max_rpm <= 0:
        return 0.0
    return max(0.0, min(100.0, rpm * 100.0 / max_rpm))


# Offsets in AC's shared memory pages (SPageFilePhysics / Graphic / Static).
PHYSICS_SIZE, GRAPHICS_SIZE, STATIC_SIZE = 800, 1600, 800


def parse_ac_pages(physics, graphics, static, max_rpm_cache):
    """Build a Telemetry from raw page bytes. Returns (Telemetry, max_rpm)."""
    t = Telemetry()
    status, = struct.unpack_from("<i", graphics, 4)       # 0 off, 1 replay, 2 live, 3 pause
    t.live = status in (2, 3)
    if status == 1:                                       # replay: do not follow it
        t.live = False
    _pid, _gas, _brake, _fuel, gear, rpms, _steer, speed = struct.unpack_from("<ifffiiff", physics, 0)
    completed, position, cur, last, best = struct.unpack_from("<iiiii", graphics, 132)
    laps, = struct.unpack_from("<i", graphics, 172)
    cars, = struct.unpack_from("<i", static, 64)
    max_rpm, = struct.unpack_from("<i", static, 412)
    if not (1000 <= max_rpm <= 30000):
        max_rpm = max_rpm_cache                            # keep the last good value
    if not (1000 <= max_rpm <= 30000):
        max_rpm = max(max_rpm_cache, rpms, 0)              # last resort: peak seen so far
    t.gear = ac_gear_to_char(gear)
    t.rpm = max(0, min(30000, rpms))
    t.rpm_percent = rpm_to_percent(t.rpm, max_rpm)
    t.speed_kmh = max(0.0, min(999.0, float(speed)))
    t.current_ms = max(0, cur)
    t.last_ms = max(0, last)
    t.best_ms = max(0, best)
    t.position = max(0, position)
    t.cars = max(0, cars)
    t.lap_current = max(0, completed + 1)
    t.lap_count = max(0, laps)
    return t, max_rpm


# --------------------------------------------------------------------------
# Shared memory reader (Windows). Opens the EXISTING mapping read-only, so it
# can never create a mapping that would interfere with the game.
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Full telemetry: a declarative table of everything Assetto Corsa publishes
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Field:
    name: str
    page: str          # 'physics', 'graphics' or 'static'
    offset: int
    fmt: str           # 'i' int32, 'f' float32, '4f' float array, 'w33' UTF-16 string of 33 characters
    group: str
    desc: str
    lo: float = None   # plausible range, used only to warn when the layout looks wrong
    hi: float = None

    @property
    def size(self):
        if self.fmt.startswith("w"):
            return int(self.fmt[1:]) * 2
        return 4 * (int(self.fmt[:-1]) if len(self.fmt) > 1 else 1)

    @property
    def count(self):
        return 1 if self.fmt in ("i", "f") or self.fmt.startswith("w") else int(self.fmt[:-1])


def _f(*a, **k):
    return Field(*a, **k)


# Layout follows Assetto Corsa's published shared-memory structures. Arrays are ordered FL, FR, RL, RR.
FIELDS = [
    # physics page
    _f("packet_id", "physics", 0, "i", "meta", "physics packet counter (changes while the game runs)"),
    _f("gas", "physics", 4, "f", "driver", "throttle pedal, 0 to 1", 0, 1),
    _f("brake", "physics", 8, "f", "driver", "brake pedal, 0 to 1", 0, 1),
    _f("fuel", "physics", 12, "f", "engine", "fuel in tank, litres", 0, 1000),
    _f("gear_raw", "physics", 16, "i", "engine", "gear as AC numbers it: 0 reverse, 1 neutral, 2 first", 0, 12),
    _f("rpm", "physics", 20, "i", "engine", "engine revolutions per minute", 0, 30000),
    _f("steer_angle", "physics", 24, "f", "driver", "steering angle, game units"),
    _f("speed_kmh", "physics", 28, "f", "dynamics", "speed, km/h", 0, 700),
    _f("velocity", "physics", 32, "3f", "dynamics", "velocity vector x, y, z, m/s"),
    _f("acc_g", "physics", 44, "3f", "dynamics", "acceleration x, y, z, in g", -15, 15),
    _f("wheel_slip", "physics", 56, "4f", "tyres", "wheel slip per tyre"),
    _f("wheel_load", "physics", 72, "4f", "tyres", "load per wheel, newtons", -1000, 40000),
    _f("tyre_pressure", "physics", 88, "4f", "tyres", "tyre pressure, psi", 0, 80),
    _f("wheel_angular_speed", "physics", 104, "4f", "tyres", "wheel angular speed, rad/s"),
    _f("tyre_wear", "physics", 120, "4f", "tyres", "tyre wear per tyre"),
    _f("tyre_dirt", "physics", 136, "4f", "tyres", "tyre dirt level per tyre"),
    _f("tyre_core_temp", "physics", 152, "4f", "tyres", "tyre core temperature, degC", -60, 400),
    _f("camber_rad", "physics", 168, "4f", "suspension", "camber per wheel, radians"),
    _f("suspension_travel", "physics", 184, "4f", "suspension", "suspension travel per wheel, metres"),
    _f("drs", "physics", 200, "f", "electronics", "DRS state, 0 to 1", 0, 1),
    _f("tc", "physics", 204, "f", "electronics", "traction control activity, 0 to 1", 0, 1),
    _f("heading", "physics", 208, "f", "dynamics", "heading, radians"),
    _f("pitch", "physics", 212, "f", "dynamics", "pitch, radians"),
    _f("roll", "physics", 216, "f", "dynamics", "roll, radians"),
    _f("cg_height", "physics", 220, "f", "car", "centre of gravity height, metres"),
    _f("car_damage", "physics", 224, "5f", "car", "damage: front, rear, left, right, centre", 0, 5000),
    _f("tyres_out", "physics", 244, "i", "car", "number of tyres off the track", 0, 4),
    _f("pit_limiter", "physics", 248, "i", "electronics", "pit limiter on, 0 or 1", 0, 1),
    _f("abs", "physics", 252, "f", "electronics", "ABS activity, 0 to 1", 0, 1),
    _f("kers_charge", "physics", 256, "f", "electronics", "KERS charge, 0 to 1"),
    _f("kers_input", "physics", 260, "f", "electronics", "KERS input"),
    _f("auto_shifter", "physics", 264, "i", "electronics", "automatic gearbox on, 0 or 1", 0, 1),
    _f("ride_height", "physics", 268, "2f", "suspension", "ride height front, rear"),
    _f("turbo_boost", "physics", 276, "f", "engine", "turbo boost", 0, 20),
    _f("ballast", "physics", 280, "f", "car", "ballast, kg"),
    _f("air_density", "physics", 284, "f", "environment", "air density", 0, 3),
    _f("air_temp", "physics", 288, "f", "environment", "air temperature, degC", -60, 80),
    _f("road_temp", "physics", 292, "f", "environment", "road temperature, degC", -60, 120),
    _f("local_angular_vel", "physics", 296, "3f", "dynamics", "angular velocity x, y, z"),
    _f("final_ff", "physics", 308, "f", "driver", "final force feedback output"),
    _f("performance_meter", "physics", 312, "f", "lap", "performance meter: time delta to the best lap"),
    _f("engine_brake", "physics", 316, "i", "electronics", "engine brake setting"),
    _f("ers_recovery_level", "physics", 320, "i", "electronics", "ERS recovery level"),
    _f("ers_power_level", "physics", 324, "i", "electronics", "ERS power level"),
    _f("ers_heat_charging", "physics", 328, "i", "electronics", "ERS heat charging"),
    _f("ers_is_charging", "physics", 332, "i", "electronics", "ERS charging, 0 or 1", 0, 1),
    _f("kers_current_kj", "physics", 336, "f", "electronics", "KERS energy used this lap, kJ"),
    _f("drs_available", "physics", 340, "i", "electronics", "DRS available, 0 or 1", 0, 1),
    _f("drs_enabled", "physics", 344, "i", "electronics", "DRS enabled, 0 or 1", 0, 1),
    _f("brake_temp", "physics", 348, "4f", "brakes", "brake disc temperature, degC", -60, 2500),
    _f("clutch", "physics", 364, "f", "driver", "clutch pedal, 0 to 1 (1 = released)", 0, 1),
    _f("tyre_temp_inner", "physics", 368, "4f", "tyres", "tyre inner temperature, degC", -60, 400),
    _f("tyre_temp_middle", "physics", 384, "4f", "tyres", "tyre middle temperature, degC", -60, 400),
    _f("tyre_temp_outer", "physics", 400, "4f", "tyres", "tyre outer temperature, degC", -60, 400),
    # graphics page
    _f("status", "graphics", 4, "i", "session", "0 off, 1 replay, 2 live, 3 pause", 0, 3),
    _f("session_type", "graphics", 8, "i", "session", "session type: 0 unknown, 1 practice, 2 qualify, 3 race ...", -1, 12),
    _f("completed_laps", "graphics", 132, "i", "lap", "laps completed", 0, 100000),
    _f("position", "graphics", 136, "i", "lap", "race position", 0, 200),
    _f("current_time_ms", "graphics", 140, "i", "lap", "current lap time, ms", 0, 36000000),
    _f("last_time_ms", "graphics", 144, "i", "lap", "last lap time, ms", 0, 36000000),
    _f("best_time_ms", "graphics", 148, "i", "lap", "best lap time reported by the game, ms", 0, 36000000),
    _f("session_time_left", "graphics", 152, "f", "session", "session time left, ms"),
    _f("distance_traveled", "graphics", 156, "f", "session", "distance driven, metres"),
    _f("is_in_pit", "graphics", 160, "i", "session", "in the pit, 0 or 1", 0, 1),
    _f("current_sector", "graphics", 164, "i", "lap", "current sector index", 0, 20),
    _f("last_sector_time_ms", "graphics", 168, "i", "lap", "last sector time, ms", 0, 36000000),
    _f("number_of_laps", "graphics", 172, "i", "lap", "laps in the session", 0, 100000),
    _f("tyre_compound", "graphics", 176, "w33", "car", "tyre compound name"),
    _f("replay_time_multiplier", "graphics", 244, "f", "session", "replay speed multiplier"),
    _f("normalized_car_position", "graphics", 248, "f", "lap", "position around the lap, 0 to 1", -0.01, 1.01),
    _f("car_coordinates", "graphics", 252, "3f", "dynamics", "world position x, y, z, metres"),
    _f("penalty_time", "graphics", 264, "f", "session", "penalty time, seconds", 0, 100000),
    _f("flag", "graphics", 268, "i", "session", "flag shown: 0 none, 1 blue, 2 yellow, 3 black, 4 white, 5 checkered", 0, 12),
    _f("ideal_line_on", "graphics", 272, "i", "session", "ideal line shown, 0 or 1", 0, 1),
    _f("is_in_pit_lane", "graphics", 276, "i", "session", "in the pit lane, 0 or 1", 0, 1),
    _f("surface_grip", "graphics", 280, "f", "environment", "track surface grip", 0, 2),
    _f("mandatory_pit_done", "graphics", 284, "i", "session", "mandatory pit stop done, 0 or 1", 0, 1),
    _f("wind_speed", "graphics", 288, "f", "environment", "wind speed, m/s", 0, 200),
    _f("wind_direction", "graphics", 292, "f", "environment", "wind direction, degrees"),
    # static page
    _f("number_of_sessions", "static", 60, "i", "session", "sessions in the event", 0, 50),
    _f("num_cars", "static", 64, "i", "session", "cars in the session", 0, 500),
    _f("car_model", "static", 68, "w33", "car", "car model id"),
    _f("track", "static", 134, "w33", "session", "track id"),
    _f("sector_count", "static", 400, "i", "session", "sectors on the track", 0, 20),
    _f("max_torque", "static", 404, "f", "car", "maximum torque, Nm", 0, 20000),
    _f("max_power", "static", 408, "f", "car", "maximum power, W", 0, 5000000),
    _f("max_rpm", "static", 412, "i", "car", "engine rev limit", 0, 30000),
    _f("max_fuel", "static", 416, "f", "car", "fuel tank capacity, litres", 0, 1000),
]
# Values the bridge computes itself from the fields above.
DERIVED = [
    ("gear", "core", "gear as a character: R, N, 1 to 9"),
    ("rpm_percent", "core", "RPM as a percentage of the rev limit, 0 to 100"),
]
GROUP_EXTRA = {   # groups made of several kinds of field
    "core": ["gear", "rpm", "rpm_percent", "speed_kmh", "gas", "brake", "clutch", "fuel"],
}
FIELD_BY_NAME = {f.name: f for f in FIELDS}
ALL_NAMES = [f.name for f in FIELDS] + [n for n, _g, _d in DERIVED]


def decode_fields(pages):
    """Read every field from raw page bytes. Fields beyond the end of a page come back as None."""
    out = {}
    for f in FIELDS:
        buf = pages.get(f.page)
        if buf is None or f.offset + f.size > len(buf):
            out[f.name] = None
            continue
        if f.fmt.startswith("w"):
            raw = bytes(buf[f.offset:f.offset + f.size])
            out[f.name] = raw.decode("utf-16-le", errors="ignore").split("\x00", 1)[0].strip()
        elif f.count == 1:
            out[f.name], = struct.unpack_from("<" + f.fmt, buf, f.offset)
        else:
            out[f.name] = list(struct.unpack_from("<%d%s" % (f.count, f.fmt[-1]), buf, f.offset))
    return out


def add_derived(values, max_rpm_cache=0):
    """Add gear and rpm_percent. Returns the max_rpm used."""
    g = values.get("gear_raw")
    values["gear"] = ac_gear_to_char(g) if isinstance(g, int) else None
    max_rpm = values.get("max_rpm")
    if not (isinstance(max_rpm, int) and 1000 <= max_rpm <= 30000):
        max_rpm = max_rpm_cache
    rpm = values.get("rpm")
    values["rpm_percent"] = round(rpm_to_percent(rpm, max_rpm), 1) if isinstance(rpm, int) and max_rpm else None
    return max_rpm


def select_fields(spec, available=None):
    """Turn 'core,tyres,-tyre_wear' into an ordered list of field names.
    Tokens may be field names, group names, 'all', or a name/group with a leading '-' to remove it."""
    available = list(available or ALL_NAMES)
    groups = {}
    for f in FIELDS:
        groups.setdefault(f.group, []).append(f.name)
    for n, g, _d in DERIVED:
        groups.setdefault(g, []).append(n)
    for g, names in GROUP_EXTRA.items():
        groups[g] = [n for n in names if n in available]
    chosen, errors = [], []
    for tok in [t.strip() for t in str(spec).split(",") if t.strip()]:
        remove = tok.startswith("-")
        tok = tok.lstrip("-")
        names = available if tok == "all" else groups.get(tok) or ([tok] if tok in available else None)
        if names is None:
            errors.append(tok)
            continue
        for n in names:
            if remove:
                chosen = [c for c in chosen if c != n]
            elif n not in chosen:
                chosen.append(n)
    if errors:
        raise ValueError("unknown field or group: %s" % ", ".join(errors))
    return chosen


def field_catalog():
    """Metadata for every selectable value: used by --list-fields and the dashboard."""
    cat = [{"name": f.name, "group": f.group, "desc": f.desc, "count": f.count} for f in FIELDS]
    cat += [{"name": n, "group": g, "desc": d, "count": 1} for n, g, d in DERIVED]
    return cat


def clean_json(value):
    """Make a value safe for strict JSON: NaN and infinity become null, floats are rounded."""
    if isinstance(value, float):
        return round(value, 4) if math.isfinite(value) else None
    if isinstance(value, list):
        return [clean_json(v) for v in value]
    return value


class PlausibilityChecker:
    """Warns (once per field) when live values fall outside what the field should hold.
    That usually means the field sits at a different place in this game version."""

    def __init__(self, patience=30):
        self.patience, self.bad, self.warned = patience, {}, set()

    def check(self, values, live):
        if not live:
            return
        for f in FIELDS:
            if f.lo is None or f.name in self.warned:
                continue
            v = values.get(f.name)
            for x in (v if isinstance(v, list) else [v]):
                if isinstance(x, (int, float)) and (not math.isfinite(x) or x < f.lo or x > f.hi):
                    n = self.bad[f.name] = self.bad.get(f.name, 0) + 1
                    if n >= self.patience:
                        self.warned.add(f.name)
                        log.warning("field %s looks wrong (%r, expected %s to %s): its position in the game's memory may differ",
                                    f.name, v, f.lo, f.hi)
                    break
            else:
                self.bad[f.name] = 0


def core_values(t):
    """The wheel-screen values, in the same names as the full telemetry (used for --simulate)."""
    return {"gear": t.gear, "rpm": t.rpm, "rpm_percent": round(t.rpm_percent, 1), "speed_kmh": round(t.speed_kmh, 1),
            "current_time_ms": t.current_ms, "last_time_ms": t.last_ms, "best_time_ms": t.best_ms,
            "position": t.position, "num_cars": t.cars, "completed_laps": max(0, t.lap_current - 1),
            "number_of_laps": t.lap_count}


# --------------------------------------------------------------------------
# Outputs for the telemetry (external monitor, other software, console)
# --------------------------------------------------------------------------
DASH_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Telemetry</title><style>
:root{color-scheme:dark;--bg:#0f1115;--card:#1a1e26;--fg:#e8eaf0;--mut:#8b93a7;--ok:#4ade80}
body{margin:0;background:var(--bg);color:var(--fg);font:16px system-ui,sans-serif}
header{display:flex;justify-content:space-between;padding:10px 16px;background:#141821}
#status{color:var(--mut)}#status.live{color:var(--ok)}
main{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px;padding:12px}
.tile{background:var(--card);border-radius:10px;padding:10px 12px}
.name{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.val{font-size:26px;font-variant-numeric:tabular-nums;margin-top:4px;word-break:break-word}
.arr{display:grid;grid-template-columns:1fr 1fr;gap:2px 10px;font-size:18px}
</style></head><body><header><b>Telemetry</b><span id="status">connecting...</span></header><main id="m"></main>
<script>
const m=document.getElementById('m'),st=document.getElementById('status');const tiles={};
function fmt(v){return typeof v==='number'?(Number.isInteger(v)?v:v.toFixed(2)):(v===null?'-':String(v))}
async function tick(){try{const r=await fetch('/data',{cache:'no-store'});const d=await r.json();
st.textContent=d.live?'live':'waiting for the game';st.className=d.live?'live':'';
for(const [k,v] of Object.entries(d.values)){let t=tiles[k];if(!t){t=tiles[k]=document.createElement('div');t.className='tile';
t.innerHTML='<div class="name"></div><div class="val"></div>';t.firstChild.textContent=k.replace(/_/g,' ');m.appendChild(t)}
const el=t.lastChild;if(Array.isArray(v)){el.className='val arr';el.innerHTML=v.map(x=>'<span>'+fmt(x)+'</span>').join('')}
else{el.className='val';el.textContent=fmt(v)}}}catch(e){st.textContent='no connection';st.className=''}setTimeout(tick,100)}
tick();
</script></body></html>"""


class UdpSink:
    """Sends each update as one JSON datagram: {"t":..., "live":..., "values":{...}}."""

    def __init__(self, host, port):
        self.addr = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def update(self, values, live):
        data = json.dumps({"t": round(time.time(), 3), "live": bool(live), "values": values},
                          allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(data) <= 65000:
            self.sock.sendto(data, self.addr)
        else:
            log.warning("UDP update too large (%d bytes); select fewer fields", len(data))

    def close(self):
        self.sock.close()


class Dashboard:
    """A small web page served on this PC: open its address in a browser on any other screen."""

    def __init__(self, host, port, names):
        self.lock, self.latest = threading.Lock(), {"t": 0, "live": False, "values": {}}
        catalog = [c for c in field_catalog() if c["name"] in set(names)]
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                try:
                    path = self.path.split("?", 1)[0]
                    if path == "/":
                        self._send(200, DASH_HTML.encode("utf-8"), "text/html; charset=utf-8")
                    elif path == "/data":
                        with owner.lock:
                            body = json.dumps(owner.latest, allow_nan=False).encode("utf-8")
                        self._send(200, body, "application/json")
                    elif path == "/fields":
                        self._send(200, json.dumps(catalog).encode("utf-8"), "application/json")
                    else:
                        self._send(404, b"not found", "text/plain")
                except (BrokenPipeError, ConnectionError):
                    pass

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def update(self, values, live):
        with self.lock:
            self.latest = {"t": round(time.time(), 3), "live": bool(live), "values": values}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ConsoleSink:
    """Prints the selected values about once a second."""

    def __init__(self, interval=1.0):
        self.interval, self.next = interval, 0.0

    def update(self, values, live):
        now = time.monotonic()
        if now >= self.next:
            self.next = now + self.interval
            print(("LIVE " if live else "idle ") + "  ".join("%s=%s" % (k, v) for k, v in values.items()))

    def close(self):
        pass


class Publisher:
    """Filters the full telemetry down to the selected fields and hands it to every output."""

    def __init__(self, names, sinks):
        self.names, self.sinks, self.failed = list(names), list(sinks), set()

    def publish(self, values, live):
        picked = {n: clean_json(values[n]) for n in self.names if n in values}
        for sink in self.sinks:
            try:
                sink.update(picked, live)
            except Exception as e:
                if id(sink) not in self.failed:
                    self.failed.add(id(sink))
                    log.warning("%s output failed: %s", type(sink).__name__, e)

    def close(self):
        for sink in self.sinks:
            try:
                sink.close()
            except Exception:
                pass


def parse_host_port(text, default_host="127.0.0.1"):
    """'8080', ':8080' or 'host:8080' -> (host, port)."""
    text = str(text).strip()
    if ":" in text:
        host, port = text.rsplit(":", 1)
    else:
        host, port = default_host, text
    return (host or default_host), int(port)


class GearFilter:
    """AC passes through neutral for about 0.1 s on every shift. Keep showing the previous gear until
    neutral has lasted `delay` seconds, so the wheel screen does not flash N between gears."""

    def __init__(self, delay=0.4):
        self.delay, self.shown, self.neutral_since = delay, None, None

    def reset(self):
        self.shown, self.neutral_since = None, None

    def update(self, gear, now):
        if self.delay <= 0 or gear != "N" or self.shown in (None, "N"):
            self.shown, self.neutral_since = gear, None
            return gear
        if self.neutral_since is None:
            self.neutral_since = now
        if now - self.neutral_since >= self.delay:
            self.shown, self.neutral_since = "N", None
            return "N"
        return self.shown


def should_recycle(not_live_for, game_running, limit=10.0):
    """Drop and re-open the game connection only when the game itself is gone, not when it sits in a menu."""
    return not_live_for > limit and not game_running


class StaleGuard:
    """AC leaves its shared memory mapped after the game exits, still saying 'live'.
    While live, the physics packet counter must keep changing; if it freezes the game is gone."""

    def __init__(self, timeout=3.0):
        self.timeout, self.pid, self.changed = timeout, None, None

    def is_live(self, packet_id, status, now):
        if status == 3:                                   # paused: counter legitimately frozen
            self.pid, self.changed = packet_id, now
            return True
        if status != 2:
            self.pid, self.changed = packet_id, now
            return False
        if packet_id != self.pid:
            self.pid, self.changed = packet_id, now
            return True
        return (now - self.changed) < self.timeout


class TelemetrySource:
    """What a game reader must provide. The wheel-screen side never needs to know which game it is.
    open() -> bool: try to attach to the game; read() -> Telemetry; close()."""

    def open(self):
        raise NotImplementedError

    def read(self):
        raise NotImplementedError

    def values(self):
        """Every field of the last read() as a dict (names as in --list-fields)."""
        return {}

    def close(self):
        pass


class AcReader(TelemetrySource):
    NAMES = {"physics": PHYSICS_SIZE, "graphics": GRAPHICS_SIZE, "static": STATIC_SIZE}

    def __init__(self, neutral_delay=0.4):
        self.views = {}
        self.max_rpm = 0
        self.guard = StaleGuard()
        self.gears = GearFilter(neutral_delay)
        self.shown_gear = None
        self.pages = {}
        self._k32 = None
        if os.name == "nt":
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.OpenFileMappingW.restype = ctypes.c_void_p
            k.OpenFileMappingW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
            k.MapViewOfFile.restype = ctypes.c_void_p
            k.MapViewOfFile.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
                                        ctypes.c_uint32, ctypes.c_size_t]
            k.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
            k.CloseHandle.argtypes = [ctypes.c_void_p]
            self._k32 = k

    def open(self):
        if self._k32 is None:
            return False
        for page in self.NAMES:
            opened = None
            for prefix in ("Local\\", ""):
                h = self._k32.OpenFileMappingW(4, 0, prefix + "acpmf_" + page)   # FILE_MAP_READ
                if h:
                    p = self._k32.MapViewOfFile(h, 4, 0, 0, 0)     # 0 = map the whole section, whatever its size
                    if p:
                        size = self._region_size(p)
                        if size > 0:
                            opened = (h, p, min(size, 8192))
                            break
                        self._k32.UnmapViewOfFile(p)
                    self._k32.CloseHandle(h)
            if not opened:
                self.close()
                return False
            self.views[page] = opened
        log.info("Mapped AC pages: %s", {k: v[2] for k, v in self.views.items()})
        return True

    def _region_size(self, ptr):
        class MBI(ctypes.Structure):
            _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                        ("AllocationProtect", ctypes.c_uint32), ("PartitionId", ctypes.c_uint16),
                        ("RegionSize", ctypes.c_size_t), ("State", ctypes.c_uint32),
                        ("Protect", ctypes.c_uint32), ("Type", ctypes.c_uint32)]
        mbi = MBI()
        self._k32.VirtualQuery.argtypes = [ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
        self._k32.VirtualQuery.restype = ctypes.c_size_t
        return mbi.RegionSize if self._k32.VirtualQuery(ptr, ctypes.byref(mbi), ctypes.sizeof(mbi)) else 0

    def values(self):
        """Every field from the pages read by the last read(), plus gear and rpm_percent."""
        v = decode_fields(self.pages)
        add_derived(v, self.max_rpm)
        if self.shown_gear is not None:
            v["gear"] = self.shown_gear                  # the steadied gear; gear_raw stays untouched
        return v

    def close(self):
        for h, p, _s in self.views.values():
            try:
                self._k32.UnmapViewOfFile(p)
                self._k32.CloseHandle(h)
            except Exception:
                pass
        self.views = {}

    def read(self):
        pages = {k: ctypes.string_at(p, s) for k, (h, p, s) in self.views.items()}
        self.pages = pages
        t, self.max_rpm = parse_ac_pages(pages["physics"], pages["graphics"], pages["static"], self.max_rpm)
        status, = struct.unpack_from("<i", pages["graphics"], 4)
        packet_id, = struct.unpack_from("<i", pages["physics"], 0)
        now = time.monotonic()
        if not self.guard.is_live(packet_id, status, now):
            t.live = False
        if t.live:
            t.gear = self.gears.update(t.gear, now)
            self.shown_gear = t.gear
        else:
            self.gears.reset()
            self.shown_gear = None
        return t


# --------------------------------------------------------------------------
# Thrustmaster SDK wrapper
# --------------------------------------------------------------------------
def find_sdk_dir(explicit=None):
    """Find the folder containing tm_api_lib_x64.dll (the installer puts it in a 'tmsdk' folder)."""
    cands = []
    if explicit:
        cands.append(explicit)
    for root in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), "C:\\"):
        if not root:
            continue
        for pat in ("Thrustmaster*/tmsdk", "Thrustmaster*/*/tmsdk", "Thrustmaster*/*/*/tmsdk", "Guillemot*/tmsdk"):
            cands += glob.glob(os.path.join(root, pat))
    for c in cands:
        if os.path.isfile(os.path.join(c, "tm_api_lib_x64.dll")):
            return c
    return None


def pe_machine(path):
    """Return 'x64', 'x86' or None for a PE file, without loading it."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
        off, = struct.unpack_from("<I", head, 0x3C)
        if head[off:off + 4] != b"PE\0\0":
            return None
        mach, = struct.unpack_from("<H", head, off + 4)
        return {0x8664: "x64", 0x14C: "x86"}.get(mach)
    except (OSError, struct.error):
        return None


def dll_candidates(sdk_dir, explicit=None):
    """Places where the 64-bit DLL that exports tm_api_* may live, most likely first."""
    if explicit:
        return [explicit]
    sysroot = os.environ.get("SystemRoot", "C:\\Windows")
    c = [os.path.join(sysroot, "System32", "tmeffcpl.dll"),       # 64-bit copy keeps the plain name
         os.path.join(sysroot, "System32", "tmeffcpl64.dll")]
    for d in (sdk_dir, os.path.dirname(sdk_dir) if sdk_dir else None):
        if d:
            c += [os.path.join(d, "tmeffcpl64.dll"), os.path.join(d, "tmeffcpl.dll")]
    seen, out = set(), []
    for x in c:
        if x.lower() not in seen:
            seen.add(x.lower()); out.append(x)
    return out


REQUIRED_EXPORTS = ("tm_api_init", "tm_api_exit", "tm_api_get_device_count", "tm_api_get_device_info", "tm_api_open_device",
                    "tm_api_close_device", "tm_api_set_properties", "tm_api_update")


def load_sdk_dll(candidates):
    """Try each candidate; return (WinDLL, path). Raise with a full report if none works."""
    report = []
    for path in candidates:
        if not os.path.isfile(path):
            report.append("%s: not present" % path)
            continue
        mach = pe_machine(path)
        if mach != "x64":
            report.append("%s: is %s, need x64" % (path, mach))
            continue
        try:
            dll = ctypes.WinDLL(path, winmode=0x8)      # LOAD_WITH_ALTERED_SEARCH_PATH: find deps next to it
        except OSError as e:
            report.append("%s: failed to load (%s)" % (path, e))
            continue
        missing = [n for n in REQUIRED_EXPORTS if not hasattr(dll, n)]
        if missing:
            report.append("%s: loaded but lacks %s" % (path, ", ".join(missing)))
            continue
        return dll, path
    raise RuntimeError("No usable SDK DLL. Tried:\n  " + "\n  ".join(report))


def call_sdk(name, fn, *args):
    """Call an SDK function; turn a crash (access violation etc.) into an error that names the call."""
    try:
        return fn(*args)
    except OSError as e:
        raise RuntimeError("%s crashed: %s" % (name, e)) from e


def build_init_block(hwnd):
    """The setup block the SDK test app passes to tm_api_init: DWORD size (12) + 8-byte window handle."""
    return struct.pack("<IQ", 12, int(hwnd or 0))


def wait_for_devices(get_count, pump, timeout, sleep=time.sleep, clock=time.monotonic, interval=0.1):
    """The SDK connects in a background thread after tm_api_init, so poll until it reports devices.
    get_count() -> (rc, count); pump() services the SDK. Returns (rc, count, distinct_results)."""
    deadline = clock() + timeout
    seen, rc, n = [], -1, 0
    while True:
        pump()
        rc, n = get_count()
        if (rc, n) not in seen:
            seen.append((rc, n))
        if rc == 0 and n > 0:
            return rc, n, seen
        if clock() >= deadline:
            return rc, n, seen
        sleep(interval)


def find_game_window(exe_names=("acs.exe", "assettocorsa.exe")):
    """Return the HWND of the visible top-level window owned by Assetto Corsa, or 0."""
    if os.name != "nt":
        return 0
    from ctypes import wintypes
    u, k = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("kernel32", use_last_error=True)
    u.IsWindowVisible.argtypes = [ctypes.c_void_p]
    u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    u.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    found = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _lparam):
        if not u.IsWindowVisible(hwnd) or u.GetWindowTextLengthW(hwnd) == 0:
            return True
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = k.OpenProcess(0x1000, False, pid.value)                 # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            buf, size = ctypes.create_unicode_buffer(520), wintypes.DWORD(520)
            if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                if os.path.basename(buf.value).lower() in exe_names:
                    found.append(hwnd)
            k.CloseHandle(h)
        return True

    u.EnumWindows.argtypes = [proc, ctypes.c_void_p]
    u.EnumWindows(proc(cb), None)
    return found[0] if found else 0


def console_window():
    if os.name != "nt":
        return 0
    k = ctypes.WinDLL("kernel32")
    k.GetConsoleWindow.restype = ctypes.c_void_p
    return k.GetConsoleWindow() or 0


def foreground_window():
    if os.name != "nt":
        return 0
    u = ctypes.WinDLL("user32")
    u.GetForegroundWindow.restype = ctypes.c_void_p
    return u.GetForegroundWindow() or 0


SDK_WINDOW = {"kind": None}      # what the current SDK session was given as its window


def resolve_hwnd(mode, explicit=None):
    """mode: 'game' (Assetto Corsa's window, fall back to console), 'console', or explicit handle."""
    if explicit:
        return explicit, "explicit"
    if mode == "game":
        h = find_game_window()
        if h:
            return h, "Assetto Corsa window"
        log.warning("Assetto Corsa window not found; using this console window instead")
    return console_window(), "console window"


INFO_SIZE = 0x648       # size of the device-info structure; its first DWORD must hold this value


THRUSTMASTER_VID = 0x044F
KNOWN_MODELS = {0xB696: "T248"}      # only models seen in real captures; unknown ones are shown by id


def parse_device_info(raw):
    """Decode the structure filled in by tm_api_get_device_info.
    Layout traced from the SDK test app: the device path at +0x210, then the capability block at the
    end of the 0x648-byte structure."""
    if len(raw) < INFO_SIZE:
        raise ValueError("device info too short")
    path = raw[0x210:0x210 + 0x104 * 2].decode("utf-16-le", errors="ignore").split("\x00", 1)[0].strip()
    axes_mask, = struct.unpack_from("<I", raw, 0x62C)
    axes, buttons, analog, povs, encoders, vec4, motors, fslots, audio, leds, screens = \
        struct.unpack_from("<11H", raw, 0x630)
    caps = (axes, buttons, analog, povs, encoders, vec4, motors, fslots, audio, leds, screens)
    m = re.search(r"vid_([0-9a-f]{4})&pid_([0-9a-f]{4})", path, re.I)
    vid, pid = (int(m.group(1), 16), int(m.group(2), 16)) if m else (None, None)
    if vid == THRUSTMASTER_VID and pid in KNOWN_MODELS:
        name = "Thrustmaster " + KNOWN_MODELS[pid]
    elif vid is not None:
        name = "device %04X:%04X" % (vid, pid)
    else:
        name = path or "(unnamed)"
    return {"name": name, "path": path, "vid": vid, "pid": pid,
            "caps_ok": all(c <= 64 for c in caps),         # absurd counts mean the layout was misread
            "axes": axes, "axes_mask": axes_mask, "buttons": buttons, "analog_buttons": analog,
            "povs": povs, "encoders": encoders, "vec4": vec4, "motors": motors, "force_slots": fslots,
            "audio": audio, "leds": leds, "screens": screens}


def describe_device(i, info):
    if not info.get("caps_ok", True):
        return "#%d %s | capabilities unreadable (layout mismatch)" % (i, info["name"])
    return ("#%d %s | screens=%d leds=%d motors=%d force_slots=%d audio=%d | axes=%d buttons=%d povs=%d"
            % (i, info["name"], info["screens"], info["leds"], info["motors"], info["force_slots"],
               info["audio"], info["axes"], info["buttons"], info["povs"]))


def pick_device(infos, requested=None):
    """Choose which SDK device to drive. requested: an index, or None for automatic.
    Automatic prefers a device with a screen, then one with LEDs, then device 0."""
    if not infos:
        raise RuntimeError("no devices to choose from")
    if requested is not None:
        if not 0 <= requested < len(infos):
            raise RuntimeError("device %d does not exist (the SDK reports %d device(s))" % (requested, len(infos)))
        return requested, "chosen with --device"
    reliable = [i for i, inf in enumerate(infos) if inf.get("caps_ok", True)]
    for i in reliable:
        if infos[i]["screens"] > 0:
            return i, "first device with a screen"
    for i in reliable:
        if infos[i]["leds"] > 0:
            return i, "no screen found; first device with LEDs"
    for i, inf in enumerate(infos):
        if inf.get("vid") == THRUSTMASTER_VID:
            return i, "capabilities not readable; first Thrustmaster device"
    return 0, "no screen or LEDs reported; using device 0"


class TmSdk:
    """Thin ctypes wrapper over tm_api_* with the signatures seen in the SDK test app."""

    def __init__(self, sdk_dir=None, dll=None):
        if os.name != "nt":
            raise RuntimeError("The Thrustmaster SDK only runs on Windows")
        self.sdk_dir = find_sdk_dir(sdk_dir)
        # The library finds its helper DLL by name, so load the helper first from its real
        # folder; Windows then reuses that module when the name is requested again.
        if self.sdk_dir:
            os.add_dll_directory(self.sdk_dir)
            os.environ["PATH"] = self.sdk_dir + os.pathsep + os.environ.get("PATH", "")
            ctypes.WinDLL(os.path.join(self.sdk_dir, "tm_api_lib_x64.dll"))
            log.info("SDK folder: %s", self.sdk_dir)
        else:
            log.warning("tm_api_lib_x64.dll not found; use --sdk-dir with the tmsdk folder")
        self.dll, path = load_sdk_dll(dll_candidates(self.sdk_dir, dll))
        log.info("Loaded SDK DLL: %s", path)
        d = self.dll
        d.tm_api_init.argtypes = [ctypes.c_int, ctypes.c_void_p];                d.tm_api_init.restype = ctypes.c_int
        d.tm_api_exit.argtypes = [];                                               d.tm_api_exit.restype = ctypes.c_int
        d.tm_api_get_device_count.argtypes = [ctypes.POINTER(ctypes.c_int)];       d.tm_api_get_device_count.restype = ctypes.c_int
        d.tm_api_open_device.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        d.tm_api_open_device.restype = ctypes.c_int
        d.tm_api_get_device_info.argtypes = [ctypes.c_int, ctypes.c_void_p];       d.tm_api_get_device_info.restype = ctypes.c_int
        d.tm_api_close_device.argtypes = [ctypes.c_int];                           d.tm_api_close_device.restype = ctypes.c_int
        d.tm_api_set_properties.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_void_p]
        d.tm_api_set_properties.restype = ctypes.c_int
        d.tm_api_update.argtypes = [ctypes.c_int, ctypes.c_void_p];                d.tm_api_update.restype = ctypes.c_int
        self.handle = None
        self._rec = ctypes.create_string_buffer(RECORD_SIZE)
        self._upd = ctypes.create_string_buffer(0x2000)       # the app clears 0x140c bytes; be generous

    def start(self, hwnd=0, timeout=8.0):
        """Initialise the SDK and wait for it to find devices. Returns the device count."""
        d = self.dll
        block = ctypes.create_string_buffer(build_init_block(hwnd), 12)
        rc = call_sdk("tm_api_init", d.tm_api_init, 0, ctypes.addressof(block))
        log.info("tm_api_init(hwnd=0x%x) -> %s", hwnd or 0, rc)
        if rc not in (0, -4):                                      # -4: already initialised in this process
            raise RuntimeError("tm_api_init failed with code %s" % rc)

        def get_count():
            n = ctypes.c_int(0)
            r = call_sdk("tm_api_get_device_count", d.tm_api_get_device_count, ctypes.byref(n))
            return r, n.value

        rc, n, seen = wait_for_devices(get_count, self.update, timeout)
        log.info("tm_api_get_device_count results while waiting (rc, count): %s", seen)
        if rc != 0 or n <= 0:
            raise RuntimeError("SDK reports no devices after %.0fs (rc=%s count=%s). Is the wheel plugged in "
                               "and the Thrustmaster driver installed?" % (timeout, rc, n))
        return n

    def enumerate(self, count):
        """Ask the SDK what each device is and what it can do."""
        infos = []
        for i in range(count):
            buf = ctypes.create_string_buffer(INFO_SIZE)
            struct.pack_into("<I", buf, 0, INFO_SIZE)
            rc = call_sdk("tm_api_get_device_info", self.dll.tm_api_get_device_info, i, ctypes.addressof(buf))
            if rc != 0:
                log.warning("tm_api_get_device_info(%d) -> rc=%s", i, rc)
                infos.append({"name": "(unknown)", "path": "", "vid": None, "pid": None, "caps_ok": False,
                              "screens": 0, "leds": 0, "motors": 0, "force_slots": 0,
                              "audio": 0, "axes": 0, "buttons": 0, "povs": 0})
                continue
            info = parse_device_info(buf.raw)
            log.debug("device %d capability block: %s", i, buf.raw[0x62C:INFO_SIZE].hex())
            infos.append(info)
            log.info("device %s", describe_device(i, info))
        return infos

    def connect(self, device_index=None, hwnd=0, timeout=8.0, enumerate_devices=True):
        """device_index None = choose automatically (a device with a screen first).
        enumerate_devices=False skips tm_api_get_device_info and opens the device directly."""
        n = self.start(hwnd, timeout)
        if enumerate_devices:
            infos = self.enumerate(n)
            idx, why = pick_device(infos, device_index)
            log.info("Driving device #%d (%s): %s", idx, infos[idx]["name"], why)
            if infos[idx].get("caps_ok", True) and infos[idx]["screens"] == 0:
                log.warning("This device reports no screen; LEDs or nothing may respond")
        else:
            idx = device_index or 0
            log.info("Not enumerating devices (--no-enumerate); opening device #%d", idx)
        h = ctypes.c_int(0)
        rc = call_sdk("tm_api_open_device", self.dll.tm_api_open_device, idx, ctypes.byref(h))
        log.info("tm_api_open_device(%s) -> rc=%s handle=0x%x (foreground window now 0x%x)",
                 idx, rc, h.value, foreground_window())
        if rc != 0:
            raise RuntimeError("tm_api_open_device failed with code %s" % rc)
        self.handle = h.value

    def set_prop(self, pid, ptype, value):
        write_record(self._rec, pid, ptype, value)
        return call_sdk("tm_api_set_properties", self.dll.tm_api_set_properties,
                        self.handle, CATEGORY_GAME_DATA, ctypes.addressof(self._rec))

    def update(self):
        return call_sdk("tm_api_update", self.dll.tm_api_update, 0, ctypes.addressof(self._upd))

    def close(self):
        try:
            if self.handle is not None:
                self.dll.tm_api_close_device(self.handle)
            self.dll.tm_api_exit()
        except Exception as e:                                # never raise during shutdown
            log.debug("close: %s", e)
        self.handle = None


def write_record(buf, pid, ptype, value):
    """Fill the 56-byte property record exactly like the SDK test app does."""
    raw = bytearray(RECORD_SIZE)
    struct.pack_into("<IIII", raw, 0, RECORD_SIZE, ptype, pid, 0)    # size, type, id, reserved
    if ptype == TYPE_INT:
        struct.pack_into("<i", raw, 0x10, int(value))
    elif ptype == TYPE_DOUBLE:
        struct.pack_into("<d", raw, 0x14, float(value))
    else:
        raise ValueError("unsupported type %r" % ptype)
    if hasattr(buf, "raw"):
        ctypes.memmove(ctypes.addressof(buf), bytes(raw), RECORD_SIZE)
    else:
        buf[:] = raw
    return bytes(raw)


class FakeSdk:
    """Stand-in used by --dry-run and the self-test. Records every call."""

    def __init__(self, fail_after=None):
        self.calls, self.connected, self.closed, self.fail_after = [], 0, 0, fail_after

    def connect(self, device_index=None, hwnd=0, timeout=0, enumerate_devices=True):
        self.connected += 1

    def set_prop(self, pid, ptype, value):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            return -1
        self.calls.append((pid, ptype, value))
        return 0

    def update(self):
        return 0

    def close(self):
        self.closed += 1


# --------------------------------------------------------------------------
# Sender: decides what to send and when, tracks failures, reconnects
# --------------------------------------------------------------------------
class _Waiting(Exception):
    pass


class Sender:
    FAILS_BEFORE_RECONNECT = 30

    def __init__(self, sdk_factory, race_info=False, refresh=1.0):
        self.factory, self.sdk = sdk_factory, None
        self.race_info, self.refresh = race_info, refresh
        self.last = {}
        self.fails = 0
        self.rc_seen = {}
        self.next_try, self.backoff, self.last_error = 0.0, 1.0, None
        self.asleep = False

    def sleep(self):
        """Put the wheel screen into standby and let go of the SDK. Safe to call more than once."""
        if self.asleep:
            return
        self.asleep = True
        if self.sdk is not None:
            try:
                self.sdk.set_prop(ID_POWER, TYPE_INT, POWER_STANDBY)
                self.sdk.update()
            except Exception as e:
                log.debug("standby send failed: %s", e)
            self.close()                        # the USB link is about to drop; reconnect after waking
        log.info("Wheel screen: standby")

    def wake(self):
        if not self.asleep:
            return
        self.asleep = False
        self.next_try, self.backoff = 0.0, 1.0   # reconnect at once; back-off covers a slow USB resume
        log.info("Wheel screen: waking up")

    def _ensure(self, now):
        if self.sdk is None:
            if now < self.next_try:
                raise _Waiting()
            sdk = None
            try:
                sdk = self.factory()
                sdk.connect()
            except Exception:
                self.next_try = now + self.backoff
                self.backoff = min(30.0, self.backoff * 2)         # 1, 2, 4 ... 30 seconds
                if sdk is not None:
                    try:
                        sdk.close()                                # tm_api_exit so the next try starts clean
                    except Exception as e:
                        log.debug("cleanup after failed connect: %s", e)
                raise
            self.sdk, self.last, self.fails = sdk, {}, 0
            self.backoff, self.last_error = 1.0, None
            log.info("SDK connected")


    def _send(self, pid, ptype, value, now, eps=0.0):
        prev = self.last.get(pid)
        if prev is not None:
            same = abs(prev[0] - value) <= eps if ptype == TYPE_DOUBLE else prev[0] == value
            if same and now - prev[1] < self.refresh:
                return True
        rc = self.sdk.set_prop(pid, ptype, value)
        n = self.rc_seen.setdefault((pid, rc), 0)
        self.rc_seen[(pid, rc)] = n + 1
        if n == 0:
            log.info("set_properties id=0x%02x -> rc=%s", pid, rc)       # log each distinct result once
        if rc == 0:
            self.last[pid] = (value, now)
            return True
        return False

    def send(self, t, now=None):
        if self.asleep:
            return
        now = time.monotonic() if now is None else now
        try:
            self._ensure(now)
            ok = True
            # record 0x49 disabled: it is not screen power; sending it made the screen show P
            # ok &= self._send(ID_POWER, TYPE_INT, POWER_AWAKE, now)       # re-asserted every second: recovers a screen that dropped to standby
            ok &= self._send(ID_GEAR, TYPE_INT, ord(t.gear[0]), now)
            ok &= self._send(ID_RPM, TYPE_INT, t.rpm, now)
            ok &= self._send(ID_RPM_PERCENT, TYPE_DOUBLE, round(t.rpm_percent, 1), now, eps=0.05)
            ok &= self._send(ID_SPEED, TYPE_DOUBLE, round(t.speed_kmh, 1), now, eps=0.05)
            ok &= self._send(ID_CURRENT_LAP, TYPE_INT, t.current_ms, now)
            if t.last_ms > 0:
                ok &= self._send(ID_LAST_LAP, TYPE_INT, t.last_ms, now)
            if t.best_ms > 0:
                ok &= self._send(ID_BEST_LAP, TYPE_INT, t.best_ms, now)
            if self.race_info:
                ok &= self._send(ID_POSITION, TYPE_INT, t.position, now)
                ok &= self._send(ID_CARS, TYPE_INT, t.cars, now)
                ok &= self._send(ID_LAP_CURRENT, TYPE_INT, t.lap_current, now)
                ok &= self._send(ID_LAP_COUNT, TYPE_INT, t.lap_count, now)
            self.sdk.update()
            self.fails = 0 if ok else self.fails + 1
        except _Waiting:
            return                                 # still backing off after a failed connect
        except Exception as e:
            if str(e) != self.last_error:          # log each distinct problem once, not every retry
                log.warning("send failed: %s", e)
                self.last_error = str(e)
            self.fails += 1
        if self.fails >= self.FAILS_BEFORE_RECONNECT:
            log.warning("%d failures in a row: reconnecting to the SDK", self.fails)
            self.close()

    def close(self):
        if self.sdk is not None:
            self.sdk.close()
        self.sdk = None


SOURCES = {"ac": AcReader}          # add other games here, e.g. a GTR2 reader


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------
def simulate_frame(t_sec):
    """A smooth fake drive: revs up through gears 1..5 and back, with a running lap clock."""
    cyc = (t_sec % 30.0) / 30.0
    up = cyc < 0.5
    prog = cyc * 2 if up else (1 - cyc) * 2
    gear_n = 1 + min(4, int(prog * 5))
    within = (prog * 5) % 1.0
    rpm = int(2500 + within * 4500)
    t = Telemetry(live=True)
    t.gear, t.rpm = str(gear_n), rpm
    t.rpm_percent = rpm_to_percent(rpm, 7500)
    t.speed_kmh = prog * 180.0
    t.current_ms = int((t_sec % 90.0) * 1000)
    t.last_ms, t.best_ms = 92300, 90800
    t.position, t.cars, t.lap_current, t.lap_count = 3, 12, 2, 5
    return t


def clock_ms(now=None):
    """Time of day as a fake lap time: 14:20 becomes 14 min 20 s, which the screen shows as 14:20.000."""
    lt = time.localtime(now)
    return (lt.tm_hour * 60 + lt.tm_min) * 1000


def idle_frame(clock=False, now=None):
    t = Telemetry(live=False, gear="N")
    if clock:
        t.current_ms = clock_ms(now)
    return t


# --------------------------------------------------------------------------
# Follow the computer's power state (sleep / resume / display off / shutdown)
# --------------------------------------------------------------------------
WM_POWERBROADCAST, WM_QUERYENDSESSION, WM_ENDSESSION = 0x218, 0x11, 0x16
PBT_APMSUSPEND, PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC, PBT_POWERSETTINGCHANGE = 4, 7, 18, 0x8013
GUID_CONSOLE_DISPLAY_STATE = "6fe69556-704a-47a0-8f24-c28d936fda47"     # data: 0 off, 1 on, 2 dimmed


class PowerState:
    """Pure state machine (no Windows calls) so it can be tested anywhere."""

    def __init__(self, follow_display=True):
        self.suspended = self.ending = False
        self.display_on = True
        self.follow_display = follow_display

    @property
    def standby(self):
        return self.suspended or self.ending or (self.follow_display and not self.display_on)

    def on_event(self, msg, wparam=0, setting_guid=None, data=None):
        if msg == WM_POWERBROADCAST:
            if wparam == PBT_APMSUSPEND:
                self.suspended = True
            elif wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                self.suspended = False
                self.display_on = True          # a missed "display on" message must not leave the screen in standby
            elif wparam == PBT_POWERSETTINGCHANGE and setting_guid == GUID_CONSOLE_DISPLAY_STATE and data is not None:
                self.display_on = data != 0
        elif msg == WM_QUERYENDSESSION:
            self.ending = True
        elif msg == WM_ENDSESSION and wparam == 0:
            self.ending = False                     # shutdown was cancelled


class PowerWatcher:
    """Listens for Windows power messages on a hidden window in its own thread."""

    def __init__(self, follow_display=True):
        self.state = PowerState(follow_display)
        self.ok = False
        if os.name == "nt":
            import threading
            self._ready = threading.Event()
            threading.Thread(target=self._run, name="power-watcher", daemon=True).start()
            self._ready.wait(3.0)

    @property
    def standby(self):
        return self.state.standby

    def _run(self):
        try:
            from ctypes import wintypes
            u, k = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("kernel32")
            LRESULT = ctypes.c_ssize_t
            WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM)

            class WNDCLASS(ctypes.Structure):
                _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                            ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE),
                            ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
                            ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]

            u.DefWindowProcW.restype = LRESULT
            u.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
            u.CreateWindowExW.restype = wintypes.HWND
            u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                          ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                          wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
            u.RegisterPowerSettingNotification.restype = wintypes.HANDLE
            u.RegisterPowerSettingNotification.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
            k.GetModuleHandleW.restype = wintypes.HINSTANCE

            def proc(hwnd, msg, wparam, lparam):
                try:
                    guid = data = None
                    if msg == WM_POWERBROADCAST and wparam == PBT_POWERSETTINGCHANGE and lparam:
                        raw = ctypes.string_at(lparam, 24)          # GUID, DWORD length, first data bytes
                        guid = str(__import__("uuid").UUID(bytes_le=raw[:16]))
                        data = raw[20]
                    before = self.state.standby
                    if msg in (WM_POWERBROADCAST, WM_QUERYENDSESSION, WM_ENDSESSION):
                        log.info("Power event: msg=0x%x wparam=0x%x%s", msg, int(wparam),
                                 "" if guid is None else " setting=%s data=%s" % (guid, data))
                    self.state.on_event(msg, int(wparam), guid, data)
                    if self.state.standby != before:
                        log.info("Power state changed: %s", "standby" if self.state.standby else "active")
                    if msg == WM_QUERYENDSESSION:
                        return 1                                    # do not block the shutdown
                except Exception as e:
                    log.debug("power message error: %s", e)
                return u.DefWindowProcW(hwnd, msg, wparam, lparam)

            self._proc = WNDPROC(proc)                              # keep a reference alive
            cls = WNDCLASS()
            cls.lpfnWndProc, cls.hInstance, cls.lpszClassName = self._proc, k.GetModuleHandleW(None), "T248BridgePower"
            u.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
            u.RegisterClassW(ctypes.byref(cls))
            # a hidden top-level window: message-only windows do not receive power broadcasts
            hwnd = u.CreateWindowExW(0, "T248BridgePower", "T248 bridge power", 0, 0, 0, 0, 0, None, None, cls.hInstance, None)
            guid_bytes = (ctypes.c_ubyte * 16).from_buffer_copy(__import__("uuid").UUID(GUID_CONSOLE_DISPLAY_STATE).bytes_le)
            u.RegisterPowerSettingNotification(hwnd, ctypes.byref(guid_bytes), 0)
            self.ok = bool(hwnd)
            if not self.ok:
                log.warning("could not create the power-watching window; the screen will not follow standby")
            self._ready.set()
            msg = wintypes.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                u.TranslateMessage(ctypes.byref(msg)); u.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            log.warning("power watching unavailable: %s", e)
            self._ready.set()


def follow_power(sender, watcher):
    """Called every loop: put the wheel to sleep or wake it to match the computer."""
    if sender is None or watcher is None:
        return
    if watcher.standby:
        sender.sleep()
    else:
        sender.wake()


_CTRL_HANDLER = []        # keeps the Windows callback alive for the life of the process


def install_cleanup(callback, on_shutdown=None):
    """Run callback when the program exits for any reason, including closing the console window,
    so the SDK session is not left half open (which can leave the wheel screen unresponsive)."""
    import atexit
    atexit.register(callback)
    if os.name == "nt":
        try:
            k = ctypes.WinDLL("kernel32")
            routine = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

            def handler(event):                  # 2 close window, 5 log off, 6 shutdown
                if event in (2, 5, 6):
                    try:
                        if event in (5, 6) and on_shutdown:
                            on_shutdown()                  # log off / shutdown: put the screen to standby first
                        callback()
                    except Exception:
                        pass
                return 0                         # let the normal handling carry on
            _CTRL_HANDLER.append(routine(handler))
            k.SetConsoleCtrlHandler(_CTRL_HANDLER[-1], True)
        except Exception as e:
            log.debug("could not install console handler: %s", e)


def run_follow(sender, rate, dry_run, reader, publisher=None, power=None, idle_clock=False):
    """sender: wheel output or None; publisher: external outputs or None."""
    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *a: stop.update(flag=True))
    period, connected, last_state, dead_since = 1.0 / rate, False, None, None
    checker = PlausibilityChecker()
    log.info("Following Assetto Corsa at %.0f Hz. Press Ctrl+C to stop.", rate)
    try:
        while not stop["flag"]:
            t0 = time.monotonic()
            follow_power(sender, power)
            if not connected:
                connected = reader.open()
                if connected:
                    log.info("Assetto Corsa shared memory found")
                else:
                    time.sleep(1.0)
                    continue
            try:
                t = reader.read()
                values = reader.values()
            except Exception as e:
                log.warning("lost Assetto Corsa: %s", e)
                reader.close(); connected = False
                if sender is not None:
                    sender.send(idle_frame())
                continue
            checker.check(values, t.live)
            if publisher is not None:
                publisher.publish(values, t.live)
            if t.live:
                dead_since = None
            else:
                dead_since = dead_since or time.monotonic()
                waited = time.monotonic() - dead_since
                if waited > 10.0:
                    if should_recycle(waited, bool(find_game_window())):
                        if (sender is not None and sender.sdk is not None
                                and SDK_WINDOW["kind"] == "Assetto Corsa window"):   # a console-window session has nothing to refresh
                            sender.send(idle_frame())                  # leave the wheel screen tidy
                            sender.close()                             # re-init with the next game window
                        reader.close(); connected = False              # the game has gone: look for it again
                    dead_since = time.monotonic()                      # in a menu: check again in 10 s
            frame = t if t.live else idle_frame(idle_clock)
            state = (frame.live, frame.gear)
            if state != last_state:
                log.info("state: live=%s gear=%s", frame.live, frame.gear)
                last_state = state
            if dry_run:
                print("gear=%s rpm=%d (%.1f%%) speed=%.1f cur=%d last=%d best=%d" % (
                    frame.gear, frame.rpm, frame.rpm_percent, frame.speed_kmh, frame.current_ms, frame.last_ms, frame.best_ms))
            if sender is not None:
                sender.send(frame)
            time.sleep(max(0.0, period - (time.monotonic() - t0)))
    finally:
        if sender is not None:
            if sender.sdk is not None:                 # only reset the screen if we ever connected
                sender.send(idle_frame())
            sender.close()
        if publisher is not None:
            publisher.close()
        reader.close()


def run_simulate(sender, rate, publisher=None, power=None):
    """sender: wheel output or None; publisher: external outputs or None."""
    signal.signal(signal.SIGINT, signal.default_int_handler)
    t0 = time.monotonic()
    log.info("Simulating telemetry. Ctrl+C to stop.")
    try:
        while True:
            t = simulate_frame(time.monotonic() - t0)
            follow_power(sender, power)
            if sender is not None:
                sender.send(t)
            if publisher is not None:
                publisher.publish(core_values(t), True)
            time.sleep(1.0 / rate)
    except KeyboardInterrupt:
        pass
    finally:
        if sender is not None:
            if sender.sdk is not None:
                sender.send(idle_frame())
            sender.close()
        if publisher is not None:
            publisher.close()


def run_list_devices(factory):
    sdk = factory()
    try:
        hwnd, _ = resolve_hwnd("console")
        n = sdk.start(hwnd, 8.0)
        infos = sdk.enumerate(n)
        print("\nThe SDK reports %d device(s):" % n)
        for i, inf in enumerate(infos):
            print("  " + describe_device(i, inf))
        idx, why = pick_device(infos, None)
        print("\nAutomatic choice: device #%d (%s)" % (idx, why))
        return 0
    except Exception as e:
        log.error("could not list devices: %s", e)
        return 1
    finally:
        sdk.close()


def run_clock_test(sender, seconds=20.0):
    """Show the time of day in the lap-time field for a while, so you can see how the screen presents it."""
    log.info("CLOCK TEST: showing %s as the current lap time for %.0f s. Watch the screen.",
             time.strftime("%H:%M"), seconds)
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            sender.send(idle_frame(clock=True))
            time.sleep(0.2)
    finally:
        sender.close()
    return 0


def run_power_test(sender, step=4.0):
    """Walk the power record through its three values so the person can note what the screen does."""
    log.info("POWER TEST: showing a test frame, then power state 0, 1, 2, 1, 0 (%.0f s each). Watch the screen.", step)
    frame = simulate_frame(10.0)
    try:
        for value in (POWER_AWAKE, POWER_STANDBY, POWER_WAKING, POWER_AWAKE, POWER_WAKING, POWER_STANDBY, POWER_AWAKE):
            sender.send(frame)
            if sender.sdk is None:
                log.error("POWER TEST: could not connect to the wheel")
                return 1
            rc = sender.sdk.set_prop(ID_POWER, TYPE_INT, value)
            sender.sdk.update()
            log.info("POWER TEST: state %d sent (rc=%s)", value, rc)
            print("power state %d  (0 = standby guess, 2 = awake guess)" % value)
            end = time.monotonic() + step
            while time.monotonic() < end:
                sender.send(frame)
                time.sleep(0.1)
    finally:
        sender.close()
    return 0


def run_probe(sender, hold=8.0):
    log.info("PROBE: connecting and sending one frame (gear 3, 4500 rpm, 90 km/h)")
    t = Telemetry(live=True, gear="3", rpm=4500, rpm_percent=60.0, speed_kmh=90.0, current_ms=65432)
    log.info("Holding the test frame for %.0f seconds: look at the wheel screen now.", hold)
    end = time.monotonic() + hold
    while time.monotonic() < end:
        sender.send(t)
        time.sleep(0.1)
    log.info("PROBE result: return codes seen (property id, code): count -> %s",
             {("0x%02x" % k[0], k[1]): v for k, v in sender.rc_seen.items()})
    ok = sender.sdk is not None and all(k[1] == 0 for k in sender.rc_seen)
    log.info("PROBE %s", "OK: look at the wheel screen for gear 3" if ok else "FAILED: send t248_bridge.log back for analysis")
    sender.close()
    return 0 if ok else 1


# --------------------------------------------------------------------------
# Self-test (no hardware, runs on any OS)
# --------------------------------------------------------------------------
def selftest():
    import unittest

    def pages(status=2, gear=4, rpm=6200, speed=123.4, cur=65432, last=70100, best=69700,
              pos=5, laps=10, completed=2, cars=20, max_rpm=7000):
        phys = bytearray(PHYSICS_SIZE); gra = bytearray(GRAPHICS_SIZE); sta = bytearray(STATIC_SIZE)
        struct.pack_into("<ifffiiff", phys, 0, 1, 0.5, 0.0, 30.0, gear, rpm, 0.0, speed)
        struct.pack_into("<i", gra, 4, status)
        struct.pack_into("<iiiii", gra, 132, completed, pos, cur, last, best)
        struct.pack_into("<i", gra, 172, laps)
        struct.pack_into("<i", sta, 64, cars)
        struct.pack_into("<i", sta, 412, max_rpm)
        return bytes(phys), bytes(gra), bytes(sta)

    class T(unittest.TestCase):
        def test_gear_mapping(self):
            self.assertEqual([ac_gear_to_char(g) for g in (0, 1, 2, 3, 7)], ["R", "N", "1", "2", "6"])
            self.assertEqual(ac_gear_to_char(40), "9")

        def test_percent(self):
            self.assertAlmostEqual(rpm_to_percent(3500, 7000), 50.0)
            self.assertEqual(rpm_to_percent(9000, 7000), 100.0)
            self.assertEqual(rpm_to_percent(100, 0), 0.0)

        def test_record_layout(self):
            raw = write_record(bytearray(RECORD_SIZE), ID_GEAR, TYPE_INT, ord("4"))
            self.assertEqual(len(raw), 56)
            self.assertEqual(struct.unpack_from("<IIII", raw, 0), (56, 2, 6, 0))
            self.assertEqual(struct.unpack_from("<i", raw, 0x10)[0], 52)
            raw = write_record(bytearray(RECORD_SIZE), ID_RPM_PERCENT, TYPE_DOUBLE, 49.4)
            self.assertEqual(struct.unpack_from("<IIII", raw, 0), (56, 4, 8, 0))
            self.assertAlmostEqual(struct.unpack_from("<d", raw, 0x14)[0], 49.4)
            self.assertEqual(struct.unpack_from("<i", raw, 0x10)[0], 0)

        def test_ctypes_buffer(self):
            buf = ctypes.create_string_buffer(RECORD_SIZE)
            write_record(buf, ID_SPEED, TYPE_DOUBLE, 120.0)
            self.assertEqual(struct.unpack_from("<III", buf.raw, 0), (56, 4, 9))

        def test_parse_live(self):
            t, mr = parse_ac_pages(*pages(), 0)
            self.assertTrue(t.live)
            self.assertEqual((t.gear, t.rpm, mr), ("3", 6200, 7000))
            self.assertAlmostEqual(t.rpm_percent, 88.57, places=1)
            self.assertAlmostEqual(t.speed_kmh, 123.4, places=1)
            self.assertEqual((t.current_ms, t.last_ms, t.best_ms), (65432, 70100, 69700))
            self.assertEqual((t.position, t.cars, t.lap_current, t.lap_count), (5, 20, 3, 10))

        def test_parse_states(self):
            self.assertFalse(parse_ac_pages(*pages(status=0), 0)[0].live)
            self.assertFalse(parse_ac_pages(*pages(status=1), 0)[0].live)      # replay ignored
            self.assertTrue(parse_ac_pages(*pages(status=3), 0)[0].live)       # pause keeps last values

        def test_bad_max_rpm_falls_back(self):
            t, mr = parse_ac_pages(*pages(max_rpm=0), 8000)
            self.assertEqual(mr, 8000)
            t, mr = parse_ac_pages(*pages(max_rpm=0, rpm=5000), 0)
            self.assertEqual(mr, 5000)

        def test_sender_sends_core_fields_and_throttles(self):
            fake = FakeSdk(); s = Sender(lambda: fake)
            t = Telemetry(live=True, gear="2", rpm=3000, rpm_percent=40.0, speed_kmh=50.0, current_ms=1000)
            s.send(t, now=0.0)
            ids = {c[0] for c in fake.calls}
            self.assertEqual(ids - {ID_POWER}, {ID_GEAR, ID_RPM, ID_RPM_PERCENT, ID_SPEED, ID_CURRENT_LAP})   # POWER = wake-up sent once on connect
            n = len(fake.calls)
            s.send(t, now=0.1)                                   # unchanged and recent: nothing new
            self.assertEqual(len(fake.calls), n)
            s.send(t, now=1.5)                                   # refresh interval passed
            self.assertEqual(len(fake.calls), 2 * n)

        def test_sender_race_info_optional(self):
            fake = FakeSdk(); s = Sender(lambda: fake, race_info=True)
            s.send(Telemetry(live=True, position=3, cars=12, lap_current=2, lap_count=5), now=0.0)
            ids = {c[0] for c in fake.calls}
            self.assertTrue({ID_POSITION, ID_CARS, ID_LAP_CURRENT, ID_LAP_COUNT} <= ids)

        def test_power_state_machine(self):
            ps = PowerState()
            self.assertFalse(ps.standby)
            ps.on_event(WM_POWERBROADCAST, PBT_APMSUSPEND); self.assertTrue(ps.standby)
            ps.on_event(WM_POWERBROADCAST, PBT_APMRESUMEAUTOMATIC); self.assertFalse(ps.standby)
            ps.on_event(WM_POWERBROADCAST, PBT_POWERSETTINGCHANGE, GUID_CONSOLE_DISPLAY_STATE, 0); self.assertTrue(ps.standby)
            ps.on_event(WM_POWERBROADCAST, PBT_POWERSETTINGCHANGE, GUID_CONSOLE_DISPLAY_STATE, 2); self.assertFalse(ps.standby)
            ps.on_event(WM_QUERYENDSESSION); self.assertTrue(ps.standby)
            ps.on_event(WM_ENDSESSION, 0); self.assertFalse(ps.standby)
            ps2 = PowerState(follow_display=False)
            ps2.on_event(WM_POWERBROADCAST, PBT_POWERSETTINGCHANGE, GUID_CONSOLE_DISPLAY_STATE, 0)
            self.assertFalse(ps2.standby)

        def test_sender_sleep_and_wake(self):
            fakes = []
            def factory():
                f = FakeSdk(); fakes.append(f); return f
            s = Sender(factory)
            t = Telemetry(live=True, rpm=3000, gear="3")
            s.send(t, now=0.0)
            self.assertNotIn(ID_POWER, [c[0] for c in fakes[0].calls])   # 0x49 is never sent while driving
            s.sleep(); s.sleep()
            self.assertIn((ID_POWER, TYPE_INT, POWER_STANDBY), fakes[0].calls)
            self.assertEqual(fakes[0].closed, 1)
            n = len(fakes[0].calls)
            s.send(t, now=1.0)                                   # asleep: nothing is sent, no reconnect
            self.assertEqual(len(fakes), 1); self.assertEqual(len(fakes[0].calls), n)
            s.wake(); s.send(t, now=2.0)
            self.assertEqual(len(fakes), 2)                      # fresh connection after resume
            self.assertNotIn(ID_POWER, [c[0] for c in fakes[1].calls])

        def test_idle_clock(self):
            ts = time.mktime((2026, 11, 16, 14, 20, 0, 0, 0, -1))
            self.assertEqual(clock_ms(ts), 860000)               # shown as 14:20.000
            self.assertEqual(idle_frame(True, ts).current_ms, 860000)
            self.assertEqual(idle_frame().current_ms, 0)

        def test_follow_power_drives_sender(self):
            class W: standby = False
            fake = FakeSdk(); s = Sender(lambda: fake); w = W()
            s.send(Telemetry(live=True), now=0.0)
            w.standby = True; follow_power(s, w); self.assertTrue(s.asleep)
            w.standby = False; follow_power(s, w); self.assertFalse(s.asleep)

        def test_reconnects_after_failures(self):
            made = []
            def factory():
                f = FakeSdk(fail_after=0 if not made else None); made.append(f); return f
            s = Sender(factory)
            t = Telemetry(live=True, rpm=1000)
            for i in range(Sender.FAILS_BEFORE_RECONNECT):
                s.send(t, now=float(i) * 2)
            self.assertIsNone(s.sdk)                              # dropped the broken connection
            self.assertEqual(made[0].closed, 1)
            s.send(t, now=100.0)                                  # new connection works
            self.assertEqual(len(made), 2)
            self.assertTrue(made[1].calls)

        def test_connect_error_backs_off(self):
            attempts = []
            class Boom:
                def connect(self, i=0): attempts.append(1); raise RuntimeError("no devices")
            s = Sender(lambda: Boom())
            s.send(Telemetry(), now=0.0)                       # attempt 1 fails, wait 1 s
            s.send(Telemetry(), now=0.5)                       # skipped
            s.send(Telemetry(), now=1.0)                       # attempt 2 fails, wait 2 s
            s.send(Telemetry(), now=2.5)                       # skipped
            s.send(Telemetry(), now=3.0)                       # attempt 3
            self.assertEqual(len(attempts), 3)
            self.assertIsNone(s.sdk)

        def _info_bytes(self, pid=0xB696, vid=0x044F, screens=1, leds=0, motors=2, junk_caps=False):
            b = bytearray(INFO_SIZE)
            struct.pack_into("<I", b, 0, INFO_SIZE)
            path = "\\\\?\\hid#vid_%04x&pid_%04x#6&1339b7b0&0&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}" % (vid, pid)
            b[0x210:0x210 + 2 * (len(path) + 1)] = (path + "\0").encode("utf-16-le")
            struct.pack_into("<I", b, 0x62C, 0x3F)
            if junk_caps:                                    # what the old, wrong layout produced: ASCII letters
                struct.pack_into("<11H", b, 0x630, 115, 116, 97, 100, 65, 101, 114, 32, 115, 116, 97)
            else:
                struct.pack_into("<11H", b, 0x630, 6, 25, 0, 1, 0, 0, motors, 8, 0, leds, screens)
            return bytes(b)

        def test_parse_device_info(self):
            inf = parse_device_info(self._info_bytes())
            self.assertEqual((inf["name"], inf["vid"], inf["pid"]), ("Thrustmaster T248", 0x044F, 0xB696))
            self.assertEqual((inf["screens"], inf["leds"], inf["motors"], inf["caps_ok"]), (1, 0, 2, True))
            self.assertEqual((inf["axes"], inf["buttons"], inf["force_slots"], inf["axes_mask"]), (6, 25, 8, 0x3F))
            self.assertEqual(parse_device_info(self._info_bytes(pid=0x1234))["name"], "device 044F:1234")
            with self.assertRaises(ValueError):
                parse_device_info(b"short")
            self.assertEqual(parse_device_info(bytes(INFO_SIZE))["name"], "(unnamed)")

        def test_absurd_capabilities_are_flagged(self):
            inf = parse_device_info(self._info_bytes(junk_caps=True))
            self.assertFalse(inf["caps_ok"])
            self.assertIn("unreadable", describe_device(0, inf))

        def test_pick_device(self):
            wheel = parse_device_info(self._info_bytes(screens=1))
            pedals = parse_device_info(self._info_bytes(pid=0x1111, screens=0, leds=0))
            leds_only = parse_device_info(self._info_bytes(pid=0x2222, screens=0, leds=8))
            self.assertEqual(pick_device([pedals, wheel])[0], 1)               # screen wins, wherever it is
            self.assertEqual(pick_device([pedals, leds_only])[0], 1)           # LEDs next best
            self.assertEqual(pick_device([pedals, pedals])[0], 0)              # nothing special: device 0
            self.assertEqual(pick_device([wheel, wheel], 1), (1, "chosen with --device"))
            with self.assertRaises(RuntimeError):
                pick_device([wheel], 3)
            with self.assertRaises(RuntimeError):
                pick_device([])

        def test_pick_device_unreadable_caps_falls_back_to_thrustmaster(self):
            other = parse_device_info(self._info_bytes(vid=0x1234, pid=1, junk_caps=True))
            tm = parse_device_info(self._info_bytes(junk_caps=True))
            idx, why = pick_device([other, tm])
            self.assertEqual(idx, 1)
            self.assertIn("not readable", why)

        def _fake_tmsdk(self, calls):
            """A TmSdk whose DLL is a stand-in that records the order of calls."""
            sdk = object.__new__(TmSdk)
            class Dll:
                def tm_api_init(d, flags, ptr): calls.append(("init", flags)); return 0
                def tm_api_get_device_count(d, ref): ref._obj.value = 1; calls.append(("count",)); return 0
                def tm_api_get_device_info(d, i, addr):
                    ctypes.memmove(addr, TestCase._info(), INFO_SIZE); calls.append(("info", i)); return 0
                def tm_api_open_device(d, i, ref): ref._obj.value = 0x400; calls.append(("open", i)); return 0
                def tm_api_update(d, flag, addr): return 0
                def tm_api_close_device(d, h): calls.append(("close", h)); return 0
                def tm_api_exit(d): calls.append(("exit",)); return 0
            sdk.dll, sdk.handle = Dll(), None
            sdk._rec, sdk._upd = ctypes.create_string_buffer(RECORD_SIZE), ctypes.create_string_buffer(0x2000)
            return sdk

        def test_sdk_call_order(self):
            TestCase._info = staticmethod(lambda: self._info_bytes())
            calls = []
            sdk = self._fake_tmsdk(calls)
            sdk.connect(None, 0x1234, 1.0)
            self.assertEqual([c[0] for c in calls], ["init", "count", "info", "open"])
            self.assertEqual(sdk.handle, 0x400)
            calls.clear()
            sdk2 = self._fake_tmsdk(calls)
            sdk2.connect(None, 0x1234, 1.0, enumerate_devices=False)
            self.assertEqual([c[0] for c in calls], ["init", "count", "open"])      # exactly the minimal sequence
            sdk2.close()
            self.assertEqual([c[0] for c in calls][-2:], ["close", "exit"])

        def test_sender_close_is_idempotent(self):
            fake = FakeSdk(); s = Sender(lambda: fake)
            s.send(Telemetry(live=True, rpm=1000), now=0.0)
            s.close(); s.close()
            self.assertEqual(fake.closed, 1)

        def test_gear_filter_hides_shift_flicker(self):
            g = GearFilter(0.4)
            self.assertEqual(g.update("3", 0.0), "3")
            self.assertEqual(g.update("N", 0.10), "3")        # neutral during a shift: keep showing 3
            self.assertEqual(g.update("N", 0.20), "3")
            self.assertEqual(g.update("4", 0.25), "4")        # shift finished
            self.assertEqual(g.update("N", 1.00), "4")        # a real neutral starts...
            self.assertEqual(g.update("N", 1.30), "4")
            self.assertEqual(g.update("N", 1.45), "N")        # ...and shows once it has lasted 0.4 s
            self.assertEqual(g.update("N", 2.00), "N")
            self.assertEqual(g.update("1", 2.10), "1")

        def test_gear_filter_edges(self):
            g = GearFilter(0.4)
            self.assertEqual(g.update("N", 0.0), "N")         # nothing shown before: neutral appears at once
            self.assertEqual(g.update("R", 0.1), "R")
            self.assertEqual(g.update("N", 0.2), "R")
            g.reset()
            self.assertEqual(g.update("N", 5.0), "N")         # after a reset there is no previous gear
            off = GearFilter(0)
            off.update("3", 0.0)
            self.assertEqual(off.update("N", 0.01), "N")      # delay 0 = pass through

        def test_should_recycle(self):
            self.assertFalse(should_recycle(5, False))        # not long enough
            self.assertFalse(should_recycle(60, True))        # game still running (sitting in a menu)
            self.assertTrue(should_recycle(11, False))        # game is gone

        def test_vid_pid_parse_without_path(self):
            inf = parse_device_info(bytes(INFO_SIZE))
            self.assertIsNone(inf["vid"])
            self.assertTrue(inf["caps_ok"])

        def _fake_pages(self):
            pages = {k: bytearray(1024) for k in ("physics", "graphics", "static")}
            for n, f in enumerate(FIELDS):
                buf = pages[f.page]
                if f.fmt.startswith("w"):
                    txt = ("x%d" % n)[:f.size // 2 - 1]
                    buf[f.offset:f.offset + f.size] = txt.encode("utf-16-le").ljust(f.size, b"\0")
                elif f.fmt.endswith("i"):
                    struct.pack_into("<i", buf, f.offset, n + 1)
                else:
                    for k in range(f.count):
                        struct.pack_into("<f", buf, f.offset + 4 * k, n + k + 0.5)
            return {k: bytes(v) for k, v in pages.items()}

        def test_schema_is_consistent(self):
            names = [f.name for f in FIELDS]
            self.assertEqual(len(names), len(set(names)))
            for page in ("physics", "graphics", "static"):
                fs = sorted((f for f in FIELDS if f.page == page), key=lambda f: f.offset)
                for a, b in zip(fs, fs[1:]):
                    self.assertLessEqual(a.offset + a.size, b.offset, "%s overlaps %s" % (a.name, b.name))
                for f in fs:
                    self.assertEqual(f.offset % 2, 0)

        def test_decode_roundtrip(self):
            pages = self._fake_pages()
            v = decode_fields(pages)
            for n, f in enumerate(FIELDS):
                if f.fmt.startswith("w"):
                    self.assertEqual(v[f.name], ("x%d" % n)[:f.size // 2 - 1])
                elif f.fmt.endswith("i"):
                    self.assertEqual(v[f.name], n + 1)
                elif f.count == 1:
                    self.assertAlmostEqual(v[f.name], n + 0.5)
                else:
                    self.assertEqual(v[f.name], [n + k + 0.5 for k in range(f.count)])

        def test_decode_short_page_gives_none(self):
            v = decode_fields({"physics": bytes(40)})
            self.assertEqual(v["rpm"], 0)
            self.assertIsNone(v["tyre_pressure"])
            self.assertIsNone(v["car_model"])

        def test_add_derived(self):
            v = {"gear_raw": 4, "rpm": 3500, "max_rpm": 7000}
            self.assertEqual(add_derived(v, 0), 7000)
            self.assertEqual((v["gear"], v["rpm_percent"]), ("3", 50.0))
            v = {"gear_raw": 0, "rpm": 3000, "max_rpm": 0}
            self.assertEqual(add_derived(v, 6000), 6000)                 # falls back to the last good value
            self.assertEqual((v["gear"], v["rpm_percent"]), ("R", 50.0))
            v = {"rpm": 1000}
            add_derived(v, 0)
            self.assertIsNone(v["gear"])
            self.assertIsNone(v["rpm_percent"])

        def test_select_fields(self):
            core = select_fields("core")
            self.assertEqual(core[:3], ["gear", "rpm", "rpm_percent"])
            self.assertIn("tyre_pressure", select_fields("tyres"))
            self.assertEqual(len(select_fields("all")), len(ALL_NAMES))
            self.assertNotIn("fuel", select_fields("core,-fuel"))
            self.assertEqual(select_fields("speed_kmh,rpm,speed_kmh"), ["speed_kmh", "rpm"])
            self.assertNotIn("tyre_wear", select_fields("tyres,-tyre_wear"))
            with self.assertRaises(ValueError) as cm:
                select_fields("core,nonsense,tyrez")
            self.assertIn("nonsense", str(cm.exception))
            self.assertIn("tyrez", str(cm.exception))

        def test_clean_json(self):
            self.assertIsNone(clean_json(float("nan")))
            self.assertIsNone(clean_json(float("inf")))
            self.assertEqual(clean_json([1.234567, float("nan"), 3]), [1.2346, None, 3])
            self.assertEqual(json.dumps(clean_json([float("nan")]), allow_nan=False), "[null]")

        def test_plausibility_warns_once(self):
            chk = PlausibilityChecker(patience=5)
            bad = {"speed_kmh": 99999.0, "tyre_pressure": [26.0, 26.0, 26.0, 26.0]}
            with self.assertLogs("t248", level="WARNING") as cm:
                for _ in range(20):
                    chk.check(bad, True)
            self.assertEqual(len(cm.output), 1)
            self.assertIn("speed_kmh", cm.output[0])
            chk2 = PlausibilityChecker(patience=2)                       # not live: never warns
            with self.assertNoLogs("t248", level="WARNING"):
                for _ in range(10):
                    chk2.check(bad, False)

        def test_core_values_names_exist(self):
            vals = core_values(simulate_frame(10.0))
            for n in vals:
                self.assertIn(n, ALL_NAMES)

        def test_parse_host_port(self):
            self.assertEqual(parse_host_port("8080"), ("127.0.0.1", 8080))
            self.assertEqual(parse_host_port(":9000"), ("127.0.0.1", 9000))
            self.assertEqual(parse_host_port("0.0.0.0:8080"), ("0.0.0.0", 8080))
            self.assertEqual(parse_host_port("192.168.1.5:20777"), ("192.168.1.5", 20777))
            with self.assertRaises(ValueError):
                parse_host_port("abc")

        def test_udp_sink_sends_json(self):
            rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            rx.bind(("127.0.0.1", 0)); rx.settimeout(2.0)
            sink = UdpSink("127.0.0.1", rx.getsockname()[1])
            sink.update({"gear": "3", "tyre_pressure": [26.5, 26.5, 25.0, 25.0]}, True)
            data, _ = rx.recvfrom(65535)
            msg = json.loads(data)
            self.assertTrue(msg["live"])
            self.assertEqual(msg["values"]["gear"], "3")
            self.assertEqual(msg["values"]["tyre_pressure"][2], 25.0)
            sink.close(); rx.close()

        def test_dashboard_serves_page_data_and_fields(self):
            import urllib.request, urllib.error
            dash = Dashboard("127.0.0.1", 0, ["gear", "rpm", "tyre_pressure"])
            try:
                base = "http://127.0.0.1:%d" % dash.port
                html = urllib.request.urlopen(base + "/", timeout=3).read().decode()
                self.assertIn("Telemetry", html)
                d0 = json.loads(urllib.request.urlopen(base + "/data", timeout=3).read())
                self.assertFalse(d0["live"])                                  # nothing published yet
                dash.update({"gear": "2", "rpm": 4100}, True)
                d1 = json.loads(urllib.request.urlopen(base + "/data", timeout=3).read())
                self.assertTrue(d1["live"])
                self.assertEqual(d1["values"], {"gear": "2", "rpm": 4100})
                f = json.loads(urllib.request.urlopen(base + "/fields", timeout=3).read())
                self.assertEqual({c["name"] for c in f}, {"gear", "rpm", "tyre_pressure"})
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(base + "/nope", timeout=3)
                self.assertEqual(cm.exception.code, 404)
            finally:
                dash.close()

        def test_dashboard_port_in_use_raises(self):
            d1 = Dashboard("127.0.0.1", 0, ["gear"])
            try:
                with self.assertRaises(OSError):
                    Dashboard("127.0.0.1", d1.port, ["gear"])
            finally:
                d1.close()

        def test_publisher_filters_and_isolates_failures(self):
            got = []
            class Good:
                def update(self, v, live): got.append((v, live))
                def close(self): pass
            class Bad:
                def update(self, v, live): raise RuntimeError("boom")
                def close(self): raise RuntimeError("boom")
            pub = Publisher(["rpm", "tyre_pressure", "missing"], [Bad(), Good()])
            pub.publish({"rpm": 5000, "gear": "2", "tyre_pressure": [1.0, float("nan"), 3.0, 4.0]}, True)
            pub.publish({"rpm": 5100}, False)
            self.assertEqual(got[0], ({"rpm": 5000, "tyre_pressure": [1.0, None, 3.0, 4.0]}, True))
            self.assertEqual(got[1], ({"rpm": 5100}, False))
            pub.close()                                                     # must not raise

        def test_sources_registry(self):
            self.assertIn("ac", SOURCES)
            self.assertTrue(issubclass(SOURCES["ac"], TelemetrySource))
            with self.assertRaises(NotImplementedError):
                TelemetrySource().read()

        def test_init_block_layout(self):
            b = build_init_block(0x1A2B3C)
            self.assertEqual(len(b), 12)
            self.assertEqual(struct.unpack("<I", b[:4])[0], 12)
            self.assertEqual(struct.unpack("<Q", b[4:])[0], 0x1A2B3C)
            self.assertEqual(struct.unpack("<Q", build_init_block(None)[4:])[0], 0)

        def test_wait_for_devices_succeeds_late(self):
            clock = {"t": 0.0}
            results = iter([(-8, 0), (-8, 0), (-8, 0), (0, 1)])
            pumped = []
            rc, n, seen = wait_for_devices(lambda: next(results), lambda: pumped.append(1), 8.0,
                                           sleep=lambda d: clock.__setitem__("t", clock["t"] + d),
                                           clock=lambda: clock["t"])
            self.assertEqual((rc, n), (0, 1))
            self.assertEqual(seen, [(-8, 0), (0, 1)])
            self.assertEqual(len(pumped), 4)

        def test_wait_for_devices_times_out(self):
            clock = {"t": 0.0}
            rc, n, seen = wait_for_devices(lambda: (-8, 0), lambda: None, 2.0,
                                           sleep=lambda d: clock.__setitem__("t", clock["t"] + d),
                                           clock=lambda: clock["t"])
            self.assertEqual((rc, n), (-8, 0))
            self.assertAlmostEqual(clock["t"], 2.0, places=1)

        def test_failed_connect_closes_sdk(self):
            made = []
            class Half:
                closed = 0
                def connect(self, i=0): raise RuntimeError("no devices")
                def close(self): Half.closed += 1
            s = Sender(lambda: made.append(1) or Half())
            s.send(Telemetry(), now=0.0)
            self.assertEqual(Half.closed, 1)
            self.assertIsNone(s.sdk)

        def test_resolve_hwnd_explicit(self):
            self.assertEqual(resolve_hwnd("game", 0x55), (0x55, "explicit"))

        def test_call_sdk_names_the_crash(self):
            def boom(*a): raise OSError("exception: access violation reading 0x1234")
            with self.assertRaises(RuntimeError) as cm:
                call_sdk("tm_api_init", boom, 0, None)
            self.assertIn("tm_api_init crashed", str(cm.exception))
            self.assertEqual(call_sdk("x", lambda a, b: a + b, 1, 2), 3)

        def test_pe_machine(self):
            import tempfile
            def fake(mach):
                b = bytearray(512); struct.pack_into("<I", b, 0x3C, 0x80)
                b[0x80:0x84] = b"PE\0\0"; struct.pack_into("<H", b, 0x84, mach); return bytes(b)
            with tempfile.TemporaryDirectory() as d:
                for name, mach, want in (("a", 0x8664, "x64"), ("b", 0x14C, "x86"), ("c", 0x1C4, None)):
                    fp = os.path.join(d, name); open(fp, "wb").write(fake(mach))
                    self.assertEqual(pe_machine(fp), want)
                open(os.path.join(d, "junk"), "wb").write(b"not a pe")
                self.assertIsNone(pe_machine(os.path.join(d, "junk")))
                self.assertIsNone(pe_machine(os.path.join(d, "missing")))

        def test_dll_candidates(self):
            c = dll_candidates("C:\\x\\tmsdk")
            self.assertTrue(c[0].lower().endswith("system32\\tmeffcpl.dll") or c[0].lower().endswith("system32/tmeffcpl.dll"))
            self.assertEqual(dll_candidates("x", "my.dll"), ["my.dll"])
            self.assertEqual(len(c), len({x.lower() for x in c}))

        def test_stale_guard(self):
            g = StaleGuard(timeout=3.0)
            self.assertTrue(g.is_live(10, 2, 0.0))
            self.assertTrue(g.is_live(11, 2, 1.0))            # counter moving
            self.assertTrue(g.is_live(11, 2, 3.5))            # frozen 2.5 s: still within timeout
            self.assertFalse(g.is_live(11, 2, 4.5))           # frozen 3.5 s: game gone
            self.assertTrue(g.is_live(12, 2, 5.0))            # new game instance starts counting again
            self.assertTrue(g.is_live(12, 3, 60.0))           # paused: frozen counter is fine
            self.assertFalse(g.is_live(12, 0, 61.0))          # menu: not live

        def test_simulation_is_sane(self):
            for sec in range(0, 120):
                t = simulate_frame(float(sec))
                self.assertIn(t.gear, list("12345"))
                self.assertTrue(0 <= t.rpm_percent <= 100)
                self.assertTrue(0 <= t.speed_kmh <= 181)

    TestCase = T
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(T)
    res = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if res.wasSuccessful() else 1


# --------------------------------------------------------------------------
def print_field_list():
    print("Selectable telemetry (use with --fields, e.g.  --fields core,tyres,-tyre_wear )\n")
    print("Groups: all, " + ", ".join(sorted({c["group"] for c in field_catalog()})) + "")
    print("  (core is a shortcut for: " + ", ".join(GROUP_EXTRA["core"]) + ")\n")
    cur = None
    for c in sorted(field_catalog(), key=lambda c: (c["group"], c["name"])):
        if c["group"] != cur:
            cur = c["group"]
            print("[%s]" % cur)
        arr = " x%d" % c["count"] if c["count"] > 1 else ""
        print("  %-24s%-4s %s" % (c["name"], arr, c["desc"]))
    print("\nArrays are ordered front-left, front-right, rear-left, rear-right.")
    print("Default selection: core,lap")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Show Assetto Corsa telemetry on the T248 screen")
    ap.add_argument("--selftest", action="store_true", help="run the built-in logic tests and exit")
    ap.add_argument("--probe", action="store_true", help="open the wheel via the SDK, send a test frame, exit")
    ap.add_argument("--simulate", action="store_true", help="send fake telemetry (no game needed)")
    ap.add_argument("--dry-run", action="store_true", help="read AC and print values; do not touch the wheel")
    ap.add_argument("--game", choices=sorted(("ac",)), default="ac", help="which game to read (currently only ac)")
    ap.add_argument("--neutral-delay", type=float, default=0.4,
                    help="seconds neutral must last before it is shown, so shifts do not flash N (default 0.4, 0 = off)")
    ap.add_argument("--race-info", action=argparse.BooleanOptionalAction, default=True, help="send position / cars / lap number / lap count (default on; --no-race-info disables)")
    ap.add_argument("--power-follow", action=argparse.BooleanOptionalAction, default=True,
                    help="put the wheel screen into standby when the computer sleeps, shuts down or turns its display off (default on)")
    ap.add_argument("--idle-clock", action=argparse.BooleanOptionalAction, default=False,
                    help="show the time of day in the lap-time field while no game is running (default off: it only "
                         "appears when the wheel is on its lap page, which the bridge cannot select)")
    ap.add_argument("--clock-test", action="store_true", help="show the time of day on the screen for 20 s, then exit")
    ap.add_argument("--power-test", action="store_true",
                    help="step the screen power state 0, 1, 2, 1, 0 so you can see what each value does, then exit")
    ap.add_argument("--hold", type=float, default=8.0, help="seconds --probe keeps the test frame on screen (default 8)")
    ap.add_argument("--rate", type=float, default=30.0, help="updates per second (default 30)")
    ap.add_argument("--sdk-dir", help="folder containing tm_api_lib_x64.dll (the 'tmsdk' folder)")
    ap.add_argument("--dll", help="path to the 64-bit DLL that exports tm_api_* (default: searched automatically)")
    ap.add_argument("--device", default="auto", help="SDK device index, or 'auto' (default): a device with a screen first")
    ap.add_argument("--no-enumerate", action="store_true", help="do not ask the SDK about each device before opening it (for troubleshooting)")
    ap.add_argument("--list-devices", action="store_true", help="show the Thrustmaster devices the SDK sees, and what each can do, then exit")
    ap.add_argument("--hwnd", help="window handle to give the SDK (e.g. 0x1A2B3C); default: the game window, or this console")
    ap.add_argument("--connect-timeout", type=float, default=8.0, help="seconds to wait for the SDK to find the wheel")
    ap.add_argument("--list-fields", action="store_true", help="list every telemetry value you can select, then exit")
    ap.add_argument("--fields", default="core,lap",
                    help="what to send to the outputs below: names, groups or 'all'; '-name' removes one. "
                         "Default 'core,lap'. Example: core,tyres,brakes,-tyre_wear")
    ap.add_argument("--serve", metavar="[HOST:]PORT",
                    help="serve a live web dashboard, e.g. 8080 (this PC only) or 0.0.0.0:8080 (other devices on your network)")
    ap.add_argument("--udp", metavar="HOST:PORT", help="send each update as a JSON datagram to HOST:PORT")
    ap.add_argument("--dump", action="store_true", help="print the selected values to the console about once a second")
    ap.add_argument("--no-wheel", action="store_true", help="do not use the wheel at all (outputs above only)")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    handlers = [logging.StreamHandler(sys.stdout),
                RotatingFileHandler(os.path.join(here, "t248_bridge.log"), maxBytes=512_000, backupCount=2, encoding="utf-8")]
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)

    if a.selftest:
        return selftest()
    if a.list_fields:
        return print_field_list()
    try:
        names = select_fields(a.fields)
    except ValueError as e:
        log.error("%s. Run with --list-fields to see the choices.", e)
        return 2
    wheel = not (a.no_wheel or a.dry_run)
    if wheel and os.name == "nt" and struct.calcsize("P") != 8:
        log.error("This needs 64-bit Python (the Thrustmaster SDK DLL is 64-bit). Use --no-wheel to skip the wheel.")
        return 2

    try:
        device_choice = None if str(a.device).lower() == "auto" else int(a.device)
    except ValueError:
        log.error("--device must be 'auto' or a number")
        return 2
    explicit = int(a.hwnd, 0) if a.hwnd else None
    mode = "game" if not (a.probe or a.simulate) else "console"

    def factory():
        sdk = TmSdk(a.sdk_dir, a.dll)
        orig = sdk.connect

        def connect(idx=0):
            hwnd, what = resolve_hwnd(mode, explicit)
            log.info("Using %s as the SDK window: 0x%x", what, hwnd or 0)
            SDK_WINDOW["kind"] = what
            orig(device_choice, hwnd, a.connect_timeout, not a.no_enumerate)
        sdk.connect = connect
        return sdk

    if a.list_devices:
        return run_list_devices(factory)

    sinks = []
    try:
        if a.serve:
            host, port = parse_host_port(a.serve)
            dash = Dashboard(host, port, names)
            sinks.append(dash)
            where = "this PC only" if host in ("127.0.0.1", "localhost") else "reachable from your network (no password!)"
            log.info("Dashboard: http://%s:%d/  (%s)", "localhost" if host in ("0.0.0.0", "") else host, dash.port, where)
        if a.udp:
            host, port = parse_host_port(a.udp)
            sinks.append(UdpSink(host, port))
            log.info("Sending JSON to udp://%s:%d", host, port)
    except (OSError, ValueError) as e:
        log.error("could not start an output: %s", e)
        for sk in sinks:
            sk.close()
        return 2
    if a.dump:
        sinks.append(ConsoleSink())
    publisher = Publisher(names, sinks) if sinks else None
    if publisher:
        log.info("Selected %d value(s): %s", len(names), ", ".join(names[:12]) + (" ..." if len(names) > 12 else ""))

    use_wheel = not (a.no_wheel or a.dry_run)
    if not use_wheel and publisher is None and not a.dry_run:
        log.error("--no-wheel with no output chosen: add --serve, --udp or --dump")
        return 2
    sender = Sender(factory, race_info=a.race_info) if use_wheel else None
    if sender is not None:
        install_cleanup(sender.close, on_shutdown=sender.sleep)
    power = PowerWatcher() if (sender is not None and a.power_follow and not a.probe and not a.power_test) else None
    if a.clock_test:
        return run_clock_test(sender) if sender else 2
    if a.power_test:
        return run_power_test(sender) if sender else 2
    if a.probe:
        return run_probe(sender, a.hold) if sender else 2
    if a.simulate:
        run_simulate(sender, a.rate, publisher, power)
        return 0
    run_follow(sender, a.rate, a.dry_run, SOURCES[a.game](neutral_delay=a.neutral_delay), publisher, power, a.idle_clock)
    return 0


if __name__ == "__main__":
    sys.exit(main())
