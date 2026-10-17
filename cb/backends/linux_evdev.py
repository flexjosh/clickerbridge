"""Linux backend: grab the clicker's evdev input node(s) exclusively (EVIOCGRAB).

Needs read access to /dev/input/event*: add your user to the "input" group
(sudo usermod -aG input $USER, then log out and in) or run with sudo.
"""
from __future__ import annotations

import select
import threading

from .base import Backend, CaptureError, DeviceInfo

try:
    import evdev
    from evdev import ecodes
except ImportError:  # pragma: no cover
    evdev = None
    ecodes = None

BUS_I8042 = 0x11


class EvdevBackend(Backend):
    name = "evdev (EVIOCGRAB)"

    def __init__(self):
        self.available = evdev is not None
        self.unavailable_reason = "" if evdev else (
            "The Python package 'evdev' is not installed. Run:  pip install evdev")
        self._devices = []
        self._thread = None
        self._stop = threading.Event()
        self._on_event = None

    def permission_hint(self) -> str:
        return ("Linux: ClickerBridge needs read access to /dev/input/event*. Add yourself to the "
                "'input' group (sudo usermod -aG input $USER, then log out and in) or run with sudo.")

    @staticmethod
    def _dev_id(dev) -> str:
        return f"{dev.info.vendor:04x}:{dev.info.product:04x}:{dev.uniq or ''}:{dev.name}"

    def _open_all(self):
        devices, errors = [], []
        for path in evdev.list_devices():
            try:
                devices.append(evdev.InputDevice(path))
            except OSError as exc:
                errors.append(f"{path}: {exc}")
        return devices, errors

    def list_devices(self) -> list[DeviceInfo]:
        if not self.available:
            raise CaptureError(self.unavailable_reason)
        devices, errors = self._open_all()
        if not devices and errors:
            raise CaptureError("No input devices are readable:\n" + "\n".join(errors[:3])
                               + "\n\n" + self.permission_hint())
        wanted = {ecodes.KEY_PAGEDOWN, ecodes.KEY_PAGEUP, ecodes.KEY_SPACE, ecodes.KEY_ENTER,
                  ecodes.KEY_B, ecodes.KEY_NEXTSONG, ecodes.KEY_VOLUMEUP, ecodes.KEY_RIGHT}
        result, seen = [], set()
        for dev in devices:
            try:
                keys = set(dev.capabilities().get(ecodes.EV_KEY, []))
                if not keys & wanted:
                    continue
                did = self._dev_id(dev)
                if did in seen:
                    continue
                seen.add(did)
                builtin = dev.info.bustype == BUS_I8042 or "AT Translated" in dev.name
                details = f"{dev.info.vendor:04X}:{dev.info.product:04X}"
                details += f" · SN {dev.uniq}" if dev.uniq else " · no serial"
                details += f" · {dev.path}"
                result.append(DeviceInfo(id=did, name=dev.name, details=details, builtin=builtin))
            finally:
                dev.close()
        result.sort(key=lambda x: (x.builtin, x.name.lower()))
        return result

    def start(self, device_id: str, on_event) -> None:
        if not self.available:
            raise CaptureError(self.unavailable_reason)
        if self.is_running():
            self.stop()
        devices, _ = self._open_all()
        targets = []
        for dev in devices:
            if self._dev_id(dev) == device_id:
                targets.append(dev)
            else:
                dev.close()
        if not targets:
            raise CaptureError("The clicker is not connected. Plug it in and try again.")
        grabbed = []
        for dev in targets:
            try:
                dev.grab()
                grabbed.append(dev)
            except OSError as exc:
                on_event("status", f"Could not grab {dev.path}: {exc}")
                dev.close()
        if not grabbed:
            raise CaptureError("Could not grab the clicker exclusively. " + self.permission_hint())
        self._devices = grabbed
        self._on_event = on_event
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="evdev", daemon=True)
        self._thread.start()
        on_event("status", f"Grabbed {len(grabbed)} input node(s) of the clicker (exclusive).")

    def _loop(self) -> None:
        fds = {dev.fd: dev for dev in self._devices}
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select(list(fds), [], [], 0.25)
            except (OSError, ValueError):
                return
            for fd in ready:
                dev = fds[fd]
                try:
                    events = list(dev.read())
                except OSError as exc:
                    if not self._stop.is_set():
                        self._on_event("error", f"Lost the clicker ({exc}). Unplugged?")
                    return
                for e in events:
                    if e.type != ecodes.EV_KEY or e.value == 2:  # 2 = autorepeat
                        continue
                    name = ecodes.KEY.get(e.code)
                    if isinstance(name, (list, tuple)):
                        name = name[0]
                    sig = {"type": "evdev", "code": int(e.code), "name": name or f"KEY_{e.code}"}
                    self._on_event("press" if e.value == 1 else "release", sig)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        for dev in self._devices:
            for action in (dev.ungrab, dev.close):
                try:
                    action()
                except Exception:
                    pass
        self._devices, self._thread = [], None

    def is_running(self) -> bool:
        return bool(self._devices)
