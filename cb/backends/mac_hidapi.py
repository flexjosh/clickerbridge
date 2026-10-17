"""macOS backend: exclusive ("seized") open of the clicker's HID interfaces via hidapi.

hidapi's Darwin backend opens devices with kIOHIDOptionsTypeSeizeDevice by default, which
detaches the device from macOS's own keyboard handling: its buttons no longer reach any
application until we close it again. Nothing else on the system is touched.

macOS 10.15+ requires the *Input Monitoring* permission for the process that opens a
keyboard-class device: Terminal/iTerm when run from a shell, Python.app when double
clicked, or the .app bundle when packaged with PyInstaller/py2app.
"""
from __future__ import annotations

import threading

from .base import Backend, CaptureError, DeviceInfo

try:
    import hid  # package name on PyPI: hidapi
except ImportError:  # pragma: no cover
    hid = None

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

    def permission_hint(self) -> str:
        return ("macOS needs the Input Monitoring permission for the program running ClickerBridge "
                "(System Settings → Privacy & Security → Input Monitoring). From a shell that is "
                "Terminal or iTerm; packaged, it is ClickerBridge.app. Quit and relaunch after "
                "granting it.")

    # ---- enumeration ------------------------------------------------------
    def _interfaces(self):
        try:
            entries = hid.enumerate()
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
            raise CaptureError("Could not open the clicker exclusively:\n" + "\n".join(errors)
                               + "\n\n" + self.permission_hint())
        self._handles = opened
        for handle in opened:
            t = threading.Thread(target=self._reader, args=(handle,), name="hid-reader", daemon=True)
            t.start()
            self._threads.append(t)
        on_event("status", f"Capturing {len(opened)} HID interface(s) of the clicker (exclusive).")
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
            if _is_idle(report):
                if pressed is not None:
                    self._on_event("release", {"type": "report", "data": pressed})
                    pressed = None
            else:
                if pressed is not None and pressed != report:
                    self._on_event("release", {"type": "report", "data": pressed})
                pressed = report
                self._on_event("press", {"type": "report", "data": report})

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
