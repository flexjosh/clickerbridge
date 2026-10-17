"""Tests for the OpenLP client and the bridge core, using a fake OpenLP HTTP server and a
fake capture backend. Run:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import queue
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cb.backends.base import Backend, CaptureError, describe_signature, signature_key  # noqa: E402
from cb.bridge import BridgeCore  # noqa: E402
from cb.openlp import OpenLPClient, OpenLPError  # noqa: E402
from cb.settings import Settings  # noqa: E402


# ---- fake OpenLP -------------------------------------------------------------
class FakeOpenLPHandler(BaseHTTPRequestHandler):
    calls: list = []
    require_login = False
    token = "secret-token"

    def log_message(self, *args):  # keep test output quiet
        pass

    def _json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _status(self, code):
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path == "/api/v2/core/system":
            self._json({"websocket_port": 4317, "login_required": self.require_login,
                        "api_version": 2, "api_revision": 7})
        else:
            self._status(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/v2/core/login":
            if body == {"username": "u", "password": "p"}:
                self._json({"token": self.token})
            else:
                self._status(401)
            return
        if self.require_login and self.headers.get("Authorization") != self.token:
            self._status(401)
            return
        FakeOpenLPHandler.calls.append((self.path, body))
        if self.path in ("/api/v2/controller/progress", "/api/v2/service/progress"):
            self._status(204 if body.get("action") in ("next", "previous") else 400)
        elif self.path == "/api/v2/core/display":
            self._status(204 if body.get("display") in ("blank", "theme", "desktop", "show", "hide") else 400)
        else:
            self._status(404)


def start_fake_openlp():
    server = HTTPServer(("127.0.0.1", 0), FakeOpenLPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# ---- fake backend ------------------------------------------------------------
class FakeBackend(Backend):
    name = "fake"
    available = True

    def __init__(self, fail_start=False):
        self.fail_start = fail_start
        self.running = False
        self.on_event = None
        self.starts = 0
        self.stops = 0

    def list_devices(self):
        return []

    def start(self, device_id, on_event):
        if self.fail_start:
            raise CaptureError("no permission")
        self.starts += 1
        self.running = True
        self.on_event = on_event

    def stop(self):
        self.stops += 1
        self.running = False

    def is_running(self):
        return self.running

    def emit(self, kind, payload):
        self.on_event(kind, payload)


def pump(core: BridgeCore, wait_for=None, timeout=3.0):
    """Drain the core's event queue the way the UI does. Returns the non-backend events."""
    out = []
    import time
    deadline = time.monotonic() + timeout
    while True:
        try:
            ev = core.events.get(timeout=0.05)
        except queue.Empty:
            if wait_for is None or any(e[0] == wait_for for e in out) or time.monotonic() > deadline:
                return out
            continue
        if ev[0] == "backend":
            core.handle_backend_event(ev[1], ev[2])
        elif ev[0] == "api_result":
            core.handle_api_result(*ev[1:])
            out.append(ev)
        else:
            out.append(ev)


SIG_FWD = {"type": "scancode", "code": 0x51, "e0": True, "e1": False}
SIG_BACK = {"type": "scancode", "code": 0x49, "e0": True, "e1": False}
SIG_BLANK = {"type": "report", "data": [0, 0, 5, 0, 0, 0, 0, 0]}
SIG_OTHER = {"type": "report", "data": [0, 0, 0x3E, 0, 0, 0, 0, 0]}


class OpenLPClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = start_fake_openlp()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeOpenLPHandler.calls.clear()
        FakeOpenLPHandler.require_login = False

    def test_actions_post_the_right_bodies(self):
        c = OpenLPClient("127.0.0.1", self.port)
        c.next_slide()
        c.previous_slide()
        c.set_display("blank")
        c.set_display("show")
        self.assertEqual(FakeOpenLPHandler.calls, [
            ("/api/v2/controller/progress", {"action": "next"}),
            ("/api/v2/controller/progress", {"action": "previous"}),
            ("/api/v2/core/display", {"display": "blank"}),
            ("/api/v2/core/display", {"display": "show"}),
        ])
        info = c.system_info()
        self.assertEqual(info["api_version"], 2)

    def test_login_flow_on_401(self):
        FakeOpenLPHandler.require_login = True
        c = OpenLPClient("127.0.0.1", self.port, "u", "p")
        c.next_slide()  # first call gets 401, logs in, retries
        c.next_slide()  # token reused
        self.assertEqual(len(FakeOpenLPHandler.calls), 2)
        bad = OpenLPClient("127.0.0.1", self.port, "u", "wrong")
        with self.assertRaises(OpenLPError) as ctx:
            bad.next_slide()
        self.assertIn("rejected", str(ctx.exception))
        nocreds = OpenLPClient("127.0.0.1", self.port)
        with self.assertRaises(OpenLPError) as ctx:
            nocreds.next_slide()
        self.assertIn("requires a login", str(ctx.exception))

    def test_unreachable_gives_clear_message(self):
        c = OpenLPClient("127.0.0.1", 1, timeout=0.5)  # port 1: nothing listens
        with self.assertRaises(OpenLPError) as ctx:
            c.next_slide()
        self.assertIn("Cannot reach OpenLP", str(ctx.exception))


class BridgeCoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = start_fake_openlp()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeOpenLPHandler.calls.clear()
        FakeOpenLPHandler.require_login = False
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(Path(self.tmp.name) / "settings.json")
        self.backend = FakeBackend()
        self.core = BridgeCore(self.settings, self.backend, OpenLPClient("127.0.0.1", self.port))

    def tearDown(self):
        self.core.shutdown()
        self.tmp.cleanup()

    def test_enable_requires_a_device(self):
        with self.assertRaises(CaptureError):
            self.core.set_enabled(True)
        self.assertFalse(self.core.enabled)

    def test_learn_then_press_fires_openlp(self):
        self.core.select_device("dev-1", "Test Clicker")
        self.core.begin_learn("forward")
        self.assertTrue(self.backend.is_running(), "learning must grab the device")
        self.backend.emit("press", SIG_FWD)
        self.backend.emit("release", SIG_FWD)
        events = pump(self.core)
        self.assertEqual(self.settings.mapping("forward"), SIG_FWD)
        self.assertIn(("learned", "forward", SIG_FWD), events)
        self.assertFalse(self.backend.is_running(), "device is released once learning is done and bridge is off")

        self.core.set_enabled(True)
        self.assertTrue(self.backend.is_running())
        self.backend.emit("press", SIG_FWD)
        self.backend.emit("press", SIG_FWD)  # auto-repeat while held: ignored
        self.backend.emit("release", SIG_FWD)
        self.backend.emit("press", SIG_FWD)  # second real press
        self.backend.emit("release", SIG_FWD)
        events = pump(self.core, wait_for="api_result")
        events += pump(self.core, wait_for="api_result")
        pressed = [e for e in events if e[0] == "pressed"]
        results = [e for e in events if e[0] == "api_result"]
        self.assertEqual(len(pressed), 2)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r[2] for r in results), results)
        self.assertEqual(FakeOpenLPHandler.calls, [("/api/v2/controller/progress", {"action": "next"})] * 2)

    def test_unmapped_button_is_swallowed_and_logged(self):
        self.core.select_device("dev-1", "Test Clicker")
        self.settings.set_mapping("forward", SIG_FWD)
        self.core.set_enabled(True)
        self.backend.emit("press", SIG_OTHER)
        self.backend.emit("release", SIG_OTHER)
        events = pump(self.core)
        self.assertFalse([e for e in events if e[0] == "pressed"])
        self.assertTrue(any("Unmapped" in e[1] for e in events if e[0] == "log"))
        self.assertEqual(FakeOpenLPHandler.calls, [])

    def test_blank_toggles_between_blank_and_show(self):
        self.core.select_device("dev-1", "Test Clicker")
        self.settings.set_mapping("blank", SIG_BLANK)
        self.core.set_enabled(True)
        for _ in range(3):
            self.backend.emit("press", SIG_BLANK)
            self.backend.emit("release", SIG_BLANK)
            pump(self.core, wait_for="api_result")
        self.assertEqual([b["display"] for _, b in FakeOpenLPHandler.calls], ["blank", "show", "blank"])
        self.assertTrue(self.core.display_blank)
        self.core.reset_display_state()
        self.assertFalse(self.core.display_blank)

    def test_mac_style_release_with_none_clears_everything(self):
        self.core.select_device("dev-1", "Test Clicker")
        self.settings.set_mapping("backward", SIG_BACK)
        self.core.set_enabled(True)
        self.backend.emit("press", SIG_BACK)
        self.backend.emit("release", None)
        self.backend.emit("press", SIG_BACK)
        events = pump(self.core, wait_for="api_result")
        events += pump(self.core, wait_for="api_result")
        self.assertEqual(len([e for e in events if e[0] == "pressed"]), 2)

    def test_relearning_moves_a_button_between_roles(self):
        self.core.select_device("dev-1", "Test Clicker")
        self.settings.set_mapping("forward", SIG_FWD)
        self.core.begin_learn("backward")
        self.backend.emit("press", SIG_FWD)
        pump(self.core)
        self.assertIsNone(self.settings.mapping("forward"))
        self.assertEqual(self.settings.mapping("backward"), SIG_FWD)

    def test_capture_failure_turns_bridge_off(self):
        self.backend.fail_start = True
        self.core.select_device("dev-1", "Test Clicker")
        with self.assertRaises(CaptureError):
            self.core.set_enabled(True)
        self.assertFalse(self.core.enabled)
        self.assertIn("failed", self.core.capture_status)

    def test_device_loss_turns_bridge_off(self):
        self.core.select_device("dev-1", "Test Clicker")
        self.core.set_enabled(True)
        self.backend.emit("error", "unplugged")
        events = pump(self.core)
        self.assertFalse(self.core.enabled)
        self.assertEqual(self.backend.stops, 1)
        self.assertTrue(any(e[0] == "error" for e in events))

    def test_switching_device_restarts_capture_when_enabled(self):
        self.core.select_device("dev-1", "A")
        self.core.set_enabled(True)
        self.core.select_device("dev-2", "B")
        self.assertTrue(self.core.enabled)
        self.assertEqual((self.backend.starts, self.backend.stops), (2, 1))
        self.assertEqual(self.settings.device_id, "dev-2")

    def test_openlp_failure_reports_error_result(self):
        self.core.select_device("dev-1", "Test Clicker")
        self.settings.set_mapping("forward", SIG_FWD)
        self.core.openlp.configure("127.0.0.1", 1)
        self.core.openlp.timeout = 0.5
        self.core.set_enabled(True)
        self.backend.emit("press", SIG_FWD)
        events = pump(self.core, wait_for="api_result")
        result = next(e for e in events if e[0] == "api_result")
        self.assertFalse(result[2])
        self.assertIn("Cannot reach OpenLP", result[3])


