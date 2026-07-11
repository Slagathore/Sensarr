# -*- mode: python ; coding: utf-8 -*-
# Single-file "portable" build: one Plexxarr-portable.exe, nothing to unzip.
# Same contents as the folder build minus the anime database (it rebuilds
# itself on first launch). Slower to start than the folder build — onefile
# extracts to a temp dir each run — but the easiest thing to hand someone.

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

spec_file = globals().get("__file__")
project_dir = Path(spec_file).resolve().parent if spec_file else Path.cwd().resolve()

datas = []
datas += collect_data_files("sv_ttk")
for _rf in ("download.mjs", "package.json", "package-lock.json", "diag.mjs"):
    _src = project_dir / "torrent_runner" / _rf
    if _src.is_file():
        datas.append((str(_src), "torrent_runner"))

hiddenimports = (
    collect_submodules("telegram")
    + collect_submodules("pystray")
    + ["sv_ttk", "send2trash", "shows_tab", "shows_store", "show_tracker",
       "downloads_store", "download_manager", "torrent_search", "torrent_routing",
       "auth_store", "db", "ui_helpers", "health", "watchlist_tab", "video_quality",
       "subtitles", "anime_db"]
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
    name="Plexxarr-portable",
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
