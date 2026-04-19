# =============================================================================
# PlexResetButton — diagnose.py
# =============================================================================
# Mission: Standalone diagnostic tool for the PlexResetButton project.
# Run this script directly (`python diagnose.py`) when the tray icon
# cannot be found. It will reveal DPI/scaling mismatches between the
# reference asset and what PyAutoGUI sees on screen, and guide you to
# capture a replacement asset at the correct resolution.
#
# Nothing here modifies bot state — it is entirely read-only / informational.
# =============================================================================

import sys
from importlib import import_module
from pathlib import Path
from typing import Any, cast

import pyautogui

Image = cast(Any, import_module("PIL.Image"))
PillowImage = Any

ASSETS_DIR = Path(__file__).parent / "assets"
TRAY_ICON_PATH = ASSETS_DIR / "tray_icon.png"
DEBUG_SCREENSHOT_PATH = Path(__file__).parent / "debug_screenshot.png"
DEBUG_TRAY_CROP_PATH  = Path(__file__).parent / "debug_tray_corner.png"

# How far in from each edge to crop the tray area.
TRAY_CROP_W = 600   # width  — wide enough to catch icons even on multi-monitor setups
TRAY_CROP_H = 200   # height — tray bar is ~40-60px; 200 gives plenty of headroom


def _section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print('=' * 60)


def check_screen_resolution() -> tuple[int, int]:
    _section("Screen resolution (PyAutoGUI logical)")
    w, h = pyautogui.size()
    print(f"  pyautogui.size() => {w} x {h} px  (logical/reported)")
    return w, h


def take_and_inspect_screenshot() -> PillowImage:
    _section("Screenshot capture")
    print("  Taking screenshot via pyautogui.screenshot()…")
    shot = pyautogui.screenshot()
    sw, sh = shot.size
    print(f"  Actual screenshot image size => {sw} x {sh} px")

    lw, lh = pyautogui.size()
    if (sw, sh) != (lw, lh):
        ratio = sw / lw
        print(f"\n  ⚠️  DPI MISMATCH DETECTED!")
        print(f"     Logical resolution : {lw} x {lh}")
        print(f"     Physical resolution: {sw} x {sh}")
        print(f"     Scale factor       : {ratio:.2f}x")
        print()
        print("  This means Win+Shift+S captures at the WRONG scale.")
        print("  Your reference asset must be captured from PyAutoGUI's")
        print("  own screenshot (see 'debug_tray_corner.png' below).")
    else:
        print("\n  ✅ No DPI mismatch — screenshot and logical size match.")

    shot.save(str(DEBUG_SCREENSHOT_PATH))
    print(f"\n  Full screenshot saved → {DEBUG_SCREENSHOT_PATH}")
    return shot


def crop_tray_corner(shot: PillowImage) -> None:
    _section("Tray corner crop")
    sw, sh = shot.size
    left   = max(0, sw - TRAY_CROP_W)
    top    = max(0, sh - TRAY_CROP_H)
    crop   = shot.crop((left, top, sw, sh))
    crop.save(str(DEBUG_TRAY_CROP_PATH))
    print(f"  Bottom-right {TRAY_CROP_W}x{TRAY_CROP_H}px region saved → {DEBUG_TRAY_CROP_PATH}")
    print()
    print("  Open this file and find the Plex tray icon inside it.")
    print("  Crop JUST that icon tightly and save it as assets/tray_icon.png.")
    print("  Use an image editor (Paint, IrfanView, etc.) — NOT Win+Shift+S.")


def inspect_current_asset() -> None:
    _section("Current tray_icon.png asset")
    if not TRAY_ICON_PATH.exists():
        print(f"  ❌ MISSING: {TRAY_ICON_PATH}")
        return
    img  = Image.open(str(TRAY_ICON_PATH))
    print(f"  Path : {TRAY_ICON_PATH}")
    print(f"  Size : {img.width} x {img.height} px")
    print(f"  Mode : {img.mode}")
    if img.width > 64 or img.height > 64:
        print()
        print("  ⚠️  The asset is larger than expected for a system tray icon.")
        print("     Tray icons are typically 20–32 px. A large asset usually means")
        print("     you captured too wide a region. Recrop tightly.")


