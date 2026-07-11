# =============================================================================
# app_icon.py
# =============================================================================
# The one Plexxarr mark, drawn in code so the tray icon, the window/taskbar
# icon, and the generated .ico can never drift apart: Plex-orange rounded
# square, white play triangle, double-chevron ">>" tail (the "arr").
# Run this file directly to regenerate assets/plexxarr.ico for the EXE build.
# =============================================================================

from PIL import Image, ImageDraw

_BG = (229, 160, 13, 255)      # plex amber
_FG = (24, 28, 36, 255)        # near-black marks


def icon_image(size: int = 64) -> Image.Image:
    """Draw the mark at any square size (crisp at 16px and 256px)."""
    s = size / 64.0  # design on a 64-grid, scale everything
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2 * s, 2 * s, 62 * s, 62 * s),
                        radius=14 * s, fill=_BG)
    # play triangle, weighted left
    d.polygon([(18 * s, 16 * s), (18 * s, 48 * s), (40 * s, 32 * s)], fill=_FG)
    # the "arr": two chevrons continuing the motion
    for x0 in (38, 48):
        d.polygon([
            (x0 * s, 20 * s), (x0 * s + 8 * s, 32 * s), (x0 * s, 44 * s),
            (x0 * s + 4 * s, 44 * s), (x0 * s + 12 * s, 32 * s),
            (x0 * s + 4 * s, 20 * s),
        ], fill=_FG)
    return img


def write_ico(dest: str = "assets/plexxarr.ico") -> str:
    from pathlib import Path
    path = Path(__file__).parent / dest
    path.parent.mkdir(parents=True, exist_ok=True)
    base = icon_image(256)
    base.save(path, format="ICO",
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                     (64, 64), (128, 128), (256, 256)])
    return str(path)


if __name__ == "__main__":
    print("wrote", write_ico())
