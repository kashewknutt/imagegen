from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def side_by_side(original: Image.Image, gen1: Image.Image, gen2: Image.Image, out_path: Path, labels: tuple[str, str, str] = ("Original", "Prompt 1", "Prompt 2")) -> None:
    imgs = [original, gen1, gen2]
    widths = [im.width for im in imgs]
    heights = [im.height for im in imgs]
    max_h = max(heights)
    total_w = sum(widths)

    canvas = Image.new("RGB", (total_w, max_h + 40), (20, 20, 20))
    x = 0
    for im, label in zip(imgs, labels):
        canvas.paste(im, (x, 40))
        x += im.width

    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    x = 0
    for im, label in zip(imgs, labels):
        draw.text((x + 8, 10), label, fill=(240, 240, 240), font=font)
        x += im.width

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