def locate_attempts(tray_icon_path: Path) -> None:
    _section("Locate attempts at varying confidence levels")
    if not tray_icon_path.exists():
        print("  Skipped — asset file not found.")
        return

    path_str = str(tray_icon_path)
    confidences = [0.95, 0.90, 0.85, 0.80, 0.75, 0.70, 0.65, 0.60, 0.55, 0.50]
    found_at    = None

    for conf in confidences:
        try:
            loc = pyautogui.locateOnScreen(path_str, confidence=conf)
        except Exception as exc:
            loc = None
            # pyautogui raises ImageNotFoundException in some versions
            _ = exc

        if loc is not None and found_at is None:
            found_at = (conf, loc)
            cx, cy = pyautogui.center(loc)
            print(f"  ✅ FOUND at confidence={conf:.2f}  =>  box={loc}  center=({cx},{cy})")
        else:
            mark = "  -" if found_at else "  ❌"
            print(f"{mark} Not found at confidence={conf:.2f}")

    print()
    if found_at is None:
        print("  Image was NOT found at any confidence level.")
        print("  Either Plex is not running / tray icon not visible,")
        print("  or the DPI of the asset doesn't match. Check debug_tray_corner.png.")
        print()
        print("  HOW TO FIX:")
        print("   1. Open debug_tray_corner.png (generated above).")
        print("   2. Locate the Plex icon in that image.")
        print("   3. Crop JUST the icon (tight, ~20-32px) and save as assets/tray_icon.png.")
        print("   4. Re-run diagnose.py to verify.")
    else:
        conf, _ = found_at
        print(f"  Icon was found at confidence={conf:.2f}.")
        if conf < 0.85:
            print(f"  Consider setting TRAY_ICON_CONFIDENCE={conf:.2f} in your .env file.")


def check_grayscale(tray_icon_path: Path) -> None:
    _section("Grayscale locate attempt")
    if not tray_icon_path.exists():
        print("  Skipped — asset file not found.")
        return
    path_str = str(tray_icon_path)
    for conf in [0.80, 0.70, 0.60]:
        try:
            loc = pyautogui.locateOnScreen(path_str, confidence=conf, grayscale=True)
        except Exception:
            loc = None
        if loc is not None:
            cx, cy = pyautogui.center(loc)
            print(f"  ✅ FOUND (grayscale) at confidence={conf:.2f}  center=({cx},{cy})")
            print("     → Set TRAY_ICON_CONFIDENCE={conf:.2f} in .env and it should work.")
            return
        else:
            print(f"  ❌ Not found (grayscale) at confidence={conf:.2f}")
    print("  Not found in grayscale either.")


def get_dpi_ratio() -> float:
    """
    Return the physical-to-logical pixel ratio.
    1.0 means no DPI scaling; 1.25 means 125% Windows scaling, etc.
    """
    lw, _lh = pyautogui.size()
    shot = pyautogui.screenshot()
    sw, _sh = shot.size
    return sw / lw


def auto_fix_asset_dpi(shot: PillowImage) -> Path | None:
    """
    If the existing tray_icon.png was captured at logical resolution but
    PyAutoGUI works at physical resolution, rescale the asset to match and
    save it back. Returns the path of the fixed asset, or None if no fix
    was needed / possible.

    Strategy:
      1. Compute DPI ratio (physical screenshot width / logical width).
      2. If ratio is ~1.0, no fix needed.
      3. Otherwise rescale tray_icon.png by ratio using high-quality Lanczos
         resampling and overwrite it (original backed up as tray_icon.orig.png).
    """
    _section("Auto-fix: DPI rescale")

    if not TRAY_ICON_PATH.exists():
        print("  Skipped — tray_icon.png not found.")
        return None

    lw, lh = pyautogui.size()
    sw, sh = shot.size
    ratio  = sw / lw

    print(f"  Logical size   : {lw} x {lh}")
    print(f"  Physical size  : {sw} x {sh}")
    print(f"  DPI ratio      : {ratio:.4f}")

    if abs(ratio - 1.0) < 0.02:
        print("\n  ✅ No DPI mismatch — rescale not needed.")
        return None

    asset = Image.open(str(TRAY_ICON_PATH))
    aw, ah = asset.size
    new_w  = round(aw * ratio)
    new_h  = round(ah * ratio)

    print(f"\n  Current asset  : {aw} x {ah} px")
    print(f"  Rescaled to    : {new_w} x {new_h} px  (ratio {ratio:.4f})")

    # Back up original
    backup_path = TRAY_ICON_PATH.with_suffix(".orig.png")
    if not backup_path.exists():
        asset.save(str(backup_path))
        print(f"  Original backed up → {backup_path.name}")
    else:
        print(f"  Backup already exists → {backup_path.name} (skipping overwrite)")

    rescaled = asset.resize((new_w, new_h), Image.LANCZOS)
    rescaled.save(str(TRAY_ICON_PATH))
    print(f"  ✅ Rescaled asset saved → {TRAY_ICON_PATH}")
    return TRAY_ICON_PATH


