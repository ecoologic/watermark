"""Resize PDFs and images, then apply a faint tiled text watermark."""

import argparse
import json
import math
import re
import sys
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

import pypdfium2 as pdfium
from PIL import Image, ImageDraw, ImageFont, ImageOps

DEFAULT_CONFIG = Path("/data/config.json")
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
JPEG_QUALITY = 85
TILE_ANGLE = 30
TEXT_GREY = 128
EXIF_DATE_TIME = 0x0132
XMP_TEMPLATE = (
    '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    '<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:xmp="http://ns.adobe.com/xap/1.0/" '
    'xmp:ModifyDate="{date}"><dc:description><rdf:Alt><rdf:li xml:lang="x-default">{text}</rdf:li></rdf:Alt>'
    '</dc:description></rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
)


class ConfigError(Exception):
    """The config file is missing, unreadable or invalid."""


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class Config:
    size: int  # maximum long edge, in pixels
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

        opacity = raw.get("opacity", cls.opacity)
        if not _is_number(opacity) or not 0 < opacity <= 1:
            raise ConfigError("'opacity' must be a number above 0 and up to 1")

        font_size = raw.get("font_size", cls.font_size)
        if not _is_number(font_size) or not 0 < font_size <= 1:
            raise ConfigError("'font_size' must be a number above 0 and up to 1")

        return cls(size, opacity, font_size)


def fit(image: Image.Image, size: int) -> Image.Image:
    """Shrink in place so the long edge is at most `size`. Never upscales."""
    image.thumbnail((size, size), Image.Resampling.LANCZOS)
    return image


def save_jpeg(image: Image.Image, path: Path, **metadata: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(path, "JPEG", quality=JPEG_QUALITY, **metadata)


def watermark_metadata(text: str, when: datetime) -> dict[str, bytes]:
    """EXIF holds the date. XMP holds the date and the exact text, which EXIF cannot store in Unicode."""
    exif = Image.Exif()
    exif[EXIF_DATE_TIME] = when.strftime("%Y:%m:%d %H:%M:%S")
    xmp = XMP_TEMPLATE.format(text=escape(text), date=when.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return {"exif": exif.tobytes(), "xmp": xmp.encode()}


def normalise(text: str) -> str:
    """Lowercase ASCII words joined by single dashes: 'Café – 2026' -> 'cafe-2026'."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")


def output_path(source: Path, output_dir: Path | None, watermark: str | None = None, page: int | None = None) -> Path:
    """`<stem>-<watermark>-<extension>[-<page>].jpg`, in `output_dir` or next to `source`."""
    parts = [normalise(source.stem), watermark and normalise(watermark), normalise(source.suffix), page]
    name = "-".join(str(part) for part in parts if part)
    return (output_dir or source.parent) / f"{name}.jpg"


def load_image(path: Path) -> Image.Image:
    with Image.open(path) as image:
        return ImageOps.exif_transpose(image)


def render_pdf(path: Path, size: int) -> Iterator[tuple[int | None, Image.Image]]:
    """Yield `(page number, image)`. The number is None for a one-page PDF."""
    pdf = pdfium.PdfDocument(path)
    try:
        for number, page in enumerate(pdf, start=1):
            width, height = page.get_size()
            bitmap = page.render(scale=size / max(width, height))
            # Rounding can add one pixel: fit() trims it.
            yield (number if len(pdf) > 1 else None), fit(bitmap.to_pil(), size)
    finally:
        pdf.close()


def pages(source: Path, size: int) -> Iterator[tuple[int | None, Image.Image]]:
    """The pages of a PDF, or the single image of any other file."""
    if source.suffix.lower() == ".pdf":
        yield from render_pdf(source, size)
    else:
        yield None, fit(load_image(source), size)


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
    for page, image in render_pdf(args.path, config.size):
        target = output_path(args.path, args.output, page=page)
        save_jpeg(image, target)
        print(target)


def cmd_watermark(args: argparse.Namespace, config: Config) -> None:
    metadata = watermark_metadata(args.watermark, datetime.now(timezone.utc))
    for page, image in pages(args.path, config.size):
        marked = apply_watermark(image, args.watermark, config.opacity, config.font_size)
        target = output_path(args.path, args.output, args.watermark, page)
        save_jpeg(marked, target, **metadata)
        print(target)


def watermark_text(value: str) -> str:
    if not normalise(value):
        raise argparse.ArgumentTypeError("needs at least one letter or digit")
    return value


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("path", type=Path, help="input file")
    common.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="config file (default: %(default)s)")
    common.add_argument("--output", type=Path, help="output folder (default: next to the input file)")

    parser = argparse.ArgumentParser(prog="watermark", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "pdf2jpg", parents=[common], help="PDF to one resized JPG per page"
    ).set_defaults(run=cmd_pdf2jpg)
    mark = commands.add_parser(
        "watermark", parents=[common], help="PDF or image to resized, watermarked JPGs"
    )
    mark.add_argument("--watermark", type=watermark_text, required=True, help="the watermark text")
    mark.set_defaults(run=cmd_watermark)
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
