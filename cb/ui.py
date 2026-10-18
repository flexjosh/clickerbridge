"""tkinter user interface for ClickerBridge, written for a volunteer who has never seen it."""
from __future__ import annotations

import queue
import sys
import time
import tkinter as tk
from tkinter import messagebox, ttk

from . import __version__
from .backends.base import CaptureError, DeviceInfo, describe_signature
from .bridge import ROLE_TITLES, BridgeCore
from .settings import ROLES, Settings

BADGES = (("backward", "LEFT"), ("forward", "RIGHT"), ("blank", "BLANK"))
ROLE_BUTTON = {
    "forward": "Capture forward click",
    "backward": "Capture backward click",
    "blank": "Capture blank screen toggle",
}
ROLE_ACTION = {
    "forward": "→ OpenLP next slide",
    "backward": "→ OpenLP previous slide",
    "blank": "→ OpenLP blank ⇄ show",
}

C_ON, C_OFF = "#1f9d55", "#8a8f98"
C_MUTED, C_TEXT, C_ACCENT = "#5b6472", "#1f2933", "#1d6fa5"
C_BADGE_IDLE, C_BADGE_FG = "#e4e7eb", "#5b6472"
C_HIT, C_ERR, C_WARN = "#2ecc71", "#e74c3c", "#f39c12"
C_DOT_OFF = "#b0b6bf"
WRAP = 820

TAGLINE = ("Control OpenLP's live slides with your presentation clicker, even when OpenLP is not "
           "the window in front.")
INTRO = ("A clicker normally just presses Page Up / Page Down in whatever window is active. ClickerBridge "
         "takes the clicker over and sends its buttons straight to OpenLP instead, so anything can be on "
         "screen and the clicker still moves the slides. Set it up once with the four steps below; "
         "afterwards just start this app and turn the bridge on.")

STEP_TITLES = {
    1: "Step 1 · Pick your clicker",
    2: "Step 2 · Teach it the buttons",
    3: "Step 3 · Point it at OpenLP",
    4: "Step 4 · Turn the bridge on",
}
STEP_1_DESC = ("Plug the clicker (or its little USB receiver) in, then choose it in the list. "
               "Only this device is taken over; every other keyboard and mouse keeps working normally.")
STEP_2_DESC = ("Click a Capture button, then press the matching button on the clicker. "
               "The app remembers which physical button means forward, backward and blank.")
STEP_3_DESC = ("In OpenLP: Settings → Configure OpenLP → Remote Interface, tick Enable. "
               "Leave the address alone when OpenLP runs on this computer, then click Test connection.")
CHECK_DESC = ("Press the buttons on the clicker: the matching badge lights up green and OpenLP moves. "
              "Red means OpenLP refused the command - the log at the bottom says why.")

HELP_TEXT = f"""ClickerBridge {__version__}

WHAT IT IS FOR
A presentation clicker normally only works when the presentation program is the active
window. ClickerBridge grabs the clicker for itself and turns its buttons into commands
sent to OpenLP's remote interface, so OpenLP can stay in the background and the clicker
still changes the slides. While the bridge is on, the clicker does nothing else; turn the
bridge off (or close the app) and it is an ordinary clicker again.

SETTING UP (once)
1. Pick your clicker: plug it in, click Refresh if needed, choose it in the list.
   Do not pick the computer's own keyboard.
2. Teach it the buttons: click "Capture forward click", then press the clicker's forward
   button. Do the same for backward and, if the clicker has one, the blank-screen button.
3. Point it at OpenLP: in OpenLP open Settings → Configure OpenLP → Remote Interface and
   tick Enable. The usual address is 127.0.0.1 and port 4316 when OpenLP runs on this
   computer. If OpenLP runs on another computer, type that computer's address instead.
   User and password are only needed when OpenLP's "Require authentication" is ticked.
   Click Test connection; it should say "Connected".
4. Turn the bridge on with the big button. Check with the badges: LEFT / RIGHT / BLANK
   light up green when the clicker's buttons are recognised.

EVERY TIME
Start ClickerBridge, make sure OpenLP is running, click the big button (or press
Ctrl+Alt+K anywhere). Leave the app open; minimising it is fine.

IF SOMETHING IS WRONG
- The clicker is not in the list: plug it in and click Refresh. Tick "Show all devices"
  to see everything the computer knows about.
- Windows says the driver is not installed: run install-interception.exe /install as
  Administrator (it is in the Interception download) and restart the computer.
- macOS says it cannot open the clicker: allow Input Monitoring for the program that
  started ClickerBridge (System Settings → Privacy & Security → Input Monitoring),
  then quit the app and start it again.
- The badge flashes red: OpenLP refused or did not answer. Check Step 3 and that OpenLP
  is running with the remote interface enabled.
- Blank is the wrong way round: OpenLP cannot tell the app whether the screen is blank,
  so if someone blanked it inside OpenLP, press the clicker's blank button once more or
  click Reset.
"""


