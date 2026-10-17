"""Windows backend: the Interception kernel driver (https://github.com/oblitum/Interception).

Only the selected keyboard device is filtered, so every other keyboard is untouched even if
this program freezes. Strokes from the clicker are consumed (never forwarded), exactly like
the original C++ version of this tool.

Requirements:
  * The driver must be installed once:  install-interception.exe /install  (as Administrator)
    from the Interception download, followed by a reboot.
  * interception.dll must be reachable: vendor/interception/<x64|x86>/ in this project, the
    folder of the running program, or anywhere on PATH.
"""
from __future__ import annotations

import ctypes
import os
import re
import sys
import threading
from ctypes import CFUNCTYPE, Structure, c_int, c_uint, c_ulong, c_ushort, c_void_p, create_string_buffer
from pathlib import Path

from .base import Backend, CaptureError, DeviceInfo

KEY_UP, KEY_E0, KEY_E1 = 0x01, 0x02, 0x04
FILTER_KEY_NONE, FILTER_KEY_ALL = 0x0000, 0xFFFF
MAX_KEYBOARD = 10
STROKE_SIZE = 20  # sizeof(InterceptionMouseStroke): the generic stroke buffer size
PREDICATE = CFUNCTYPE(c_int, c_int)

DRIVER_MISSING = ("The Interception driver is not installed (or Windows has not been rebooted "
                  "since installing it).\n\nInstall it once: run  install-interception.exe /install  "
                  "as Administrator from the Interception download, then reboot.")


class KeyStroke(Structure):
    _fields_ = [("code", c_ushort), ("state", c_ushort), ("information", c_uint)]


def _dll_candidates():
    arch = "x64" if ctypes.sizeof(c_void_p) == 8 else "x86"
    here = Path(__file__).resolve()
    roots = [here.parents[2], Path(sys.executable).resolve().parent, Path(os.getcwd())]
    if getattr(sys, "frozen", False):  # PyInstaller bundle
        roots.insert(0, Path(getattr(sys, "_MEIPASS", roots[1])))
    for root in roots:
        yield root / "vendor" / "interception" / arch / "interception.dll"
        yield root / "interception.dll"
    yield Path("interception.dll")  # PATH lookup


class _Lib:
    """ctypes binding of the handful of interception.dll functions we need."""

    def __init__(self):
        last_error = None
        self.dll = None
        self.path = None
        for candidate in _dll_candidates():
            try:
                self.dll = ctypes.CDLL(str(candidate))
                self.path = candidate
                break
            except OSError as exc:
                last_error = exc
        if self.dll is None:
            raise OSError(f"interception.dll not found ({last_error})")
        d = self.dll
        d.interception_create_context.restype = c_void_p
        d.interception_create_context.argtypes = []
        d.interception_destroy_context.restype = None
        d.interception_destroy_context.argtypes = [c_void_p]
        d.interception_set_filter.restype = None
        d.interception_set_filter.argtypes = [c_void_p, PREDICATE, c_ushort]
        d.interception_wait_with_timeout.restype = c_int
        d.interception_wait_with_timeout.argtypes = [c_void_p, c_ulong]
        d.interception_receive.restype = c_int
        d.interception_receive.argtypes = [c_void_p, c_int, c_void_p, c_uint]
        d.interception_send.restype = c_int
        d.interception_send.argtypes = [c_void_p, c_int, c_void_p, c_uint]
        d.interception_get_hardware_id.restype = c_uint
        d.interception_get_hardware_id.argtypes = [c_void_p, c_int, c_void_p, c_uint]
        d.interception_is_keyboard.restype = c_int
        d.interception_is_keyboard.argtypes = [c_int]


