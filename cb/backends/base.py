"""Shared types for the capture backends.

A backend grabs ONE device exclusively (so its button presses never reach the operating
system) and reports presses/releases as *signatures*: small JSON-friendly dicts that
identify a button in a backend-specific way:

  {"type": "scancode", "code": 81, "e0": true, "e1": false}   Windows (Interception driver)
  {"type": "report",   "data": [1, 0, 0, 78, 0, 0, 0, 0]}      macOS   (raw HID input report)
  {"type": "evdev",    "code": 109, "name": "KEY_PAGEDOWN"}     Linux   (evdev key code)

The core never interprets signatures; it only compares them, so any button a device can
send (keyboard key, media key, vendor-specific report) can be mapped.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable


@dataclass
class DeviceInfo:
    id: str                 # stable identifier stored in settings
    name: str               # friendly name
    details: str            # VID:PID, serial, interface kinds ...
    builtin: bool = False   # probably the computer's own keyboard: hidden unless "show all"

    @property
    def label(self) -> str:
        return f"{self.name}   [{self.details}]"


class CaptureError(Exception):
    """A backend could not list or capture devices. The message is user-facing."""


EventCallback = Callable[[str, object], None]
"""on_event(kind, payload); kind is "press", "release", "status" or "error"."""


class Backend:
    name = "none"
    available = False
    unavailable_reason = "No capture backend exists for this platform."

    def list_devices(self) -> list[DeviceInfo]:
        raise CaptureError(self.unavailable_reason)

    def start(self, device_id: str, on_event: EventCallback) -> None:
        raise CaptureError(self.unavailable_reason)

    def stop(self) -> None:
        pass

    def is_running(self) -> bool:
        return False

    def permission_hint(self) -> str:
        return ""


def signature_key(sig) -> str:
    """Canonical string form, for comparisons and set membership."""
    return json.dumps(sig, sort_keys=True, separators=(",", ":"))


# ---- human readable names ----------------------------------------------------

_HID_KEYS = {
    0x28: "Enter", 0x29: "Esc", 0x2A: "Backspace", 0x2B: "Tab", 0x2C: "Space", 0x37: ".",
    0x4A: "Home", 0x4B: "Page Up", 0x4D: "End", 0x4E: "Page Down",
    0x4F: "Right", 0x50: "Left", 0x51: "Down", 0x52: "Up",
}
for _i in range(26):
    _HID_KEYS[0x04 + _i] = chr(ord("A") + _i)
for _i in range(9):
    _HID_KEYS[0x1E + _i] = str(_i + 1)
_HID_KEYS[0x27] = "0"
for _i in range(12):
    _HID_KEYS[0x3A + _i] = f"F{_i + 1}"

_CONSUMER = {
    0xB0: "Play", 0xB1: "Pause", 0xB3: "Fast Forward", 0xB4: "Rewind", 0xB5: "Next Track",
    0xB6: "Previous Track", 0xB7: "Stop", 0xCD: "Play/Pause", 0xE2: "Mute",
    0xE9: "Volume Up", 0xEA: "Volume Down", 0x223: "Browser Home", 0x224: "Back", 0x225: "Forward",
}

_SCANCODES = {  # (make code, extended E0 prefix)
    (0x01, False): "Esc", (0x0E, False): "Backspace", (0x0F, False): "Tab", (0x1C, False): "Enter",
    (0x1C, True): "Keypad Enter", (0x39, False): "Space", (0x34, False): ".",
    (0x47, True): "Home", (0x48, True): "Up", (0x49, True): "Page Up", (0x4B, True): "Left",
    (0x4D, True): "Right", (0x4F, True): "End", (0x50, True): "Down", (0x51, True): "Page Down",
    (0x52, True): "Insert", (0x53, True): "Delete",
    (0x20, True): "Mute", (0x30, True): "Volume Up", (0x2E, True): "Volume Down",
    (0x19, True): "Next Track", (0x10, True): "Previous Track", (0x22, True): "Play/Pause",
}
for _i, _code in enumerate((0x3B, 0x3C, 0x3D, 0x3E, 0x3F, 0x40, 0x41, 0x42, 0x43, 0x44, 0x57, 0x58)):
    _SCANCODES[(_code, False)] = f"F{_i + 1}"
for _row, _base in (("QWERTYUIOP", 0x10), ("ASDFGHJKL", 0x1E), ("ZXCVBNM", 0x2C)):
    for _i, _ch in enumerate(_row):
        _SCANCODES[(_base + _i, False)] = _ch
for _i, _ch in enumerate("1234567890"):
    _SCANCODES[(0x02 + _i, False)] = _ch


def _guess_report(data: list[int]) -> str:
    """Best-effort decoding of a raw HID input report (boot keyboard or consumer control)."""
    # Keyboard report: [modifiers, reserved, key1..key6], possibly preceded by a report-ID byte.
    # A small first byte followed by a zero is more likely a report ID than a modifier, so try
    # the "with report ID" reading first in that case.
    offsets = (1, 0) if (len(data) >= 4 and 1 <= data[0] <= 3 and data[1] == 0) else (0, 1)
    for offset in offsets:
        seg = data[offset:offset + 8]
        if len(seg) >= 3 and seg[1] == 0:
            keys = [k for k in seg[2:8] if k]
            if keys and all(4 <= k <= 0xE7 for k in keys):
                mods = seg[0]
                prefix = ""
                if mods & 0x11:
                    prefix += "Ctrl+"
                if mods & 0x22:
                    prefix += "Shift+"
                if mods & 0x44:
                    prefix += "Alt+"
                if mods & 0x88:
                    prefix += "Cmd+"
                return prefix + "+".join(_HID_KEYS.get(k, f"key 0x{k:02X}") for k in keys)
    if 2 <= len(data) <= 5:
        candidates = []
        if len(data) >= 3:
            candidates.append(data[1] | (data[2] << 8))
        candidates.append(data[0] | (data[1] << 8))
        candidates.append(data[1])
        for usage in candidates:
            if usage in _CONSUMER:
                return _CONSUMER[usage]
    return ""


def describe_signature(sig) -> str:
    if not sig or not isinstance(sig, dict):
        return "not set"
    kind = sig.get("type")
    if kind == "scancode":
        code, e0 = int(sig.get("code", 0)), bool(sig.get("e0"))
        return _SCANCODES.get((code, e0), f"scancode 0x{code:02X}{' E0' if e0 else ''}")
    if kind == "report":
        data = [int(b) for b in sig.get("data", [])]
        hexs = " ".join(f"{b:02X}" for b in data[:16])
        guess = _guess_report(data)
        return f"{guess}  (report {hexs})" if guess else f"report {hexs}"
    if kind == "evdev":
        name = sig.get("name") or f"code {sig.get('code')}"
        if isinstance(name, str) and name.startswith("KEY_"):
            return name[4:].replace("_", " ").title()
        return str(name)
    return str(sig)
