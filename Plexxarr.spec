# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


spec_file = globals().get("__file__")
project_dir = Path(spec_file).resolve().parent if spec_file else Path.cwd().resolve()

datas = []
# sv-ttk ships its Sun Valley theme as Tcl data files; without collecting them
# the dark theme silently falls back to the stock gray ttk look in the EXE.
datas += collect_data_files("sv_ttk")
# The Node webtorrent downloader lives beside the app; ship the script +
# manifest so the Downloads pipeline works from the bundle. (node_modules is
# NOT bundled — run `npm install` in torrent_runner/ next to the EXE once, and
# Node.js must be on PATH.)
for _rf in ("download.mjs", "package.json", "package-lock.json", "diag.mjs"):
    _src = project_dir / "torrent_runner" / _rf
    if _src.is_file():
        datas.append((str(_src), "torrent_runner"))

hiddenimports = (
    collect_submodules("telegram")
    + collect_submodules("pystray")
    # New modules are imported dynamically enough that we pin them explicitly.
    + ["sv_ttk", "send2trash", "shows_tab", "shows_store", "show_tracker",
       "downloads_store", "download_manager", "torrent_search", "torrent_routing",
       "auth_store", "db", "ui_helpers", "health", "watchlist_tab", "video_quality", "subtitles"]
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
    name="Plexxarr",
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
    name="Plexxarr",
)
