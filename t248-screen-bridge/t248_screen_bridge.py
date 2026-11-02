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
  python t248_screen_bridge.py --probe         # init the SDK, open the wheel, send one frame
  python t248_screen_bridge.py --simulate      # sweep fake values on the wheel screen
  python t248_screen_bridge.py                 # normal use: follow Assetto Corsa
  python t248_screen_bridge.py --dry-run       # read AC, print what would be sent, touch no hardware

Everything it learns is written to t248_bridge.log next to this script.

STATUS: written from reverse engineering of the SDK and of USB captures. The
record layout, parameter IDs and call order come from Thrustmaster's own test
app, but this program has not been run against a real wheel by its author.
Run --probe first and send back t248_bridge.log if anything fails.
"""

import argparse
import ctypes
import glob
import logging
import os
import signal
import struct
import sys
import time
from dataclasses import dataclass
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


class AcReader:
    NAMES = {"physics": PHYSICS_SIZE, "graphics": GRAPHICS_SIZE, "static": STATIC_SIZE}

    def __init__(self):
        self.views = {}
        self.max_rpm = 0
        self.guard = StaleGuard()
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
        for page, size in self.NAMES.items():
            opened = None
            for prefix in ("Local\\", ""):
                h = self._k32.OpenFileMappingW(4, 0, prefix + "acpmf_" + page)   # FILE_MAP_READ
                if h:
                    p = self._k32.MapViewOfFile(h, 4, 0, 0, size)
                    if p:
                        opened = (h, p, size)
                        break
                    self._k32.CloseHandle(h)
            if not opened:
                self.close()
                return False
            self.views[page] = opened
        return True

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
        t, self.max_rpm = parse_ac_pages(pages["physics"], pages["graphics"], pages["static"], self.max_rpm)
        status, = struct.unpack_from("<i", pages["graphics"], 4)
        packet_id, = struct.unpack_from("<i", pages["physics"], 0)
        if not self.guard.is_live(packet_id, status, time.monotonic()):
            t.live = False
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


REQUIRED_EXPORTS = ("tm_api_init", "tm_api_exit", "tm_api_get_device_count", "tm_api_open_device",
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
        d.tm_api_close_device.argtypes = [ctypes.c_int];                           d.tm_api_close_device.restype = ctypes.c_int
        d.tm_api_set_properties.argtypes = [ctypes.c_int, ctypes.c_uint32, ctypes.c_void_p]
        d.tm_api_set_properties.restype = ctypes.c_int
        d.tm_api_update.argtypes = [ctypes.c_int, ctypes.c_void_p];                d.tm_api_update.restype = ctypes.c_int
        self.handle = None
        self._rec = ctypes.create_string_buffer(RECORD_SIZE)
        self._upd = ctypes.create_string_buffer(0x2000)       # the app clears 0x140c bytes; be generous

    def connect(self, device_index=0, hwnd=0, timeout=8.0):
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
        h = ctypes.c_int(0)
        rc = call_sdk("tm_api_open_device", d.tm_api_open_device, device_index, ctypes.byref(h))
        log.info("tm_api_open_device(%s) -> rc=%s handle=0x%x (foreground window now 0x%x)",
                 device_index, rc, h.value, foreground_window())
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

    def connect(self, device_index=0, hwnd=0, timeout=0):
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
        now = time.monotonic() if now is None else now
        try:
            self._ensure(now)
            ok = True
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


def idle_frame():
    return Telemetry(live=False, gear="N")


def run_follow(sender, rate, dry_run):
    reader = AcReader()
    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *a: stop.update(flag=True))
    period, connected, last_state, dead_since = 1.0 / rate, False, None, None
    log.info("Following Assetto Corsa at %.0f Hz. Press Ctrl+C to stop.", rate)
    while not stop["flag"]:
        t0 = time.monotonic()
        if not connected:
            connected = reader.open()
            if connected:
                log.info("Assetto Corsa shared memory found")
            else:
                time.sleep(1.0)
                continue
        try:
            t = reader.read()
        except Exception as e:
            log.warning("lost Assetto Corsa: %s", e)
            reader.close(); connected = False
            sender.send(idle_frame())
            continue
        if t.live:
            dead_since = None
        else:
            dead_since = dead_since or time.monotonic()
            if time.monotonic() - dead_since > 10.0:
                if sender.sdk is not None:
                    sender.send(idle_frame())                          # leave the wheel screen tidy
                    sender.close()                                     # re-init with the next game window
                reader.close(); connected = False; dead_since = None   # re-open: AC may have restarted
        frame = t if t.live else idle_frame()
        state = (frame.live, frame.gear)
        if state != last_state:
            log.info("state: live=%s gear=%s", frame.live, frame.gear)
            last_state = state
        if dry_run:
            print("gear=%s rpm=%d (%.1f%%) speed=%.1f cur=%d last=%d best=%d" % (
                frame.gear, frame.rpm, frame.rpm_percent, frame.speed_kmh, frame.current_ms, frame.last_ms, frame.best_ms))
        else:
            sender.send(frame)
        time.sleep(max(0.0, period - (time.monotonic() - t0)))
    if sender.sdk is not None:                 # only reset the screen if we ever connected
        sender.send(idle_frame())
    sender.close()
    reader.close()


def run_simulate(sender, rate):
    signal.signal(signal.SIGINT, signal.default_int_handler)
    t0 = time.monotonic()
    log.info("Simulating telemetry. Watch the wheel screen. Ctrl+C to stop.")
    try:
        while True:
            sender.send(simulate_frame(time.monotonic() - t0))
            time.sleep(1.0 / rate)
    except KeyboardInterrupt:
        pass
    if sender.sdk is not None:                 # only reset the screen if we ever connected
        sender.send(idle_frame())
    sender.close()


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
            self.assertEqual(ids, {ID_GEAR, ID_RPM, ID_RPM_PERCENT, ID_SPEED, ID_CURRENT_LAP})
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

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(T)
    res = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if res.wasSuccessful() else 1


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Show Assetto Corsa telemetry on the T248 screen")
    ap.add_argument("--selftest", action="store_true", help="run the built-in logic tests and exit")
    ap.add_argument("--probe", action="store_true", help="open the wheel via the SDK, send a test frame, exit")
    ap.add_argument("--simulate", action="store_true", help="send fake telemetry (no game needed)")
    ap.add_argument("--dry-run", action="store_true", help="read AC and print values; do not touch the wheel")
    ap.add_argument("--race-info", action="store_true", help="also send position / lap count (field meaning not fully verified)")
    ap.add_argument("--hold", type=float, default=8.0, help="seconds --probe keeps the test frame on screen (default 8)")
    ap.add_argument("--rate", type=float, default=30.0, help="updates per second (default 30)")
    ap.add_argument("--sdk-dir", help="folder containing tm_api_lib_x64.dll (the 'tmsdk' folder)")
    ap.add_argument("--dll", help="path to the 64-bit DLL that exports tm_api_* (default: searched automatically)")
    ap.add_argument("--device", type=int, default=0, help="SDK device index (default 0)")
    ap.add_argument("--hwnd", help="window handle to give the SDK (e.g. 0x1A2B3C); default: the game window, or this console")
    ap.add_argument("--connect-timeout", type=float, default=8.0, help="seconds to wait for the SDK to find the wheel")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    handlers = [logging.StreamHandler(sys.stdout),
                RotatingFileHandler(os.path.join(here, "t248_bridge.log"), maxBytes=512_000, backupCount=2, encoding="utf-8")]
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)

    if a.selftest:
        return selftest()
    if os.name == "nt" and struct.calcsize("P") != 8:
        log.error("This needs 64-bit Python (the Thrustmaster SDK DLL is 64-bit).")
        return 2

    explicit = int(a.hwnd, 0) if a.hwnd else None
    mode = "game" if not (a.probe or a.simulate) else "console"

    def factory():
        sdk = TmSdk(a.sdk_dir, a.dll)
        orig = sdk.connect

        def connect(idx=0):
            hwnd, what = resolve_hwnd(mode, explicit)
            log.info("Using %s as the SDK window: 0x%x", what, hwnd or 0)
            orig(a.device, hwnd, a.connect_timeout)
        sdk.connect = connect
        return sdk

    sender = Sender(factory, race_info=a.race_info)
    if a.probe:
        return run_probe(sender, a.hold)
    if a.simulate:
        run_simulate(sender, a.rate)
        return 0
    run_follow(sender, a.rate, a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
