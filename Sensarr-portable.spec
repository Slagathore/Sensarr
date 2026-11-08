# -*- mode: python ; coding: utf-8 -*-
# Single-file "portable" build: one Sensarr-portable.exe, nothing to unzip.
# Same contents as the folder build minus the anime database (it rebuilds
# itself on first launch). Slower to start than the folder build — onefile
# extracts to a temp dir each run — but the easiest thing to hand someone.

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

spec_file = globals().get("__file__")
project_dir = Path(spec_file).resolve().parent if spec_file else Path.cwd().resolve()

datas = []
datas += collect_data_files("sv_ttk")
# rank-torrent-name + its parser (distribution "parsett", IMPORT PACKAGE "PTT")
# carry regex/pattern data files the analysis misses. collect_data_files takes
# an IMPORT name, so "parsett" collected nothing and PTT/keywords/*.txt was
# left out of 1.6.0 — PTT's default adult-keyword handler reads those files on
# EVERY parse, so every release name raised FileNotFoundError and the selection
# engine rejected the entire world as "unparseable". Collect "PTT" by name.
datas += collect_data_files("RTN")
datas += collect_data_files("PTT")
datas += collect_data_files("parsett")
for _rf in ("download.mjs", "package.json", "package-lock.json", "diag.mjs"):
    _src = project_dir / "torrent_runner" / _rf
    if _src.is_file():
        datas.append((str(_src), "torrent_runner"))

hiddenimports = (
    collect_submodules("telegram")
    + collect_submodules("pystray")
    # rank-torrent-name (selection engine) + transitive deps.
    + collect_submodules("RTN")
    + collect_submodules("PTT")
    + collect_submodules("parsett")
    + collect_submodules("pydantic")
    # watchdog's observer backend is chosen by dynamic import at runtime; collect
    # every submodule so the folder watcher runs from the bundle (optional dep).
    + collect_submodules("watchdog")
    + ["pydantic_core", "orjson", "Levenshtein", "arrow", "pymediainfo"]
    + ["sv_ttk", "send2trash", "shows_tab", "shows_store", "show_tracker",
       "downloads_store", "download_manager", "torrent_search", "torrent_routing",
       "auth_store", "db", "ui_helpers", "health", "watchlist_tab", "video_quality",
       "subtitles", "anime_db", "media_identity", "library_watch"]
)

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
    a.binaries,
    a.datas,
    [],
    name="Sensarr-portable",
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
    # Start as the current user; explicit Run as administrator remains supported.
    uac_admin=False,
    icon="assets/sensarr.ico",
)
