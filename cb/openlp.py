"""Minimal OpenLP 3.x HTTP API (v2) client using only the standard library.

Endpoints (checked against OpenLP's source, openlp/core/api/versions/v2):
  POST /api/v2/controller/progress   {"action": "next" | "previous"}   -> next/previous slide
  POST /api/v2/service/progress      {"action": "next" | "previous"}   -> next/previous service item
  POST /api/v2/core/display          {"display": "blank"|"theme"|"desktop"|"show"|"hide"}
  POST /api/v2/core/login            {"username", "password"}          -> {"token": "..."}
  GET  /api/v2/core/system           -> {"websocket_port", "login_required", "api_version", ...}

When OpenLP's "Require authentication" option is on, requests must carry the raw token in
the Authorization header (no "Bearer" prefix).
"""
from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.request

DISPLAY_MODES = ("blank", "theme", "desktop", "show", "hide")


class OpenLPError(Exception):
    """A request to OpenLP failed. The message is user-facing."""


class OpenLPClient:
    def __init__(self, host="127.0.0.1", port=4316, username="", password="", timeout=3.0):
        self._lock = threading.Lock()
        self.timeout = timeout
        self.host = (host or "127.0.0.1").strip()
        self.port = int(port or 4316)
        self.username = username or ""
        self.password = password or ""
        self._token = None

    # ---- configuration --------------------------------------------------
    def configure(self, host, port, username="", password=""):
        host = (host or "127.0.0.1").strip()
        port = int(port or 4316)
        username, password = username or "", password or ""
        with self._lock:
            if (host, port, username, password) != (self.host, self.port, self.username, self.password):
                self._token = None
            self.host, self.port, self.username, self.password = host, port, username, password

    @property
    def base_url(self) -> str:
        host = self.host
        if ":" in host and not host.startswith("["):  # bare IPv6 address
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    # ---- high level actions ---------------------------------------------
    def next_slide(self):
        self._post("/api/v2/controller/progress", {"action": "next"})

    def previous_slide(self):
        self._post("/api/v2/controller/progress", {"action": "previous"})

    def next_item(self):
        self._post("/api/v2/service/progress", {"action": "next"})

    def previous_item(self):
        self._post("/api/v2/service/progress", {"action": "previous"})

    def set_display(self, mode: str):
        if mode not in DISPLAY_MODES:
            raise ValueError(f"unknown display mode {mode!r}")
        self._post("/api/v2/core/display", {"display": mode})

    def system_info(self) -> dict:
        data = self._request("GET", "/api/v2/core/system", None)
        try:
            info = json.loads(data.decode("utf-8")) if data else {}
        except ValueError:
            info = {}
        return info if isinstance(info, dict) else {}

    # ---- plumbing -------------------------------------------------------
    def _login(self) -> str:
        if not self.username and not self.password:
            raise OpenLPError("OpenLP requires a login: enter the username and password "
                              "(OpenLP → Settings → Remote → Authentication).")
        body = {"username": self.username, "password": self.password}
        try:
            data = self._request("POST", "/api/v2/core/login", body, use_token=False)
        except OpenLPError as exc:
            if "HTTP 401" in str(exc):
                raise OpenLPError("OpenLP rejected the username/password.") from exc
            raise
        try:
            token = json.loads(data.decode("utf-8")).get("token")
        except (ValueError, AttributeError):
            token = None
        if not token:
            raise OpenLPError("OpenLP login returned no token.")
        return str(token)

    def _post(self, path: str, body: dict):
        try:
            self._request("POST", path, body)
        except OpenLPError as exc:
            if "HTTP 401" not in str(exc):
                raise
            # Token missing or stale: log in once and retry.
            with self._lock:
                self._token = self._login()
            self._request("POST", path, body)

    def _request(self, method: str, path: str, body, use_token: bool = True) -> bytes:
        url = self.base_url + path
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=payload, method=method)
        if payload is not None:
            req.add_header("Content-Type", "application/json")
        token = self._token
        if use_token and token:
            req.add_header("Authorization", token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200].strip()
            except Exception:
                pass
            raise OpenLPError(f"OpenLP answered HTTP {exc.code} for {path} {detail}".strip()) from exc
        except urllib.error.URLError as exc:
            raise OpenLPError(f"Cannot reach OpenLP at {self.base_url} ({exc.reason}). "
                              "Is OpenLP running with the remote interface enabled?") from exc
        except socket.timeout as exc:
            raise OpenLPError(f"OpenLP at {self.base_url} did not answer in time.") from exc
        except (OSError, ValueError) as exc:
            raise OpenLPError(f"Request to {url} failed: {exc}") from exc
