# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build recipe for ClickerBridge.

    pip install pyinstaller
    pyinstaller --noconfirm clickerbridge.spec

Windows: dist/ClickerBridge.exe  (single file; interception.dll is bundled inside)
macOS:   dist/ClickerBridge.app  (zip it to distribute: ditto -c -k --keepParent dist/ClickerBridge.app ClickerBridge-macos.zip)
Linux:   dist/ClickerBridge      (single file)
"""
import sys
from pathlib import Path

here = Path(SPECPATH)
sys.path.insert(0, str(here))
from cb import __version__  # noqa: E402

datas = []
if sys.platform.startswith("win"):
    # The Interception user-mode library; the kernel driver itself is installed separately.
    datas.append((str(here / "vendor" / "interception"), "vendor/interception"))

a = Analysis(
    ["clickerbridge.py"],
    pathex=[str(here)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "cb.backends.win_interception",
        "cb.backends.mac_hidapi",
        "cb.backends.linux_evdev",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "unittest", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="ClickerBridge",
        debug=False,
        strip=False,
        upx=False,
        console=False,
        argv_emulation=False,
    )
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ClickerBridge")
    app = BUNDLE(
        coll,
        name="ClickerBridge.app",
        icon=None,
        bundle_identifier="dev.clickerbridge.app",
        info_plist={
            "CFBundleName": "ClickerBridge",
            "CFBundleDisplayName": "ClickerBridge",
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": True,  # always light: the UI uses fixed light colours
            "NSHumanReadableCopyright": "ClickerBridge",
        },
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="ClickerBridge",
        debug=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        icon=None,
    )
