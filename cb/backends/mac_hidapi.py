"""macOS backend: read the clicker's HID reports via hidapi, block its keys with an event tap.

macOS only lets root seize (open exclusively) a keyboard-class device, so the clicker is
opened shared: reading it needs the *Input Monitoring* permission. Its keystrokes would then
still reach the front app, so when block_keys is on a CGEventTap (needs the *Accessibility*
permission) drops every key event that arrives while the clicker is sending reports. An event
tap cannot tell which keyboard a key came from, so the clicker's own HID reports decide.
"""
from __future__ import annotations

import ctypes
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .base import Backend, CaptureError, DeviceInfo

try:
    import hid  # package name on PyPI: hidapi
except ImportError:  # pragma: no cover
    hid = None
if hid is not None:
    ctypes.CDLL(hid.__file__).hid_darwin_set_open_exclusive(0)  # seizing a keyboard needs root

_cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
_cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_TapCallback = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p)
_cg.CGEventTapCreate.restype = ctypes.c_void_p
_cg.CGEventTapCreate.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint64,
                                 _TapCallback, ctypes.c_void_p]
_cg.CGEventTapEnable.argtypes = [ctypes.c_void_p, ctypes.c_bool]
_cf.CFMachPortCreateRunLoopSource.restype = ctypes.c_void_p
_cf.CFMachPortCreateRunLoopSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
_cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
_cf.CFRunLoopAddSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
_cf.CFRunLoopRunInMode.argtypes = [ctypes.c_void_p, ctypes.c_double, ctypes.c_bool]
_cf.CFMachPortInvalidate.argtypes = [ctypes.c_void_p]
_cf.CFRelease.argtypes = [ctypes.c_void_p]
_DEFAULT_MODE = ctypes.c_void_p.in_dll(_cf, "kCFRunLoopDefaultMode")
_KEY_EVENTS = (1 << 10) | (1 << 11) | (1 << 14)  # key down, key up, system-defined (media keys)

KEYBOARD_USAGES = {(0x01, 0x06): "keyboard", (0x01, 0x07): "keypad", (0x0C, 0x01): "consumer control"}
POINTER_USAGES = {(0x01, 0x01): "pointer", (0x01, 0x02): "mouse"}
APPLE_VENDOR_ID = 0x05AC


def _usage(d) -> tuple[int, int]:
    return int(d.get("usage_page") or 0), int(d.get("usage") or 0)


def _group_id(d) -> str:
    serial = (d.get("serial_number") or "").strip()
    return f"{int(d['vendor_id']):04x}:{int(d['product_id']):04x}:{serial}"


def _is_idle(report: list[int]) -> bool:
    """True for "nothing pressed" reports: all zero, or zero apart from a leading report-ID byte."""
    if len(report) > 1:
        return not any(report[1:])
    return not report or report[0] == 0


