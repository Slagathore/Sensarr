ASSETS FOLDER
=============

This folder holds the reference PNG images used for on-screen icon matching.
You need to capture four images before the bot will work correctly.

HOW TO CAPTURE IMAGES
---------------------
Use the Windows Snipping Tool (Win+Shift+S) or Greenshot.
Crop TIGHTLY around just the icon — a few pixels of padding is fine,
but do NOT include other icons or background in the crop.

IMPORTANT: Capture at your screen's actual DPI scaling.
  Check: Settings → Display → Scale
  If it says 125% or 150%, pyautogui is working at that resolution and
  your reference images must match it exactly.

FILES NEEDED
------------

1. tray_icon.png
   The Plex icon that appears in the Windows system tray (bottom-right,
   near the clock). If it's hidden, click the "^" expand arrow first.
   Target size: ~20x20 to 32x32 px.

2. taskbar_icon.png
   The Plex icon pinned to the taskbar (the main bar along the bottom).
   This is a different visual from the tray icon.
   Target size: ~40x40 px.

3. tray_expand_arrow.png
   The small up-arrow "^" chevron in the system tray that reveals hidden
   overflow icons. Usually in the bottom-right area of the taskbar.
   Target size: ~16x16 px.

4. exit_menu_item.png
   The "Exit Plex Media Server" option in the right-click context menu
   that appears when you right-click the Plex tray icon.
   Capture just that one menu row (text + any icon on its left).
   Target size: roughly 200x20 px.

TIPS
----
- If matching fails, try lowering the confidence values in .env
  (e.g. TRAY_ICON_CONFIDENCE=0.75).
- Recapture assets after a Plex version update if the icon changes.
- Run a quick test: open a Python shell and try:
    import pyautogui
    print(pyautogui.locateOnScreen('assets/tray_icon.png', confidence=0.85))
  It should print a Box(...) if the icon is currently on screen.