class InterceptionBackend(Backend):
    name = "Interception driver"

    def __init__(self):
        self._lib = None
        self.available = False
        self.unavailable_reason = ""
        try:
            self._lib = _Lib()
            self.available = True
        except OSError as exc:
            self.unavailable_reason = f"interception.dll could not be loaded: {exc}"
        self._ctx = None
        self._thread = None
        self._targets: set[int] = set()
        self._predicate = None
        self._stop = threading.Event()
        self._on_event = None

    def permission_hint(self) -> str:
        return DRIVER_MISSING

    # ---- enumeration ------------------------------------------------------
    def _context(self):
        ctx = self._lib.dll.interception_create_context()
        if not ctx:
            raise CaptureError(DRIVER_MISSING)
        return ctx

    def _hardware_ids(self, ctx) -> dict[int, str]:
        ids = {}
        buf = create_string_buffer(2048)
        for dev in range(1, MAX_KEYBOARD + 1):
            n = self._lib.dll.interception_get_hardware_id(ctx, dev, buf, len(buf))
            if n and n > 0:
                text = buf.raw[:n].decode("utf-16-le", errors="ignore").split("\x00")[0].strip()
                if text:
                    ids[dev] = text
        return ids

    @staticmethod
    def _hid_names() -> dict[tuple[int, int], tuple[str, str]]:
        """Friendly names via hidapi (optional): Windows lets us *list* keyboards, just not read them."""
        try:
            import hid
        except ImportError:
            return {}
        names: dict[tuple[int, int], tuple[str, str]] = {}
        try:
            for d in hid.enumerate():
                key = (int(d.get("vendor_id") or 0), int(d.get("product_id") or 0))
                vendor = (d.get("manufacturer_string") or "").strip()
                product = (d.get("product_string") or "").strip()
                name = " ".join(x for x in (vendor, product) if x)
                serial = (d.get("serial_number") or "").strip()
                if name and (key not in names or not names[key][0]):
                    names[key] = (name, serial)
        except Exception:
            pass
        return names

    def list_devices(self) -> list[DeviceInfo]:
        if not self.available:
            raise CaptureError(self.unavailable_reason)
        ctx = self._context()
        try:
            ids = self._hardware_ids(ctx)
        finally:
            self._lib.dll.interception_destroy_context(ctx)
        names = self._hid_names()
        result, seen = [], set()
        for dev, hwid in sorted(ids.items()):
            if hwid in seen:
                continue
            seen.add(hwid)
            m = re.search(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})", hwid)
            name, serial, vidpid = "", "", ""
            if m:
                vid, pid = int(m.group(1), 16), int(m.group(2), 16)
                vidpid = f"{vid:04X}:{pid:04X}"
                name, serial = names.get((vid, pid), ("", ""))
            upper = hwid.upper()
            builtin = upper.startswith("ACPI\\") or "PS2" in upper or "I8042" in upper
            display = name or ("Built-in keyboard" if builtin else hwid)
            parts = [p for p in (vidpid, f"SN {serial}" if serial else "", f"slot {dev}", hwid) if p]
            result.append(DeviceInfo(id=hwid, name=display, details=" · ".join(parts), builtin=builtin))
        return result

    # ---- capture ----------------------------------------------------------
    def start(self, device_id: str, on_event) -> None:
        if not self.available:
            raise CaptureError(self.unavailable_reason)
        if self.is_running():
            self.stop()
        ctx = self._context()
        ids = self._hardware_ids(ctx)
        targets = {dev for dev, hwid in ids.items() if hwid == device_id}
        if not targets:
            self._lib.dll.interception_destroy_context(ctx)
            raise CaptureError("The clicker is not connected. Plug it in and try again.")
        self._ctx, self._targets = ctx, targets
        self._on_event = on_event
        self._stop.clear()
        self._predicate = PREDICATE(lambda dev: 1 if dev in targets else 0)
        self._lib.dll.interception_set_filter(ctx, self._predicate, FILTER_KEY_ALL)
        self._thread = threading.Thread(target=self._loop, name="interception", daemon=True)
        self._thread.start()
        on_event("status", f"Capturing keyboard slot(s) {sorted(targets)} via the Interception driver.")

    def _loop(self) -> None:
        dll = self._lib.dll
        buf = create_string_buffer(STROKE_SIZE)
        while not self._stop.is_set():
            dev = dll.interception_wait_with_timeout(self._ctx, 250)
            if dev <= 0:
                continue
            n = dll.interception_receive(self._ctx, dev, buf, 1)
            if n <= 0:
                continue
            if dev not in self._targets:
                dll.interception_send(self._ctx, dev, buf, 1)  # not ours: pass it on untouched
                continue
            stroke = KeyStroke.from_buffer_copy(buf.raw[:ctypes.sizeof(KeyStroke)])
            sig = {"type": "scancode", "code": int(stroke.code),
                   "e0": bool(stroke.state & KEY_E0), "e1": bool(stroke.state & KEY_E1)}
            self._on_event("release" if stroke.state & KEY_UP else "press", sig)
            # Deliberately not forwarded: the clicker's keys never reach Windows.

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
        if self._ctx is not None:
            dll = self._lib.dll
            try:
                if self._predicate is not None:
                    dll.interception_set_filter(self._ctx, self._predicate, FILTER_KEY_NONE)
            finally:
                dll.interception_destroy_context(self._ctx)
        self._ctx, self._thread, self._targets, self._predicate = None, None, set(), None

    def is_running(self) -> bool:
        return self._ctx is not None