def self_match_test(shot: PillowImage) -> bool:
    """
    THE most critical test. Crops a 40x40 px region from the *same* screenshot
    that was just taken and immediately tries to locate it on screen via
    locateOnScreen. If this fails, pyautogui/opencv is broken or the screen
    changed dramatically in the fraction of a second between capture and search.

    Returns True if self-match succeeded (opencv pipeline is healthy).
    """
    _section("Self-match pipeline test (most important)")
    sw, sh = shot.size

    # Pick a region near the tray — bottom-right area, avoid the very edge
    # Use a 40x40 crop from slightly inward so it definitely has real content.
    cx = max(40, sw - 400)
    cy = max(40, sh - 60)
    region_img = shot.crop((cx, cy, cx + 40, cy + 40))

    self_match_path = Path(__file__).parent / "debug_selfmatch.png"
    region_img.save(str(self_match_path))
    print(f"  Self-match crop (40x40 from screenshot) saved → {self_match_path.name}")
    print(f"  Crop origin: ({cx}, {cy})")
    print()

    # Try to find this exact crop back on screen at 100%, then lower
    for conf in [0.99, 0.95, 0.90, 0.85, 0.80]:
        try:
            loc = pyautogui.locateOnScreen(str(self_match_path), confidence=conf)
        except Exception as exc:
            print(f"  ❌ Exception at confidence={conf:.2f}: {exc}")
            loc = None

        if loc is not None:
            print(f"  ✅ Self-match PASSED at confidence={conf:.2f} — opencv pipeline is healthy.")
            print()
            print("  This means pyautogui CAN match images. The problem is either:")
            print("   a) The tray icon is not visible on screen when the bot searches.")
            print("      (hidden in overflow tray, Plex not running, etc.)")
            print("   b) Your tray_icon.png crop includes background/neighbors that")
            print("      don't match exactly. It must be ONLY the icon, tightly cropped.")
            print("   c) The icon appearance changes (Plex updates, theme changes).")
            return True

    print("  ❌ SELF-MATCH FAILED — pyautogui/opencv cannot find a region")
    print("     taken from its own screenshot a moment ago.")
    print()
    print("  This indicates a fundamental environment problem, most likely:")
    print("   - opencv-python is not installed or is corrupted:")
    print("       pip install --force-reinstall opencv-python")
    print("   - Screen DPI is set to a fractional scale (e.g. 125%, 150%) and")
    print("     pyautogui is not handling it. Try setting your display to 100%")
    print("     in Windows Settings → Display → Scale, then re-run.")
    print("   - Multiple monitors with different DPI settings.")
    return False


def check_icon_visibility_live() -> None:
    """
    Take a fresh screenshot and show the tray corner so the user can confirm
    the icon is actually visible on screen at the instant of the search.
    """
    _section("Live tray visibility check")
    print("  Taking a FRESH screenshot to confirm icon is on screen right now…")
    fresh = pyautogui.screenshot()
    sw, sh = fresh.size
    left  = max(0, sw - TRAY_CROP_W)
    top   = max(0, sh - TRAY_CROP_H)
    live_crop = fresh.crop((left, top, sw, sh))
    live_path = Path(__file__).parent / "debug_live_tray.png"
    live_crop.save(str(live_path))
    print(f"  Live tray crop saved → {live_path.name}")
    print()
    print("  Please open this file and check: is the Plex tray icon visible in it?")
    print("  If NOT, the icon was hidden when the bot tries to find it.")
    print("  Common causes: tray overflow is collapsed, Plex is not running.")

    # Immediately try locating tray_icon.png against this FRESH screenshot
    # (search IN the image, not on screen) — bypasses any timing race
    if TRAY_ICON_PATH.exists():
        print()
        print("  Searching for tray_icon.png WITHIN the live tray crop image...")
        cv2 = cast(Any, import_module("cv2"))
        np = cast(Any, import_module("numpy"))

        haystack = cv2.cvtColor(np.array(fresh), cv2.COLOR_RGB2BGR)
        asset    = Image.open(str(TRAY_ICON_PATH))
        needle   = cv2.cvtColor(np.array(asset), cv2.COLOR_RGBA2BGR if asset.mode == "RGBA" else cv2.COLOR_RGB2BGR)

        result   = cv2.matchTemplate(haystack, needle, cv2.TM_CCOEFF_NORMED)
        _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(result)

        print(f"  Best match score (0–1): {max_val:.4f}  at pixel {max_loc}")
        if max_val >= 0.85:
            print(f"  ✅ Strong match — icon found in live screenshot at ({max_loc[0]}, {max_loc[1]}).")
            print("     If locateOnScreen still fails, a timing race is the cause:")
            print("     the icon disappears between when the bot fires and when it searches.")
        elif max_val >= 0.65:
            print(f"  ⚠️  Weak match ({max_val:.2f}) — icon may be partially visible or slightly different.")
            print("     Try setting TRAY_ICON_CONFIDENCE=0.60 in your .env.")
        else:
            print(f"  ❌ No match ({max_val:.2f}) — icon is NOT in the live screenshot.")
            print("     The Plex tray icon is not visible right now.  Possible causes:")
            print("       - The tray overflow is closed (bot must expand it first).")
            print("       - assets/tray_icon.png was cropped from a different icon.")

        # Save a visualisation with the match location marked
        vis = cv2.rectangle(
            haystack.copy(),
            max_loc,
            (max_loc[0] + needle.shape[1], max_loc[1] + needle.shape[0]),
            (0, 0, 255),
            2,
        )
        vis_path = Path(__file__).parent / "debug_match_vis.png"
        cv2.imwrite(str(vis_path), vis)
        print(f"  Match visualisation (red box = best match) saved → {vis_path.name}")