class HidapiBackend(Backend):
    name = "hidapi (exclusive IOKit open)"

    def __init__(self):
        self.available = hid is not None
        self.unavailable_reason = "" if hid else (
            "The Python package 'hidapi' is not installed. Run:  pip install hidapi")
        self._handles = []
        self._threads = []
        self._stop = threading.Event()
        self._on_event = None
        self.block_keys = True
        self._tap = None
        self._tap_callback = _TapCallback(self._on_key)
        self._clicker_down = False
        self._last_report = 0.0
        # hid.enumerate() spins the calling thread's run loop; on the Tk thread that re-enters Tk
        # and aborts Python. So it always runs on this one thread (hidapi binds to its first caller).
        self._hid_thread = ThreadPoolExecutor(max_workers=1)

    def permission_hint(self) -> str:
        return ("macOS needs the Input Monitoring permission for the program running ClickerBridge "
                "(System Settings → Privacy & Security → Input Monitoring). From a shell that is "
                "Terminal or iTerm; packaged, it is ClickerBridge.app. Quit and relaunch after "
                "granting it.")

    # ---- enumeration ------------------------------------------------------
    def _interfaces(self):
        try:
            entries = self._hid_thread.submit(hid.enumerate).result()
        except Exception as exc:  # hidapi raises plain Exception/IOError
            raise CaptureError(f"hidapi could not enumerate devices: {exc}") from exc
        return [d for d in entries if _usage(d) in KEYBOARD_USAGES or _usage(d) in POINTER_USAGES]

    def list_devices(self) -> list[DeviceInfo]:
        if not self.available:
            raise CaptureError(self.unavailable_reason)
        groups: dict[str, dict] = {}
        for d in self._interfaces():
            g = groups.setdefault(_group_id(d), {"info": d, "kinds": set()})
            g["kinds"].add(KEYBOARD_USAGES.get(_usage(d)) or POINTER_USAGES.get(_usage(d)) or "other")
            if not (g["info"].get("product_string") or "").strip():
                g["info"] = d
        result = []
        for gid, g in groups.items():
            if not any(k in g["kinds"] for k in KEYBOARD_USAGES.values()):
                continue  # pointer-only devices cannot be clickers
            d = g["info"]
            vendor = (d.get("manufacturer_string") or "").strip()
            product = (d.get("product_string") or "").strip()
            name = " ".join(x for x in (vendor, product) if x) or "Unnamed HID device"
            serial = (d.get("serial_number") or "").strip()
            details = f"{int(d['vendor_id']):04X}:{int(d['product_id']):04X}"
            details += f" · SN {serial}" if serial else " · no serial"
            details += " · " + ", ".join(sorted(g["kinds"]))
            builtin = int(d["vendor_id"]) == APPLE_VENDOR_ID or "Apple Internal" in name
            result.append(DeviceInfo(id=gid, name=name, details=details, builtin=builtin))
        result.sort(key=lambda x: (x.builtin, x.name.lower()))
        return result

    # ---- capture ----------------------------------------------------------
    def start(self, device_id: str, on_event) -> None:
        if not self.available:
            raise CaptureError(self.unavailable_reason)
        if self.is_running():
            self.stop()
        targets = [d for d in self._interfaces()
                   if _group_id(d) == device_id and _usage(d) in KEYBOARD_USAGES]
        if not targets:
            raise CaptureError("The clicker is not connected. Plug it in (or pair it) and try again.")
        self._stop.clear()
        self._on_event = on_event
        opened, errors = [], []
        for d in targets:
            handle = hid.device()
            try:
                handle.open_path(d["path"])
                handle.set_nonblocking(0)
            except (OSError, IOError, ValueError) as exc:
                errors.append(f"{KEYBOARD_USAGES.get(_usage(d), 'interface')}: {exc}")
                continue
            opened.append(handle)
        if not opened:
            raise CaptureError("Could not open the clicker:\n" + "\n".join(errors)
                               + "\n\n" + self.permission_hint())
        self._handles = opened
        for handle in opened:
            t = threading.Thread(target=self._reader, args=(handle,), name="hid-reader", daemon=True)
            t.start()
            self._threads.append(t)
        if self.block_keys:
            # session tap, head insert, active filter
            self._tap = _cg.CGEventTapCreate(1, 0, 0, _KEY_EVENTS, self._tap_callback, None)
            if not self._tap:
                self.stop()
                raise CaptureError("To block the clicker's keys in other apps, macOS needs the Accessibility "
                                   "permission for ClickerBridge (System Settings → Privacy & Security → "
                                   "Accessibility). Grant it and turn the bridge on again, or untick "
                                   "\"Block the clicker's keys\".")
            t = threading.Thread(target=self._run_tap, name="key-tap", daemon=True)
            t.start()
            self._threads.append(t)
        mode = "its keys are blocked in other apps" if self.block_keys else "shared: other apps get its keys too"
        on_event("status", f"Capturing {len(opened)} HID interface(s) of the clicker ({mode}).")
        for err in errors:
            on_event("status", "Skipped an interface: " + err)

    def _reader(self, handle) -> None:
        last = None
        pressed = None  # the last non-idle report, until the idle report arrives
        while not self._stop.is_set():
            try:
                data = handle.read(64, 250)  # (max bytes, timeout ms)
            except (OSError, IOError, ValueError) as exc:
                if not self._stop.is_set():
                    self._on_event("error", f"Lost the clicker ({exc}). Unplugged?")
                return
            if not data:
                continue
            report = [int(b) for b in data]
            if report == last:
                continue  # some devices resend the current state periodically
            last = report
            self._last_report = time.monotonic()
            self._clicker_down = not _is_idle(report)
            if _is_idle(report):
                if pressed is not None:
                    self._on_event("release", {"type": "report", "data": pressed})
                    pressed = None
            else:
                if pressed is not None and pressed != report:
                    self._on_event("release", {"type": "report", "data": pressed})
                pressed = report
                self._on_event("press", {"type": "report", "data": report})

    def _run_tap(self) -> None:
        source = _cf.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        _cf.CFRunLoopAddSource(_cf.CFRunLoopGetCurrent(), source, _DEFAULT_MODE)
        while not self._stop.is_set():
            _cf.CFRunLoopRunInMode(_DEFAULT_MODE, 0.25, False)
        _cf.CFMachPortInvalidate(self._tap)
        _cf.CFRelease(source)
        _cf.CFRelease(self._tap)
        self._tap = None

    def _on_key(self, _proxy, event_type, event, _refcon):
        if event_type >= 0xFFFFFFFE:  # macOS disabled the tap (timeout / user input): re-enable it
            _cg.CGEventTapEnable(self._tap, True)
            return event
        # The key event can arrive a moment before the clicker's HID report, so wait briefly.
        deadline = time.monotonic() + 0.05
        while True:
            if self._clicker_down or time.monotonic() - self._last_report < 0.2:
                return None  # the clicker's key: swallow it
            if time.monotonic() > deadline:
                return event
            time.sleep(0.002)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1.0)
        for handle in self._handles:
            try:
                handle.close()
            except Exception:
                pass
        self._threads, self._handles = [], []

    def is_running(self) -> bool:
        return bool(self._handles)