class SettingsAndNamesTests(unittest.TestCase):
    def test_settings_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.json"
            s = Settings(path)
            s.set_device("abc", "My Clicker")
            s.set_mapping("forward", SIG_FWD)
            s.openlp["host"] = "10.0.0.5"
            s.save()
            s2 = Settings(path)
            self.assertEqual(s2.device_id, "abc")
            self.assertEqual(s2.device_name, "My Clicker")
            self.assertEqual(s2.mapping("forward"), SIG_FWD)
            self.assertEqual(s2.openlp["host"], "10.0.0.5")
            self.assertEqual(s2.openlp["port"], 4316)  # default kept

    def test_describe_signature(self):
        self.assertEqual(describe_signature(SIG_FWD), "Page Down")
        self.assertEqual(describe_signature(SIG_BACK), "Page Up")
        self.assertTrue(describe_signature(SIG_BLANK).startswith("B  (report"))
        self.assertTrue(describe_signature({"type": "report", "data": [1, 0, 0, 0x4E, 0, 0, 0, 0]}).startswith("Page Down"))
        self.assertEqual(describe_signature({"type": "report", "data": [2, 0xB5, 0]}), "Next Track  (report 02 B5 00)")
        self.assertEqual(describe_signature({"type": "evdev", "code": 109, "name": "KEY_PAGEDOWN"}), "Pagedown")
        self.assertEqual(describe_signature(None), "not set")
        self.assertEqual(signature_key({"b": 1, "a": 2}), '{"a":2,"b":1}')


if __name__ == "__main__":
    unittest.main(verbosity=2)
