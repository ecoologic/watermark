"""Resize PDFs and images, then apply a faint tiled text watermark."""

import argparse
import json
import math
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFont, ImageOps

DEFAULT_CONFIG = Path("/data/config.json")
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
JPEG_QUALITY = 85
TILE_ANGLE = 30
TEXT_GREY = 128


class ConfigError(Exception):
    """The config file is missing, unreadable or invalid."""


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class Config:
    size: int  # maximum long edge, in pixels
    watermark: str | None
    opacity: float = 0.15
    font_size: float = 0.04  # fraction of the image long edge

    @classmethod
    def load(cls, path: Path) -> "Config":
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            raise ConfigError(f"cannot read {path}: {error}") from error
        if not isinstance(raw, dict):
            raise ConfigError("the top level must be a JSON object")

        size = raw.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise ConfigError("'size' must be a positive integer")

        watermark = raw.get("watermark")
        if watermark is not None and not (isinstance(watermark, str) and watermark.strip()):
            raise ConfigError("'watermark' must be a non-empty string")

        opacity = raw.get("opacity", cls.opacity)
        if not _is_number(opacity) or not 0 < opacity <= 1:
            raise ConfigError("'opacity' must be a number above 0 and up to 1")

        font_size = raw.get("font_size", cls.font_size)
        if not _is_number(font_size) or not 0 < font_size <= 1:
            raise ConfigError("'font_size' must be a number above 0 and up to 1")

        return cls(size, watermark, opacity, font_size)


def fit(image: Image.Image, size: int) -> Image.Image:
    """Shrink in place so the long edge is at most `size`. Never upscales."""
    image.thumbnail((size, size), Image.Resampling.LANCZOS)
    return image


def save_jpeg(image: Image.Image, path: Path) -> None:
    image.convert("RGB").save(path, "JPEG", quality=JPEG_QUALITY)


def load_image(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return ImageOps.exif_transpose(image)


def render_pdf(path: Path, size: int) -> Iterator[Image.Image]:
    pdf = pdfium.PdfDocument(path)
    try:
        for page in pdf:
            width, height = page.get_size()
            bitmap = page.render(scale=size / max(width, height))
            # Rounding can add one pixel: fit() trims it.
            yield fit(bitmap.to_pil(), size)
    finally:
        pdf.close()


def pdf_to_jpgs(source: Path, size: int) -> list[Path]:
    targets = []
    for number, page in enumerate(render_pdf(source, size), start=1):
        target = source.with_name(f"{source.stem}-{number}.jpg")
        save_jpeg(page, target)
        targets.append(target)
    return targets


def tile_layer(text: str, extent: int, font: ImageFont.FreeTypeFont, alpha: int) -> Image.Image:
    """Transparent square filled with rows of `text`, every other row shifted."""
    layer = Image.new("RGBA", (extent, extent), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    text_width, text_height = right - left, bottom - top
    step_x = text_width + 2 * text_height
    step_y = 4 * text_height
    fill = (TEXT_GREY, TEXT_GREY, TEXT_GREY, alpha)
    for row, y in enumerate(range(0, extent, step_y)):
        shift = step_x // 2 if row % 2 else 0
        for x in range(-shift, extent, step_x):
            draw.text((x - left, y - top), text, font=font, fill=fill)
    return layer


def apply_watermark(image: Image.Image, text: str, opacity: float, font_ratio: float) -> Image.Image:
    base = image.convert("RGBA")
    width, height = base.size
    font = ImageFont.truetype(FONT_PATH, max(8, round(max(width, height) * font_ratio)))

    # A square as wide as the diagonal still covers the image after rotation.
    extent = math.ceil(math.hypot(width, height))
    layer = tile_layer(text, extent, font, round(255 * opacity))
    layer = layer.rotate(TILE_ANGLE, resample=Image.Resampling.BICUBIC)
    left, top = (extent - width) // 2, (extent - height) // 2
    layer = layer.crop((left, top, left + width, top + height))
    return Image.alpha_composite(base, layer)


def cmd_pdf2jpg(args: argparse.Namespace, config: Config) -> None:
    for target in pdf_to_jpgs(args.path, config.size):
        print(target)


def cmd_watermark(args: argparse.Namespace, config: Config) -> None:
    if config.watermark is None:
        raise ConfigError("'watermark' is missing")
    image = fit(load_image(args.path), config.size)
    marked = apply_watermark(image, config.watermark, config.opacity, config.font_size)
    target = args.path.with_name(f"{args.path.stem}-wm.jpg")
    save_jpeg(marked, target)
    print(target)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("path", type=Path, help="input file")
    common.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="config file (default: %(default)s)")

    parser = argparse.ArgumentParser(prog="wm", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "pdf2jpg", parents=[common], help="PDF to one resized JPG per page"
    ).set_defaults(run=cmd_pdf2jpg)
    commands.add_parser(
        "watermark", parents=[common], help="image to one resized, watermarked JPG"
    ).set_defaults(run=cmd_watermark)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.run(args, Config.load(args.config))
    except ConfigError as error:
        print(f"config error: {error}", file=sys.stderr)
        return 2
    except (OSError, pdfium.PdfiumError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
