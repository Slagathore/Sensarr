# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


spec_file = globals().get("__file__")
project_dir = Path(spec_file).resolve().parent if spec_file else Path.cwd().resolve()
datas = [(str(project_dir / "assets"), "assets")]
hiddenimports = (
    collect_submodules("telegram")
    + collect_submodules("pyautogui")
    + collect_submodules("pystray")
    + collect_submodules("mouseinfo")
    + collect_submodules("pyscreeze")
    + collect_submodules("pygetwindow")
)


a = Analysis(
    ["main.py"],
    pathex=[str(project_dir)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PlexResetButton",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PlexResetButton",
)