class App:
    def __init__(self, settings: Settings, core: BridgeCore, backend_name: str, start_minimized: bool = False):
        self.settings = settings
        self.core = core
        self.backend_name = backend_name
        self.devices: list[DeviceInfo] = []
        self.backend_error = ""
        self.openlp_ok: bool | None = None
        self._flash_tokens: dict[str, object] = {}
        self._help_window = None
        self._auto_pending = False
        self._auto_logged = False

        self.root = tk.Tk()
        self.root.title("ClickerBridge - clicker → OpenLP")
        self.root.minsize(760, 520)
        self._build()
        self._fit_window()
        self._log(f"ClickerBridge {__version__} started. Settings file: {self.settings.path}")
        self._refresh_devices(initial=True)
        self._refresh_mappings()
        self._refresh_state()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(30, self._poll)
        if self.settings.data.get("enable_on_launch") and self.settings.device_id:
            self._auto_pending = True
            self.root.after(600, self._auto_enable)
        if start_minimized:
            self.root.after(100, self.root.iconify)

    def run(self):
        self.root.mainloop()

    def _auto_enable(self):
        """Turn the bridge on at startup; keep trying quietly until the clicker is plugged in."""
        if not self._auto_pending or self.core.enabled:
            self._auto_pending = False
            return
        try:
            self.core.set_enabled(True)
        except CaptureError as exc:
            if not self._auto_logged:
                self._auto_logged = True
                self._log(f"Cannot turn the bridge on yet ({str(exc).splitlines()[0]}) - "
                          "will keep trying every 5 seconds.")
            self.root.after(5000, self._auto_enable)
            return
        self._auto_pending = False
        self._refresh_devices()
        self._refresh_state()
        self._refresh_mappings()

    # ---- layout -------------------------------------------------------------
    def _build(self):
        # Everything sits in a scrollable page so the app still works on small laptop screens.
        style = ttk.Style(self.root)
        page_bg = style.lookup("TFrame", "background") or self.root.cget("bg")
        container = ttk.Frame(self.root)
        container.pack(fill="both", expand=True)
        self.page = tk.Canvas(container, highlightthickness=0, bg=page_bg)
        page_scroll = ttk.Scrollbar(container, orient="vertical", command=self.page.yview)
        self.page.configure(yscrollcommand=page_scroll.set)
        page_scroll.pack(side="right", fill="y")
        self.page.pack(side="left", fill="both", expand=True)
        outer = ttk.Frame(self.page, padding=(12, 10))
        self._page_item = self.page.create_window((0, 0), window=outer, anchor="nw")
        outer.bind("<Configure>", lambda _e: self.page.configure(scrollregion=self.page.bbox("all")))
        self.page.bind("<Configure>", lambda e: self.page.itemconfigure(self._page_item, width=e.width))
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)
        self.root.bind_all("<Button-4>", self._on_mousewheel)
        self.root.bind_all("<Button-5>", self._on_mousewheel)

        # Header ------------------------------------------------------------
        head = ttk.Frame(outer)
        head.pack(fill="x")
        title_row = ttk.Frame(head)
        title_row.pack(fill="x")
        ttk.Label(title_row, text="ClickerBridge", font=("TkDefaultFont", 18, "bold")).pack(side="left")
        ttk.Button(title_row, text="Help", command=self._show_help).pack(side="right")
        ttk.Label(head, text=TAGLINE, font=("TkDefaultFont", 11, "bold"), foreground=C_TEXT,
                  wraplength=WRAP, justify="left").pack(fill="x", pady=(2, 2))
        ttk.Label(head, text=INTRO, foreground=C_MUTED, wraplength=WRAP, justify="left").pack(fill="x")
        self.next_step = ttk.Label(head, text="", font=("TkDefaultFont", 11, "bold"), foreground=C_ACCENT,
                                   wraplength=WRAP, justify="left")
        self.next_step.pack(fill="x", pady=(8, 8))

        self.step_boxes: dict[int, ttk.LabelFrame] = {}

        # Step 1: clicker ----------------------------------------------------
        box = ttk.LabelFrame(outer, text=STEP_TITLES[1], padding=8)
        box.pack(fill="x", pady=(0, 8))
        self.step_boxes[1] = box
        ttk.Label(box, text=STEP_1_DESC, foreground=C_MUTED, wraplength=WRAP, justify="left").pack(fill="x", pady=(0, 6))
        row = ttk.Frame(box)
        row.pack(fill="x")
        ttk.Label(row, text="Clicker:").pack(side="left")
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(row, textvariable=self.device_var, state="readonly", width=70)
        self.device_combo.pack(side="left", fill="x", expand=True, padx=6)
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)
        ttk.Button(row, text="Refresh", command=self._refresh_devices).pack(side="left")
        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(4, 0))
        self.show_all_var = tk.BooleanVar(value=bool(self.settings.data.get("show_all_devices")))
        ttk.Checkbutton(row2, text="Show all devices (includes this computer's own keyboard - never pick that one)",
                        variable=self.show_all_var, command=self._toggle_show_all).pack(side="left")
        status_row = ttk.Frame(box)
        status_row.pack(fill="x", pady=(6, 0))
        self.dot = tk.Label(status_row, text="●", fg=C_DOT_OFF, font=("TkDefaultFont", 14))
        self.dot.pack(side="left")
        self.status_label = ttk.Label(status_row, text="")
        self.status_label.pack(side="left", padx=4)

        # Step 2: buttons ----------------------------------------------------
        box = ttk.LabelFrame(outer, text=STEP_TITLES[2], padding=8)
        box.pack(fill="x", pady=(0, 8))
        self.step_boxes[2] = box
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text=STEP_2_DESC, foreground=C_MUTED, wraplength=WRAP, justify="left").grid(
            row=0, column=0, columnspan=5, sticky="w", pady=(0, 6))
        self.mapping_labels: dict[str, ttk.Label] = {}
        self.capture_buttons: dict[str, ttk.Button] = {}
        for i, role in enumerate(ROLES, start=1):
            ttk.Label(box, text=ROLE_TITLES[role] + ":", width=13).grid(row=i, column=0, sticky="w", pady=3)
            lbl = ttk.Label(box, text="not set", foreground=C_MUTED)
            lbl.grid(row=i, column=1, sticky="w", padx=6)
            self.mapping_labels[role] = lbl
            btn = ttk.Button(box, text=ROLE_BUTTON[role], width=34, command=lambda r=role: self._capture(r))
            btn.grid(row=i, column=2, padx=4)
            self.capture_buttons[role] = btn
            ttk.Button(box, text="✕", width=3, command=lambda r=role: self.core.clear_mapping(r)).grid(row=i, column=3)
            ttk.Label(box, text=ROLE_ACTION[role], foreground=C_MUTED).grid(row=i, column=4, sticky="w", padx=(10, 0))
        self.learn_hint = ttk.Label(box, text="", foreground=C_MUTED, wraplength=WRAP, justify="left")
        self.learn_hint.grid(row=len(ROLES) + 1, column=0, columnspan=5, sticky="w", pady=(6, 0))

        # Step 3: OpenLP -----------------------------------------------------
        box = ttk.LabelFrame(outer, text=STEP_TITLES[3], padding=8)
        box.pack(fill="x", pady=(0, 8))
        self.step_boxes[3] = box
        ttk.Label(box, text=STEP_3_DESC, foreground=C_MUTED, wraplength=WRAP, justify="left").pack(fill="x", pady=(0, 6))
        o = self.settings.openlp
        self.host_var = tk.StringVar(value=str(o.get("host", "127.0.0.1")))
        self.port_var = tk.StringVar(value=str(o.get("port", 4316)))
        self.user_var = tk.StringVar(value=str(o.get("username", "")))
        self.pass_var = tk.StringVar(value=str(o.get("password", "")))
        r = ttk.Frame(box)
        r.pack(fill="x")
        ttk.Label(r, text="Address:").pack(side="left")
        e1 = ttk.Entry(r, textvariable=self.host_var, width=20)
        e1.pack(side="left", padx=(4, 12))
        ttk.Label(r, text="Port:").pack(side="left")
        e2 = ttk.Entry(r, textvariable=self.port_var, width=7)
        e2.pack(side="left", padx=(4, 12))
        ttk.Label(r, text="User:").pack(side="left")
        e3 = ttk.Entry(r, textvariable=self.user_var, width=12)
        e3.pack(side="left", padx=(4, 12))
        ttk.Label(r, text="Password:").pack(side="left")
        e4 = ttk.Entry(r, textvariable=self.pass_var, width=12, show="•")
        e4.pack(side="left", padx=(4, 12))
        ttk.Button(r, text="Test connection", command=self._test_openlp).pack(side="left")
        for entry in (e1, e2, e3, e4):
            entry.bind("<FocusOut>", lambda _e: self._apply_openlp())
            entry.bind("<Return>", lambda _e: self._apply_openlp())
        self.test_label = ttk.Label(
            box, foreground=C_MUTED, wraplength=WRAP, justify="left",
            text="User and password are only needed when OpenLP's 'Require authentication' is ticked.")
        self.test_label.pack(fill="x", pady=(4, 0))

        # Step 4: big toggle -------------------------------------------------
        box = ttk.LabelFrame(outer, text=STEP_TITLES[4], padding=(8, 4))
        box.pack(fill="x", pady=(0, 8))
        self.step_boxes[4] = box
        self.toggle = tk.Canvas(box, height=80, highlightthickness=0, cursor="hand2", bg=page_bg)
        self.toggle.pack(fill="x", pady=(2, 4))
        self.toggle.bind("<Button-1>", lambda _e: self._toggle())
        self.toggle.bind("<Configure>", lambda _e: self._draw_toggle())
        self.auto_on_var = tk.BooleanVar(value=bool(self.settings.data.get("enable_on_launch")))
        ttk.Checkbutton(box, text="Turn the bridge on automatically whenever this app starts "
                                  "(tick this if ClickerBridge runs as a startup program)",
                        variable=self.auto_on_var, command=self._toggle_auto_on).pack(anchor="w", pady=(0, 4))

        # Check: test panel --------------------------------------------------
        box = ttk.LabelFrame(outer, text="Check that it works", padding=8)
        box.pack(fill="x", pady=(0, 8))
        ttk.Label(box, text=CHECK_DESC, foreground=C_MUTED, wraplength=WRAP, justify="left").pack(fill="x")
        row = ttk.Frame(box)
        row.pack()
        self.badges: dict[str, tk.Label] = {}
        for role, text in BADGES:
            badge = tk.Label(row, text=text, width=12, height=2, bg=C_BADGE_IDLE, fg=C_BADGE_FG,
                             font=("TkDefaultFont", 13, "bold"), relief="flat", bd=0, cursor="hand2")
            badge.pack(side="left", padx=10, pady=6)
            badge.bind("<Button-1>", lambda _e, r=role: self._send_test(r))
            self.badges[role] = badge
        info = ttk.Frame(box)
        info.pack(fill="x")
        self.display_label = ttk.Label(info, text="", foreground=C_MUTED)
        self.display_label.pack(side="left")
        ttk.Button(info, text="Reset", width=6, command=self.core.reset_display_state).pack(side="left", padx=6)
        ttk.Label(info, text="Click a badge to send that command without the clicker.", foreground=C_MUTED).pack(side="right")
        self._set_display_text(False)

        # Log ----------------------------------------------------------------
        box = ttk.LabelFrame(outer, text="Log (what the app is doing)", padding=4)
        box.pack(fill="both", expand=True)
        self.log_text = tk.Text(box, height=5, wrap="word", state="disabled", font=("TkFixedFont", 9))
        scroll = ttk.Scrollbar(box, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.footer = ttk.Label(outer, text="", foreground=C_MUTED, wraplength=WRAP, justify="left")
        self.footer.pack(fill="x", pady=(4, 0))

    # ---- window sizing / scrolling ------------------------------------------
    def _fit_window(self):
        """Open at the page's natural size, shrunk to fit the screen (the page scrolls if needed)."""
        self.root.update_idletasks()
        page = self.page.winfo_children()[0] if self.page.winfo_children() else None
        natural_w = (page.winfo_reqwidth() if page else 900) + 24
        natural_h = (page.winfo_reqheight() if page else 900) + 8
        width = min(max(natural_w, 900), self.root.winfo_screenwidth() - 40)
        height = min(natural_h, self.root.winfo_screenheight() - 110)
        self.root.geometry(f"{width}x{height}")

    def _on_mousewheel(self, event):
        if str(event.widget).startswith(str(self.log_text)):
            return  # the log scrolls by itself
        if self.page.yview() == (0.0, 1.0):
            return  # whole page already visible
        if getattr(event, "num", None) == 4:
            step = -1
        elif getattr(event, "num", None) == 5:
            step = 1
        elif sys.platform == "darwin":
            step = -int(event.delta)
        else:
            step = -int(event.delta / 120)
        if step:
            self.page.yview_scroll(step, "units")

    # ---- guidance -----------------------------------------------------------
    def _steps_done(self) -> dict[int, bool]:
        return {
            1: bool(self.settings.device_id),
            2: bool(self.settings.mapping("forward") and self.settings.mapping("backward")),
            3: self.openlp_ok is True,
            4: self.core.enabled,
        }

    def _next_step_text(self) -> str:
        done = self._steps_done()
        if self.backend_error and not done[1]:
            return f"Before anything else: {self.backend_error}  (click Help for details)"
        if not done[1]:
            return "Next: plug the clicker in and pick it from the list in Step 1."
        if not done[2]:
            return "Next: teach the forward and backward buttons in Step 2 (blank is optional)."
        if not done[3]:
            return "Next: click Test connection in Step 3 so we know OpenLP answers."
        if not done[4]:
            return "Next: click the big button in Step 4 to turn the bridge on."
        return "All set: the clicker now controls OpenLP. Leave this window open (minimised is fine)."

    def _refresh_guidance(self):
        done = self._steps_done()
        for n, box in self.step_boxes.items():
            box.configure(text=STEP_TITLES[n] + ("   ✓" if done[n] else ""))
        self.next_step.configure(text=self._next_step_text())

    def _show_help(self):
        if self._help_window is not None and self._help_window.winfo_exists():
            self._help_window.lift()
            return
        win = tk.Toplevel(self.root)
        win.title("ClickerBridge help")
        win.geometry("720x640")
        text = tk.Text(win, wrap="word", font=("TkDefaultFont", 10), padx=12, pady=10)
        scroll = ttk.Scrollbar(win, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.insert("1.0", HELP_TEXT)
        text.configure(state="disabled")
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self._help_window = win

    # ---- big toggle button ------------------------------------------------
    def _draw_toggle(self):
        c = self.toggle
        c.delete("all")
        w, h = max(c.winfo_width(), 10), max(c.winfo_height(), 10)
        on = self.core.enabled
        done = self._steps_done()
        self._round_rect(c, 2, 2, w - 2, h - 2, 18, fill=(C_ON if on else C_OFF), outline="")
        if on:
            title, sub = "BRIDGE IS ON", "the clicker controls OpenLP   ·   click to turn off"
        elif not done[1]:
            title, sub = "BRIDGE IS OFF", "pick your clicker in Step 1 first"
        elif not done[2]:
            title, sub = "BRIDGE IS OFF", "teach the buttons in Step 2 first, then click here"
        else:
            title, sub = "BRIDGE IS OFF", "click to turn on"
        c.create_text(w / 2, h / 2 - 13, text=title, fill="white", font=("TkDefaultFont", 20, "bold"))
        c.create_text(w / 2, h / 2 + 18, text=sub, fill="white", font=("TkDefaultFont", 11))

    @staticmethod
    def _round_rect(c, x1, y1, x2, y2, r, **kw):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
               x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return c.create_polygon(pts, smooth=True, **kw)

    # ---- devices ----------------------------------------------------------
    def _refresh_devices(self, initial: bool = False):
        show_all = bool(self.show_all_var.get())
        try:
            devices = self.core.backend.list_devices()
            self.backend_error = ""
            self.footer.configure(text=f"Capture method on this computer: {self.backend_name}")
        except CaptureError as exc:
            devices = []
            self.backend_error = str(exc).splitlines()[0]
            self._log(f"Device list: {exc}")
            self.footer.configure(text=self.backend_error)
            if not initial:
                messagebox.showerror("ClickerBridge", str(exc))
        self.devices = [d for d in devices if show_all or not d.builtin]
        labels = [d.label for d in self.devices]
        self.device_combo["values"] = labels
        saved = self.settings.device_id
        index = next((i for i, d in enumerate(self.devices) if d.id == saved), None)
        if index is not None:
            self.device_combo.current(index)
        elif saved:
            self.device_var.set(f"{self.settings.device_name}   [saved - not plugged in right now]")
        else:
            self.device_var.set("— choose your clicker —" if labels else "— no clicker found: plug it in and click Refresh —")
        self._refresh_guidance()

    def _on_device_selected(self, _event=None):
        index = self.device_combo.current()
        if 0 <= index < len(self.devices):
            device = self.devices[index]
            self._safe(lambda: self.core.select_device(device.id, device.name))

    def _toggle_show_all(self):
        self.settings.data["show_all_devices"] = bool(self.show_all_var.get())
        self.settings.save()
        self._refresh_devices()

    # ---- buttons / learn --------------------------------------------------
    def _capture(self, role: str):
        if self.core.learning == role:
            self.core.cancel_learn()
            self._refresh_mappings()
            return
        self._apply_openlp()
        self._safe(lambda: self.core.begin_learn(role))

    def _refresh_mappings(self):
        for role in ROLES:
            sig = self.settings.mapping(role)
            self.mapping_labels[role].configure(text=describe_signature(sig),
                                                foreground=(C_TEXT if sig else C_MUTED))
            if self.core.learning == role:
                self.capture_buttons[role].configure(text="Press it on the clicker now… (click to cancel)")
            else:
                self.capture_buttons[role].configure(text=ROLE_BUTTON[role])
        if self.core.learning:
            self.learn_hint.configure(
                text=f"Waiting for you to press the clicker's '{ROLE_TITLES[self.core.learning]}' button…",
                foreground="#d35400")
        else:
            self.learn_hint.configure(
                text="While the bridge is on, every button on the clicker is swallowed; only the ones taught here do something.",
                foreground=C_MUTED)
        self._refresh_guidance()

    # ---- OpenLP -----------------------------------------------------------
    def _apply_openlp(self):
        host = self.host_var.get().strip() or "127.0.0.1"
        try:
            port = int(self.port_var.get().strip() or "4316")
        except ValueError:
            port = 4316
            self.port_var.set("4316")
        new = {"host": host, "port": port, "username": self.user_var.get(), "password": self.pass_var.get()}
        if new != self.settings.openlp:
            self.settings.openlp.update(new)
            self.settings.save()
            self.openlp_ok = None
            self._refresh_guidance()
        self.core.openlp.configure(host, port, new["username"], new["password"])

    def _test_openlp(self):
        self._apply_openlp()
        self.test_label.configure(text="Testing…", foreground=C_MUTED)
        self.core.test_openlp()

    def _toggle(self):
        self._auto_pending = False  # a manual click wins over the startup retry
        self._apply_openlp()
        self._safe(self.core.toggle)

    def _toggle_auto_on(self):
        self.settings.data["enable_on_launch"] = bool(self.auto_on_var.get())
        self.settings.save()
        self._log("The bridge will turn on automatically when the app starts."
                  if self.auto_on_var.get() else "Automatic start switched off.")

    def _send_test(self, role: str):
        self._apply_openlp()
        self.core.send_test(role)

    def _set_display_text(self, blank: bool):
        self.display_label.configure(
            text=f"Next blank press will: {'show the screen again' if blank else 'blank the screen'}")

    # ---- state / feedback -------------------------------------------------
    def _safe(self, fn):
        try:
            fn()
        except CaptureError as exc:
            self._log(str(exc))
            messagebox.showerror("ClickerBridge", str(exc))
        finally:
            self._refresh_state()
            self._refresh_mappings()

    def _refresh_state(self):
        on = self.core.enabled
        running = self.core.backend.is_running()
        self.dot.configure(fg=(C_HIT if (on and running) else C_WARN if running else C_DOT_OFF))
        device = self.settings.device_name or "no clicker chosen yet"
        if on and running:
            status = "Bridge ON - the clicker controls OpenLP"
        elif running:
            status = "Learning a button"
        elif "failed" in self.core.capture_status or "lost" in self.core.capture_status:
            reason = self.core.capture_status.replace("Off", "", 1).strip(" ()")
            status = f"Bridge off ({reason})"
        else:
            status = "Bridge off - the clicker works like a normal keyboard"
        self.status_label.configure(text=f"{status}   ·   {device}")
        self._draw_toggle()
        self._refresh_guidance()

    def _flash(self, role: str, color: str, ms: int = 350):
        badge = self.badges.get(role)
        if badge is None:
            return
        badge.configure(bg=color, fg="white")
        token = object()
        self._flash_tokens[role] = token

        def reset():
            if self._flash_tokens.get(role) is token:
                badge.configure(bg=C_BADGE_IDLE, fg=C_BADGE_FG)

        self.root.after(ms, reset)

    def _log(self, text: str):
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"{stamp}  {text}\n")
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 500:
            self.log_text.delete("1.0", f"{lines - 500}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ---- event loop -------------------------------------------------------
    def _poll(self):
        try:
            while True:
                self._dispatch(self.core.events.get_nowait())
        except queue.Empty:
            pass
        finally:
            self.root.after(30, self._poll)

    def _dispatch(self, event: tuple):
        kind, args = event[0], event[1:]
        if kind == "backend":
            try:
                self.core.handle_backend_event(*args)
            except CaptureError as exc:
                self._log(str(exc))
            self._refresh_state()
            self._refresh_mappings()
        elif kind == "state":
            self._refresh_state()
        elif kind == "log":
            self._log(str(args[0]))
        elif kind == "error":
            messagebox.showwarning("ClickerBridge", str(args[0]))
        elif kind == "learned":
            self._refresh_mappings()
            self._flash(args[0], C_HIT)
        elif kind == "pressed":
            self._flash(args[0], C_HIT)
        elif kind == "api_result":
            role, ok, text, target = args
            self.core.handle_api_result(role, ok, text, target)
            self.openlp_ok = bool(ok)
            if not ok:
                self._flash(role, C_ERR, 900)
            self._refresh_guidance()
        elif kind == "test_result":
            ok, text = args
            self.openlp_ok = bool(ok)
            self.test_label.configure(text=text, foreground=(C_ON if ok else "#c0392b"))
            self._log(text)
            self._refresh_guidance()
        elif kind == "display":
            self._set_display_text(bool(args[0]))
        elif kind == "mappings":
            self._refresh_mappings()

    # ---- shutdown ---------------------------------------------------------
    def on_close(self):
        try:
            self._apply_openlp()
            self.core.shutdown()
            self.settings.save()
        finally:
            self.root.destroy()
