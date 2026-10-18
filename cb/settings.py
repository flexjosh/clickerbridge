"""Persistent settings (JSON file in the per-user config folder)."""
from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path

APP_NAME = "ClickerBridge"
ROLES = ("forward", "backward", "blank")


def platform_key() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("win"):
        return "win32"
    return "linux"


def config_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform.startswith("win"):
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / APP_NAME


DEFAULTS = {
    "openlp": {"host": "127.0.0.1", "port": 4316, "username": "", "password": ""},
    "enable_on_launch": False,
    "block_keys": True,  # macOS: keep the clicker's keys out of other apps
    "show_all_devices": False,
    # One section per platform so the same settings file can travel between machines.
    # Each section: {"device": <backend id>, "device_name": str, "mappings": {role: signature}}
    "platforms": {},
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = path or (config_dir() / "settings.json")
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    # ---- file I/O -------------------------------------------------------
    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                self.data = _merge(DEFAULTS, loaded)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            print(f"[settings] could not read {self.path}: {exc}", file=sys.stderr)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError as exc:
            print(f"[settings] could not write {self.path}: {exc}", file=sys.stderr)

    # ---- OpenLP -----------------------------------------------------------
    @property
    def openlp(self) -> dict:
        return self.data["openlp"]

    # ---- per-platform device + mappings ----------------------------------
    @property
    def section(self) -> dict:
        sec = self.data["platforms"].setdefault(platform_key(), {})
        sec.setdefault("device", None)
        sec.setdefault("device_name", "")
        sec.setdefault("mappings", {})
        return sec

    @property
    def device_id(self):
        return self.section["device"]

    @property
    def device_name(self) -> str:
        return self.section["device_name"] or ""

    def set_device(self, device_id, device_name: str) -> None:
        self.section["device"] = device_id
        self.section["device_name"] = device_name or ""
        self.save()

    def mapping(self, role: str):
        return self.section["mappings"].get(role)

    def set_mapping(self, role: str, signature) -> None:
        if signature is None:
            self.section["mappings"].pop(role, None)
        else:
            self.section["mappings"][role] = signature
        self.save()
