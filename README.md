# ClickerBridge

ClickerBridge takes the buttons of **one** presentation clicker (Kingston, Kensington, Logitech,
any USB or Bluetooth presenter) and turns them into calls to **OpenLP's HTTP API**, so OpenLP
does not need to be the focused window. While the bridge is on, the clicker is captured
*exclusively*: its presses never reach any other application. Turn the bridge off and the
clicker is a normal keyboard again. Every other keyboard and mouse is left alone.

It runs on **Windows, macOS and Linux** from the same Python/tkinter code; only the
capture backend differs per OS.

The window is written for a volunteer who has never seen it: a short explanation at the
top, a "Next: …" line that always says what to do now, a tick on each step once it is done,
and a Help button with the full walkthrough.

```
ClickerBridge                                                                   [Help]
Control OpenLP's live slides with your presentation clicker, even when OpenLP is not in front.
Next: teach the forward and backward buttons in Step 2 (blank is optional).

Step 1 · Pick your clicker ✓   [ Kensington Presenter  [047D:2012 · SN 1A2B3C · keyboard] v ] [Refresh]
Step 2 · Teach it the buttons  Forward:   Page Down   [Capture forward click]        -> OpenLP next slide
                               Backward:  not set     [Capture backward click]       -> OpenLP previous slide
                               Blank:     not set     [Capture blank screen toggle]  -> OpenLP blank <-> show
Step 3 · Point it at OpenLP    Address [127.0.0.1]  Port [4316]  User [ ]  Password [ ]  [Test connection]
Step 4 · Turn the bridge on    [=============  BRIDGE IS OFF  ·  click to turn on  =============]
Check that it works            [ LEFT ]   [ RIGHT ]   [ BLANK ]   <- light up when a button is recognised
Log                            10:41:02  OK: Next slide (12 ms)
```

## How capture works on each OS