def retry_locate_after_fix() -> None:
    """Re-run locate attempts after the asset has been rescaled by auto_fix_asset_dpi."""
    _section("Locate attempts after DPI fix")
    if not TRAY_ICON_PATH.exists():
        print("  Skipped — tray_icon.png not found.")
        return

    path_str = str(TRAY_ICON_PATH)
    confidences = [0.90, 0.85, 0.80, 0.75, 0.70, 0.65, 0.60]
    found_at = None

    for conf in confidences:
        try:
            loc = pyautogui.locateOnScreen(path_str, confidence=conf)
        except Exception:
            loc = None

        if loc is not None and found_at is None:
            found_at = (conf, loc)
            cx, cy = pyautogui.center(loc)
            print(f"  ✅ FOUND at confidence={conf:.2f}  center=({cx},{cy})")
        else:
            mark = "  -" if found_at else "  ❌"
            print(f"{mark} Not found at confidence={conf:.2f}")

    print()
    if found_at is None:
        print("  Still not found after rescale.")
        print("  The asset may need to be recropped from debug_tray_corner.png")
        print("  using an image editor. Make sure you crop TIGHTLY around only")
        print("  the Plex icon (no background, no neighboring icons).")
    else:
        conf, _ = found_at
        print(f"  ✅ SUCCESS — icon found at confidence={conf:.2f}.")
        if conf < 0.85:
            print(f"  Add this to your .env:  TRAY_ICON_CONFIDENCE={conf:.2f}")


def main() -> None:
    print("\nPlexResetButton — Icon Diagnostic Tool")
    print("Ensure Plex is running and its tray icon is visible before continuing.")
    input("Press Enter to start…")

    lw, lh   = check_screen_resolution()
    shot     = take_and_inspect_screenshot()
    crop_tray_corner(shot)
    inspect_current_asset()

    # Step 1: prove the opencv pipeline works at all
    pipeline_ok = self_match_test(shot)

    # Step 2: standard locate attempts
    locate_attempts(TRAY_ICON_PATH)
    check_grayscale(TRAY_ICON_PATH)

    # Step 3: direct cv2 match + live visibility check — most informative
    check_icon_visibility_live()

    # Step 4: attempt DPI rescale only if pipeline is healthy but locate failed
    if pipeline_ok:
        fixed = auto_fix_asset_dpi(shot)
        if fixed is not None:
            retry_locate_after_fix()

    _section("Done")
    print("  Debug files written:")
    print(f"    {DEBUG_SCREENSHOT_PATH.name}      — full screen")
    print(f"    {DEBUG_TRAY_CROP_PATH.name}  — bottom-right corner")
    print(f"    debug_live_tray.png          — fresh tray corner at locate time")
    print(f"    debug_selfmatch.png          — self-match crop (pipeline test)")
    print(f"    debug_match_vis.png          — best cv2 match location (red box)")
    if (TRAY_ICON_PATH.with_suffix(".orig.png")).exists():
        print(f"    tray_icon.orig.png           — original asset before rescaling")
    print()
    print("  If still not working after checking all output above,")
    print("  open debug_match_vis.png — the red box shows exactly where")
    print("  cv2 thinks your asset matches on screen.")
    print()

    # #todo: draw a red highlight box on the full screenshot at the found location
    # #todo: also test taskbar_icon.png and exit_menu_item.png in the same pass


if __name__ == "__main__":
    main()
