"""make_icons.py
Draws Koli's mark (a "K" breathing out a sound wave, in a circle) as the
Windows tray icons for each state and the app's .ico file, in JumboMail's
colours. Run it after changing the mark or the colours."""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'src' / 'whisper_key' / 'platform' / 'windows' / 'assets'

TURQUOISE = (21, 180, 212, 255)   # idle: JumboMail's brand colour
RED = (224, 63, 63, 255)          # recording
SUN = (254, 187, 41, 255)         # processing
SLATE = (14, 42, 51, 255)         # the K, dark on every circle

SIZE = 512  # drawn large, then scaled down so the edges stay smooth


# The mark on a 48-unit grid, as in the app (brand.js): a stem, the two arms
# of the K, and a small arc for the sound wave.
def draw_mark(fill, with_wave=True) -> Image.Image:
    image = Image.new('RGBA', (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((0, 0, SIZE - 1, SIZE - 1), fill=fill)
    unit = SIZE / 48
    width = int(5.2 * unit)

    def pt(x, y):
        return (x * unit, y * unit)

    def round_cap(x, y):
        r = width / 2
        cx, cy = pt(x, y)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=SLATE)

    draw.line([pt(16, 12), pt(16, 36)], fill=SLATE, width=width)
    draw.line([pt(32, 13), pt(22, 24), pt(32, 35)], fill=SLATE, width=width, joint='curve')
    for x, y in ((16, 12), (16, 36), (32, 13), (32, 35), (22, 24)):
        round_cap(x, y)
    if with_wave:
        # The same cubic curve as the SVG: M37.5 19.5 c2 2.6 2 6.4 0 9
        p0, p1, p2, p3 = (37.5, 19.5), (39.5, 22.1), (39.5, 25.9), (37.5, 28.5)
        points = []
        for i in range(201):
            t = i / 200
            x = (1 - t) ** 3 * p0[0] + 3 * (1 - t) ** 2 * t * p1[0] + 3 * (1 - t) * t ** 2 * p2[0] + t ** 3 * p3[0]
            y = (1 - t) ** 3 * p0[1] + 3 * (1 - t) ** 2 * t * p1[1] + 3 * (1 - t) * t ** 2 * p2[1] + t ** 3 * p3[1]
            points.append(pt(x, y))
        # A dense chain of round dots: a smooth, round-capped stroke.
        r = 4.2 * unit / 2
        for cx, cy in points:
            draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=SLATE)
    return image


def main():
    for name, colour in (('tray_idle.png', TURQUOISE), ('tray_recording.png', RED), ('tray_processing.png', SUN)):
        # The wave is dropped at tray size, where it would only blur.
        draw_mark(colour, with_wave=False).resize((32, 32), Image.LANCZOS).save(ASSETS / name)
    big = draw_mark(TURQUOISE)
    big.save(ASSETS / 'whisperkey-icon.ico', sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"Wrote tray icons and the app icon to {ASSETS.relative_to(ROOT)}")


if __name__ == '__main__':
    main()