| OS | Backend | What it does | One-time setup |
|----|---------|--------------|----------------|
| Windows | [Interception](https://github.com/oblitum/Interception) kernel driver (what the original C++ version used) | Filters only the keyboard device whose hardware ID you picked; its strokes are consumed instead of forwarded | Install the driver once (admin) and reboot. `interception.dll` is bundled in `vendor/` |
| macOS | `hidapi` (shared IOKit open) + a `CGEventTap` | Reads the clicker's HID reports; the event tap drops key events that arrive while the clicker is sending (macOS only lets root open a keyboard exclusively). Untick *Block the clicker's keys* for shared mode | Grant **Input Monitoring** and **Accessibility** to the program that runs ClickerBridge |
| Linux | `evdev` (`EVIOCGRAB`) | Grabs the clicker's `/dev/input/event*` node(s) exclusively | Read access to `/dev/input` (the `input` group) |

Button identity is learned, not assumed: you press *Capture forward click* and then the
button on the clicker, and ClickerBridge stores exactly what the device sent (a scancode on
Windows, the raw HID report on macOS, the evdev key code on Linux). Media keys and odd
vendor reports work as well as Page Up/Down.

## Download a ready-made build

Every version tag produces a GitHub Release with `ClickerBridge-windows-x64.exe` and
`ClickerBridge-macos-arm64.zip` (Apple Silicon) on the repository's **Releases** page. They
contain Python and everything else, so nothing needs installing except the Interception
driver on Windows (see below).

* **Windows**: run the `.exe`. SmartScreen may warn because it is not code-signed: click
  *More info* → *Run anyway*.
* **macOS**: unzip, drag `ClickerBridge.app` to Applications. It is not notarized, so the
  first time right-click → *Open* (or run `xattr -d com.apple.quarantine /Applications/ClickerBridge.app`),
  then allow Input Monitoring when macOS asks, and add ClickerBridge under
  Privacy & Security → **Accessibility** (needed to block the clicker's keys in other apps).

## Install from source

Python 3.10 or newer with tkinter (the python.org installers include it; on Homebrew use
`brew install python-tk`; on Debian/Ubuntu `sudo apt install python3-tk`).

```bash
cd clickerbridge
python -m venv .venv
# Windows:        .venv\Scripts\activate
# macOS / Linux:  source .venv/bin/activate
pip install -r requirements.txt
python clickerbridge.py
```

`requirements.txt` installs `hidapi` on Windows/macOS, `evdev` on Linux. Everything else is the standard library.

### Windows

1. From the Interception download (`command line installer` folder) open an **Administrator**
   command prompt and run `install-interception.exe /install`, then **reboot**.
   (`/uninstall` removes it again.)
2. Start ClickerBridge. The dropdown lists every keyboard-class device the driver sees; the
   clicker shows with its name, `VID:PID`, serial number (when it has one) and hardware ID.

### macOS

1. Start ClickerBridge from a terminal: `python clickerbridge.py`.
2. The first time you turn the bridge on (or capture a button), macOS asks to allow
   **Input Monitoring** for the app that launched Python: *Terminal* or *iTerm*, or
   *ClickerBridge.app* if you packaged it (see below). Allow it, quit ClickerBridge and start
   it again. You can check later under System Settings → Privacy & Security → Input Monitoring.
   Blocking the clicker's keys in other apps also needs **Accessibility** for the same app.
3. The dropdown hides Apple's built-in keyboard and trackpad (vendor 05AC) unless you tick
   *Show all devices*. Never capture the built-in keyboard: you would lock yourself out
   until you click the big button with the mouse.

### Linux

`sudo usermod -aG input $USER`, log out and in again (or add a udev rule for the clicker).

## Using it

1. **Clicker**: pick it in the dropdown. *Refresh* re-scans after plugging it in.
2. **Buttons**: click *Capture forward click* and press the clicker's forward button; the
   RIGHT badge lights up and the button's name appears. Do the same for backward (LEFT) and
   the blank-screen button (BLANK). Capturing grabs the clicker only for that moment if the
   bridge is off. The ✕ clears a mapping.
3. **OpenLP**: in OpenLP enable the remote (Settings → Remote). Defaults are `127.0.0.1` and
   port `4316`; use the machine's address when OpenLP runs on another computer. User and
   password are only needed when OpenLP's *Require authentication* is on. *Test connection*
   asks OpenLP for its API version.
4. **The big button** turns the bridge on and off. On: every clicker press is swallowed,
   mapped ones are sent to OpenLP and the matching badge flashes green (red if OpenLP
   rejected the call; the log says why). Off: the clicker is released and works normally.
5. Clicking a badge sends that command by hand, which is handy to check the OpenLP side
   without the clicker.

OpenLP calls used: `POST /api/v2/controller/progress {"action":"next"|"previous"}` for the
slides and `POST /api/v2/core/display {"display":"blank"|"show"}` for the blank button. OpenLP
has no "toggle blank" endpoint, so ClickerBridge remembers what it last sent (shown under
the badges). If you blank or unblank inside OpenLP itself, the next clicker press may do the
opposite of what you expect: press again, or click *Reset*.

## Settings file

| OS | Location |
|----|----------|
| Windows | `%APPDATA%\ClickerBridge\settings.json` |
| macOS | `~/Library/Application Support/ClickerBridge/settings.json` |
| Linux | `~/.config/ClickerBridge/settings.json` |

Device and button mappings are stored per platform (`platforms.win32`, `platforms.darwin`,
`platforms.linux`) because each OS identifies buttons differently, so one file can move
between machines. Other keys: `openlp.{host,port,username,password}`, `enable_on_launch`
(turn the bridge on as soon as the app starts), `show_all_devices`.

`python clickerbridge.py --settings other.json` uses a different file;
`python clickerbridge.py --list-devices` prints what the capture backend sees.

## Troubleshooting

* **"The Interception driver is not installed"** (Windows): run the installer as
  Administrator and reboot. Both `keyboard.sys` and `mouse.sys` must be present in
  `C:\Windows\System32\drivers`.
* **"Could not open the clicker"** (macOS): Input Monitoring is missing for the
  launching app, or another tool (Karabiner-Elements, Hammerspoon) already holds the device.
  Grant the permission, quit and relaunch. These permissions are tied to the exact build, so
  after updating remove ClickerBridge from the list with **–** and add it again.
* **The clicker is not in the list**: it must present itself as a keyboard, keypad or
  consumer-control device (presenters always do). Plug it in, then *Refresh*. Devices that
  are only a mouse/pointer are not listed because they cannot be clickers.
* **A learned button stopped working**: some clickers send a different code in a different
  mode (e.g. after switching between "PowerPoint" and "Keynote" modes). Capture it again.
* **Only some buttons are captured**: a clicker can expose several HID interfaces. On macOS
  all keyboard-type interfaces of the device are opened; on Windows the driver filters the
  keyboard device(s) with that hardware ID. Laser/air-mouse functions are deliberately not
  touched.
* **Everything in the log but nothing happens in OpenLP**: use *Test connection*, check the
  remote port in OpenLP, and that the OpenLP computer's firewall allows port 4316 when it is
  a different machine.

## Starting automatically with the computer

Two things are needed: the app must turn the bridge on by itself, and the computer must
start the app at login.

1. In Step 4 tick **Turn the bridge on automatically whenever this app starts** (this sets
   `enable_on_launch` in the settings file). If the clicker is not plugged in yet when the
   app starts, it retries every 5 seconds until it is. The `--minimized` flag starts the
   window minimized.
2. Make the operating system launch it:

**Windows**: press Win+R, type `shell:startup`, Enter. In the folder that opens create a
shortcut (right-click → New → Shortcut) whose target is

```
"C:\path\to\clickerbridge\.venv\Scripts\pythonw.exe" "C:\path\to\clickerbridge\clickerbridge.py" --minimized
```

`pythonw.exe` runs without a console window (use the python.org `pythonw.exe` if you did
not create a venv). The Interception driver loads with Windows, nothing else is needed.
If you packaged the app with PyInstaller, point the shortcut at `ClickerBridge.exe` instead.

**macOS**: Login Items want an application, so package it once (see below) and move
`dist/ClickerBridge.app` to Applications. Start it, grant Input Monitoring when asked, then
System Settings → General → Login Items → **+** → ClickerBridge. Without packaging, make an
Automator *Application* with a single "Run Shell Script" action containing
`cd /path/to/clickerbridge && .venv/bin/python clickerbridge.py --minimized`, save it to
Applications, add that to Login Items and grant Input Monitoring to it when macOS asks.

**Linux**: create `~/.config/autostart/clickerbridge.desktop` with
`Exec=/path/to/clickerbridge/.venv/bin/python /path/to/clickerbridge/clickerbridge.py --minimized`.

## Building the standalone app

`clickerbridge.spec` is the PyInstaller recipe for all three platforms:

```bash
pip install pyinstaller
pyinstaller --noconfirm clickerbridge.spec
```

Windows gives `dist/ClickerBridge.exe` (single file, `interception.dll` inside), macOS gives
`dist/ClickerBridge.app` (grant Input Monitoring to it instead of Terminal), Linux a single
`dist/ClickerBridge` binary.

### Releases on GitHub (automatic builds)

`.github/workflows/release.yml` builds the Windows and macOS downloads on GitHub's runners
and attaches them to a Release. It is deliberately **not** triggered by commits: it runs
only when you push a version tag, or when you press *Run workflow* on the Actions tab.

```bash
# 1. bump __version__ in cb/__init__.py and commit
# 2. tag and push the tag
git tag v1.0.0
git push origin main --tags
```

A few minutes later the release `v1.0.0` appears with both files. Pushing commits to `main`
without a tag builds nothing, so the workflow cannot spam runs.

About minutes: on a **public** repository GitHub-hosted runners are free. On a **private**
repository every run draws on the plan's monthly allowance (2,000 minutes on the Free
plan), and macOS minutes count 10× while Windows minutes count 2×; one release takes about
4 minutes on each runner, so roughly 50 allowance-minutes per tag. Usage is visible under
Settings → Billing and plans → Usage.

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests run the bridge core against a fake capture backend and a fake OpenLP server
(including the token login flow), so they pass on any OS without hardware.

## Project layout

```
clickerbridge.py            entry point (GUI, --list-devices, --settings)
cb/settings.py              JSON settings, per-platform device + mappings
cb/openlp.py                OpenLP API v2 client (stdlib urllib, token login)
cb/bridge.py                core: capture lifecycle, learn mode, held-key tracking, OpenLP calls
cb/ui.py                    tkinter window
cb/backends/base.py         DeviceInfo, Backend interface, signature names
cb/backends/win_interception.py   Windows: Interception driver via ctypes
cb/backends/mac_hidapi.py         macOS: shared HID open via hidapi + key-blocking event tap
cb/backends/linux_evdev.py        Linux: evdev grab
vendor/interception/        interception.dll (x64, x86) + header from the Interception download
tests/test_core.py          unit tests
```

Third-party components: Interception by Francisco Lopes (driver free for non-commercial use,
see the licences in its download), hidapi, python-evdev.
