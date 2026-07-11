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
       "auth_store", "db", "ui_helpers", "health", "watchlist_tab", "video_quality",
       "subtitles", "anime_db"]
)


# Heavyweight packages from OTHER projects in the global site-packages that
# PyInstaller's analysis reaches via entry-point/plugin scanning (subliminal's
# stevedore loader etc.). None are Plexxarr dependencies — excluding them cuts
# minutes off the build and a lot of megabytes off the bundle.
_EXCLUDE_HEAVY = [
    "torch", "torchvision", "torchaudio", "torchao", "triton",
    "tensorflow", "tensorflow-plugins", "keras", "transformers", "timm",
    "sklearn", "scipy", "pandas", "matplotlib", "numpy", "numba", "llvmlite",
    "moviepy", "imageio", "imageio_ffmpeg", "librosa", "soundfile", "nltk",
    "onnxruntime", "cv2", "h5py", "boto3", "botocore", "duckdb", "sqlalchemy",
    "lxml", "openpyxl", "IPython", "jedi", "parso", "black", "blib2to3",
    "pytest", "_pytest", "py", "uvicorn", "websockets", "keyring", "fsspec",
    "lz4", "dns", "pythonnet", "clr_loader", "win32com",
]

a = Analysis(
    ["main.py"],
    pathex=[str(project_dir)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=_EXCLUDE_HEAVY,
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
    icon="assets/plexxarr.ico",
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
