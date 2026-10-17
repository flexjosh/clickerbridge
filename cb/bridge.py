"""The bridge core: owns the capture backend, button mappings, learn mode and OpenLP calls.

Threading model: backend threads only enqueue events; the UI thread drains the queue and
calls handle_backend_event(), so every state change happens on one thread. OpenLP HTTP calls
run on a single worker thread so they never block capture or the UI; their results come back
through the same queue ("api_result").
"""
from __future__ import annotations

import queue
import time
from concurrent.futures import ThreadPoolExecutor

from .backends.base import Backend, CaptureError, describe_signature, signature_key
from .openlp import OpenLPClient, OpenLPError
from .settings import ROLES, Settings

ROLE_TITLES = {"forward": "Forward", "backward": "Backward", "blank": "Blank toggle"}


class BridgeCore:
    def __init__(self, settings: Settings, backend: Backend, openlp: OpenLPClient):
        self.settings = settings
        self.backend = backend
        self.openlp = openlp
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.enabled = False
        self.learning: str | None = None
        self.display_blank = False
        self.capture_status = "Off"
        self._held: set[str] = set()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="openlp")
        self._closed = False

    # ---- event plumbing ---------------------------------------------------
    def post(self, kind, *args):
        self.events.put((kind, *args))

    def log(self, text: str):
        self.post("log", text)

    def _backend_event(self, kind, payload):
        """Called from backend threads: hand over to the UI thread."""
        self.events.put(("backend", kind, payload))

    # ---- mappings ---------------------------------------------------------
    @property
    def device_id(self):
        return self.settings.device_id

    def role_for(self, sig):
        key = signature_key(sig)
        for role in ROLES:
            mapped = self.settings.mapping(role)
            if mapped is not None and signature_key(mapped) == key:
                return role
        return None

    def clear_mapping(self, role: str):
        self.settings.set_mapping(role, None)
        self.log(f"Cleared the '{ROLE_TITLES[role]}' button.")
        self.post("mappings")

    # ---- capture lifecycle ------------------------------------------------
    def _want_capture(self) -> bool:
        return bool(self.device_id) and (self.enabled or self.learning is not None)

    def _reconcile(self):
        """Make the backend state match what we want. Raises CaptureError when capture fails."""
        want = self._want_capture()
        running = self.backend.is_running()
        if want and not running:
            self._held.clear()
            try:
                self.backend.start(self.device_id, self._backend_event)
            except CaptureError:
                self.enabled = False
                self.learning = None
                self.capture_status = "Off (capture failed)"
                self.post("state")
                raise
        elif not want and running:
            self.backend.stop()
            self._held.clear()
        if self.backend.is_running():
            self.capture_status = "Capturing" if self.enabled else "Capturing (learning)"
        else:
            self.capture_status = "Off"
        self.post("state")

    def set_enabled(self, enabled: bool):
        enabled = bool(enabled)
        if enabled and not self.device_id:
            raise CaptureError("Pick your clicker from the dropdown first.")
        if enabled == self.enabled:
            self.post("state")
            return
        self.enabled = enabled
        self._reconcile()  # raises CaptureError (and resets enabled) when the clicker cannot be captured
        if enabled:
            self.log("Bridge ON: the clicker's buttons go to OpenLP; nothing reaches other apps.")
        else:
            self.log("Bridge OFF: the clicker behaves like a normal keyboard again.")

    def toggle(self):
        self.set_enabled(not self.enabled)

    def begin_learn(self, role: str):
        if role not in ROLES:
            raise ValueError(role)
        if not self.device_id:
            raise CaptureError("Pick your clicker from the dropdown first.")
        self.learning = role
        self.log(f"Learning '{ROLE_TITLES[role]}': press that button on the clicker now.")
        self._reconcile()

    def cancel_learn(self):
        if self.learning is None:
            return
        self.log(f"Learning '{ROLE_TITLES[self.learning]}' cancelled.")
        self.learning = None
        self._reconcile()

    def select_device(self, device_id, device_name: str):
        if device_id == self.device_id:
            return
        was_enabled = self.enabled
        self.enabled = False
        self.learning = None
        if self.backend.is_running():
            self.backend.stop()
            self._held.clear()
        self.settings.set_device(device_id, device_name)
        self.log(f"Clicker: {device_name}" if device_id else "No clicker selected.")
        if was_enabled and device_id:
            self.enabled = True
        self._reconcile()

    # ---- events from the backend (always on the UI thread) ---------------
    def handle_backend_event(self, kind: str, payload):
        if kind == "press":
            key = signature_key(payload)
            if key in self._held:
                return  # auto-repeat while the button is held down
            self._held.add(key)
            if self.learning is not None:
                role, self.learning = self.learning, None
                other = self.role_for(payload)
                if other is not None and other != role:
                    self.settings.set_mapping(other, None)
                    self.log(f"That button was '{ROLE_TITLES[other]}'; it is now '{ROLE_TITLES[role]}'.")
                self.settings.set_mapping(role, payload)
                self.log(f"Learned '{ROLE_TITLES[role]}' = {describe_signature(payload)}")
                self.post("learned", role, payload)
                self.post("pressed", role)
                self._reconcile()
                return
            role = self.role_for(payload)
            if role is None:
                self.log(f"Unmapped button {describe_signature(payload)} - swallowed.")
                return
            if self.enabled:
                self._fire(role)
        elif kind == "release":
            if payload is None:
                self._held.clear()
            else:
                self._held.discard(signature_key(payload))
        elif kind == "status":
            self.log(str(payload))
        elif kind == "error":
            self.log(f"Capture error: {payload}")
            self.enabled = False
            self.learning = None
            if self.backend.is_running():
                self.backend.stop()
            self._held.clear()
            self.capture_status = "Off (device lost)"
            self.post("state")
            self.post("error", str(payload))

    # ---- OpenLP -----------------------------------------------------------
    def _fire(self, role: str):
        self.post("pressed", role)
        target = None
        if role == "forward":
            fn, label = self.openlp.next_slide, "Next slide"
        elif role == "backward":
            fn, label = self.openlp.previous_slide, "Previous slide"
        else:
            target = "show" if self.display_blank else "blank"
            fn, label = (lambda: self.openlp.set_display(target)), f"Display → {target}"
        started = time.monotonic()

        def run():
            try:
                fn()
            except OpenLPError as exc:
                self.post("api_result", role, False, str(exc), None)
                return
            except Exception as exc:  # never let the worker die silently
                self.post("api_result", role, False, f"{type(exc).__name__}: {exc}", None)
                return
            ms = int((time.monotonic() - started) * 1000)
            self.post("api_result", role, True, f"{label} ({ms} ms)", target)

        self._pool.submit(run)

    def handle_api_result(self, role: str, ok: bool, text: str, target):
        if ok and role == "blank" and target:
            self.display_blank = (target == "blank")
            self.post("display", self.display_blank)
        self.log(("OK: " if ok else "OpenLP error: ") + text)

    def send_test(self, role: str):
        if role in ROLES:
            self._fire(role)

    def test_openlp(self):
        def run():
            try:
                info = self.openlp.system_info()
            except OpenLPError as exc:
                self.post("test_result", False, str(exc))
                return
            version = info.get("api_version", "?")
            login = "yes" if info.get("login_required") else "no"
            self.post("test_result", True,
                      f"Connected to OpenLP at {self.openlp.base_url} (API v{version}, login required: {login})")

        self._pool.submit(run)

    def reset_display_state(self):
        self.display_blank = False
        self.post("display", False)
        self.log("Display state reset: ClickerBridge now assumes OpenLP is showing the screen.")

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        self.enabled = False
        self.learning = None
        try:
            if self.backend.is_running():
                self.backend.stop()
        finally:
            self._pool.shutdown(wait=False)
