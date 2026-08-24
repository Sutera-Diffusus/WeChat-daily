from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "src-tauri" / "icons"


def logo(size: int = 1024) -> Image.Image:
    scale = size / 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    pts = lambda values: [(round(x * scale), round(y * scale)) for x, y in values]
    draw.polygon(pts([(10, 9), (56, 9), (56, 53), (29, 53), (17, 62), (17, 53), (10, 53)]), fill="#68a0f5")
    draw.polygon(pts([(4, 3), (51, 3), (51, 47), (24, 47), (12, 57), (12, 47), (4, 47)]), fill="#fffdf5")
    width = max(2, round(3 * scale))
    draw.line(pts([(5.5, 4.5), (49.5, 4.5), (49.5, 45.5), (22.8, 45.5), (13.5, 53), (13.5, 45.5), (5.5, 45.5), (5.5, 4.5)]), fill="#111111", width=width, joint="curve")
    draw.rectangle((round(11*scale), round(11*scale), round(43*scale), round(20*scale)), fill="#31df76", outline="#111111", width=max(1, round(2*scale)))
    draw.rectangle((round(15*scale), round(14*scale), round(35*scale), round(17*scale)), fill="#111111")
    draw.rectangle((round(11*scale), round(26*scale), round(24*scale), round(40*scale)), fill="#111111")
    draw.rectangle((round(15*scale), round(30*scale), round(20*scale), round(37*scale)), fill="#d5453f")
    for y, end in ((26, 43), (32, 43), (38, 38)):
        draw.rectangle((round(29*scale), round(y*scale), round(end*scale), round((y+3)*scale)), fill="#111111")
    return image


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    source = logo()
    for name, size in (("32x32.png", 32), ("128x128.png", 128), ("128x128@2x.png", 256), ("icon.png", 512)):
        source.resize((size, size), Image.Resampling.LANCZOS).save(OUT / name)
    source.save(OUT / "icon.ico", sizes=[(16,16), (24,24), (32,32), (48,48), (64,64), (128,128), (256,256)])
    print(f"Generated Wei Daily icons in {OUT}")


if __name__ == "__main__":
    main()
