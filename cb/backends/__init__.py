"""Capture backends: one per operating system, picked by load_backend()."""
from __future__ import annotations

import sys

from .base import Backend, CaptureError, DeviceInfo, describe_signature, signature_key

__all__ = ["Backend", "CaptureError", "DeviceInfo", "describe_signature", "signature_key", "load_backend"]


def load_backend() -> Backend:
    if sys.platform == "darwin":
        from .mac_hidapi import HidapiBackend
        return HidapiBackend()
    if sys.platform.startswith("win"):
        from .win_interception import InterceptionBackend
        return InterceptionBackend()
    from .linux_evdev import EvdevBackend
    return EvdevBackend()
