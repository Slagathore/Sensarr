import logging
import time

import pyautogui

import config

logger = logging.getLogger(__name__)


def _locate(image_path: str, confidence: float, region=None, grayscale: bool = False):
    """
    Single attempt to locate an image on screen.
    Returns a Box (left, top, width, height) or None.
    Handles both the return-None and raise-ImageNotFoundException styles.
    """
    try:
        return pyautogui.locateOnScreen(
            image_path,
            confidence=confidence,
            region=region,
            grayscale=grayscale,
        )
    except Exception:
        return None


def find_icon(
    image_path: str,
    confidence: float = 0.85,
    timeout: float = 5.0,
    region=None,
    grayscale: bool = False,
):
    """
    Search for an icon on screen, polling until found or timeout.
    Returns (x, y) center tuple or None.
    """
    start = time.time()
    while time.time() - start < timeout:
        loc = _locate(image_path, confidence, region=region, grayscale=grayscale)
        if loc is not None:
            cx, cy = pyautogui.center(loc)
            logger.debug("Found %s at (%d, %d)", image_path, cx, cy)
            return (cx, cy)
        time.sleep(0.3)
    return None


def expand_system_tray() -> bool:
    """
    Click the Windows 11 system tray overflow expand arrow so hidden tray
    icons become visible.  Tries image match first, falls back to an
    approximate screen coordinate.
    """
    pos = find_icon(config.TRAY_EXPAND_ARROW_PATH, confidence=0.80, timeout=3.0)
    if pos is None:
        pos = find_icon(
            config.TRAY_EXPAND_ARROW_PATH,
            confidence=0.75,
            timeout=2.0,
            grayscale=True,
        )

    if pos is not None:
        pyautogui.click(*pos)
    else:
        logger.warning(
            "Could not find tray expand arrow by image — trying coordinate fallback."
        )
        screen_w, screen_h = pyautogui.size()
        # The expand chevron is typically just left of the clock/notification area.
        # This coordinate is approximate; adjust TRAY_EXPAND_ARROW_PATH asset if it misses.
        pyautogui.click(screen_w - 320, screen_h - 15)

    time.sleep(0.5)
    return True


def find_plex_tray_icon():
    """
    Find the Plex system tray icon.
    Strategy: direct search → (expand tray if asset exists) + retry → grayscale fallback.
    Returns (x, y) or None.
    """
    import os

    # 1. Direct search — icon may already be visible
    pos = find_icon(config.TRAY_ICON_PATH, confidence=config.TRAY_ICON_CONFIDENCE, timeout=3.0)
    if pos:
        return pos

    # 2. Expand overflow tray, then search again — only if the expand arrow asset exists
    if os.path.exists(config.TRAY_EXPAND_ARROW_PATH):
        logger.info("Plex tray icon not visible directly — expanding system tray.")
        expand_system_tray()
        pos = find_icon(config.TRAY_ICON_PATH, confidence=config.TRAY_ICON_CONFIDENCE, timeout=4.0)
        if pos:
            return pos

    # 3. Grayscale fallback
    pos = find_icon(config.TRAY_ICON_PATH, confidence=0.75, timeout=3.0, grayscale=True)
    return pos


def find_plex_taskbar_icon():
    """
    Find the Plex pinned taskbar icon.
    Searches the bottom strip of the screen first (faster), then full screen.
    Returns (x, y) or None.
    """
    screen_w, screen_h = pyautogui.size()
    taskbar_region = (0, screen_h - 80, screen_w, 80)

    # 1. Bottom strip only
    pos = find_icon(
        config.TASKBAR_ICON_PATH,
        confidence=config.TASKBAR_ICON_CONFIDENCE,
        timeout=4.0,
        region=taskbar_region,
    )
    if pos:
        return pos

    # 2. Full screen
    pos = find_icon(
        config.TASKBAR_ICON_PATH,
        confidence=config.TASKBAR_ICON_CONFIDENCE,
        timeout=3.0,
    )
    if pos:
        return pos

    # 3. Grayscale fallback
    return find_icon(config.TASKBAR_ICON_PATH, confidence=0.75, timeout=3.0, grayscale=True)


def find_exit_menu_item():
    """
    Find the 'Exit Plex Media Server' right-click context menu item.
    Call this shortly after right-clicking the tray icon.
    Returns (x, y) or None.
    """
    pos = find_icon(config.EXIT_MENU_ITEM_PATH, confidence=0.80, timeout=4.0)
    if pos:
        return pos
    return find_icon(config.EXIT_MENU_ITEM_PATH, confidence=0.75, timeout=2.0, grayscale=True)


def plex_tray_icon_gone() -> bool:
    """Returns True if the Plex tray icon is no longer visible on screen."""
    pos = find_icon(config.TRAY_ICON_PATH, confidence=config.TRAY_ICON_CONFIDENCE, timeout=1.5)
    if pos:
        return False
    pos = find_icon(config.TRAY_ICON_PATH, confidence=0.75, timeout=1.0, grayscale=True)
    return pos is None
