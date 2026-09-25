"""Tesseract wrapper: image in, positioned words out."""

from __future__ import annotations

import io
import subprocess
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageChops, ImageFilter, ImageOps

from config import INSTALL_AUTO_OCR_DEEP_LANGS
from logger.logger import log

from .matcher import Word

OCR_TIMEOUT = 30


def upscale_factor(width: int, height: int) -> int:
    """Installer text is ~9pt and tesseract misreads it below ~4x, so small
    crops (a lone dialog) get more magnification than a full screen."""
    longest = max(width, height)
    if longest <= 700:
        return 4
    if longest <= 1100:
        return 3
    return 2


LIGHT_TEXT_THRESHOLD = 170


def ocr_words(
    image: Image.Image,
    langs: str = INSTALL_AUTO_OCR_DEEP_LANGS,
    light_text: bool = False,
) -> list[Word]:
    """OCR ``image`` and return words in the image's own pixel coordinates.

    ``light_text`` keeps only bright pixels (as black on white), which reads
    the white labels of skinned installers drawn over pictures or colored
    tiles that the plain pass misses."""
    gray = image.convert("L")
    if light_text:
        gray = gray.point(lambda p: 0 if p > LIGHT_TEXT_THRESHOLD else 255)
    else:
        gray = ImageOps.autocontrast(gray)
    scale = upscale_factor(gray.width, gray.height)
    big = gray.resize((gray.width * scale, gray.height * scale), Image.LANCZOS)
    buf = io.BytesIO()
    big.save(buf, format="PNG")
    try:
        result = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", langs, "--psm", "11", "tsv"],
            input=buf.getvalue(),
            capture_output=True,
            timeout=OCR_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning(f"Install auto mode: tesseract failed: {e}")
        return []
    if result.returncode != 0:
        log.warning(
            "Install auto mode: tesseract exited with "
            f"{result.returncode}: {result.stderr.decode(errors='replace')[:200]}"
        )
    return parse_tsv(result.stdout.decode("utf-8", errors="replace"), scale)


def parse_tsv(tsv: str, scale: int = 1) -> list[Word]:
    words: list[Word] = []
    for row in tsv.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5":
            continue
        text = cols[11].strip()
        if not text:
            continue
        try:
            block, par, line = int(cols[2]), int(cols[3]), int(cols[4])
            left, top, width, height = (int(c) // scale for c in cols[6:10])
            conf = float(cols[10])
        except ValueError:
            continue
        words.append(Word(text, left, top, width, height, conf, (block, par, line)))
    return words


# Deep pass: the buttons of a wizard sit in its bottom strip, drawn on flat
# backgrounds where whole-page OCR often misses them. Tesseract reads small,
# tight crops far better, so that strip is cut into separate text blocks.
DEEP_BAND_HEIGHT = 46
# Taller band for the light-pixel pass: skinned wizards keep their buttons
# above a decorative footer.
LIGHT_BAND_HEIGHT = 200
FRAME_INSET = 4
DEEP_SCALE = 4
DEEP_PAD = 20
INK_THRESHOLD = 28
ROW_GAP = 3
COL_GAP = 14
MIN_BLOCK_SIDE = 7
MAX_BLOCK_HEIGHT = 60
MAX_BLOCKS = 8
BLOCK_WORKERS = 4
# Wider blocks are sentences and pictures, not buttons or checkbox labels.
MAX_BLOCK_WIDTH = 420
RULE_FRACTION = 0.6
# Rows this bright are sky or artwork, not text, in the light-pixel pass.
LIGHT_RULE_FRACTION = 0.2


def _runs(profile: list[int], max_gap: int, min_len: int) -> list[tuple[int, int]]:
    """Spans of non-zero entries, merging spans closer than ``max_gap``."""
    spans: list[list[int]] = []
    for i, v in enumerate(profile):
        if not v:
            continue
        if spans and i - spans[-1][1] <= max_gap:
            spans[-1][1] = i + 1
        else:
            spans.append([i, i + 1])
    return [(a, b) for a, b in spans if b - a >= min_len]


def text_blocks(
    image: Image.Image, light: bool = False
) -> list[tuple[int, int, int, int]]:
    """Boxes (left, top, right, bottom) of separate text-like blocks, ordered
    bottom first. ``light`` looks for bright pixels instead of edges."""
    if light:
        ink = image.convert("L").point(lambda p: 255 if p > LIGHT_TEXT_THRESHOLD else 0)
    else:
        gray = ImageOps.autocontrast(image.convert("L"))
        ink = gray.filter(ImageFilter.FIND_EDGES).point(
            lambda p: 255 if p > INK_THRESHOLD else 0
        )
    width = ink.width
    pixels = ink.tobytes()
    lines = [pixels[y * width : (y + 1) * width] for y in range(ink.height)]
    # Full-width lines (bar borders, separators) are not text and would glue
    # every block on their rows into one.
    usable = [
        0 < line.count(255) <= width * (LIGHT_RULE_FRACTION if light else RULE_FRACTION)
        for line in lines
    ]
    boxes = []
    for top, bottom in _runs([int(u) for u in usable], ROW_GAP, MIN_BLOCK_SIDE):
        if bottom - top > MAX_BLOCK_HEIGHT:
            continue
        rows = [lines[y] for y in range(top, bottom) if usable[y]]
        cols = [1 if any(row[x] for row in rows) else 0 for x in range(width)]
        for left, right in _runs(cols, COL_GAP, MIN_BLOCK_SIDE):
            if right - left <= MAX_BLOCK_WIDTH:
                boxes.append((left, top, right, bottom))
    boxes.sort(key=lambda b: -b[1])
    return boxes[:MAX_BLOCKS]


def _ocr_block(
    band: Image.Image,
    box: tuple[int, int, int, int],
    index: int,
    origin: tuple[int, int],
    langs: str,
    light: bool,
) -> list[Word]:
    """Read one text block of ``band``; words come back in image coordinates."""
    crop = band.crop(box).convert("L")
    if light:
        crop = crop.point(lambda p: 0 if p > LIGHT_TEXT_THRESHOLD else 255)
    else:
        crop = ImageOps.autocontrast(crop)
    crop = ImageOps.expand(crop, DEEP_PAD, fill=255 if light else crop.getpixel((0, 0)))
    big = crop.resize(
        (crop.width * DEEP_SCALE, crop.height * DEEP_SCALE), Image.BICUBIC
    )
    buf = io.BytesIO()
    big.save(buf, format="PNG", dpi=(300, 300))
    try:
        result = subprocess.run(
            [
                "tesseract",
                "stdin",
                "stdout",
                "-l",
                langs,
                "--psm",
                "7",
                "--dpi",
                "300",
                "tsv",
            ],
            input=buf.getvalue(),
            capture_output=True,
            timeout=OCR_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [
        Word(
            w.text,
            origin[0] + box[0] + w.left - DEEP_PAD,
            origin[1] + box[1] + w.top - DEEP_PAD,
            w.width,
            w.height,
            w.conf,
            (1000 + index, 0, 0),
        )
        for w in parse_tsv(result.stdout.decode("utf-8", errors="replace"), DEEP_SCALE)
    ]


def ocr_bottom_blocks(
    image: Image.Image,
    langs: str = INSTALL_AUTO_OCR_DEEP_LANGS,
    band_height: int = DEEP_BAND_HEIGHT,
    light: bool = False,
) -> list[Word]:
    """OCR the bottom strip of ``image`` block by block; words come back in
    ``image`` coordinates, one OCR line per block."""
    # The crop carries a margin of desktop around the window: measure from
    # the window itself, not from the crop's edge.
    background = Image.new(image.mode, image.size, image.getpixel((0, 0)))
    content = ImageChops.difference(image, background).getbbox()
    if content is None:
        return []
    # Inset so the window's own border lines don't count as text.
    band_left = content[0] + FRAME_INSET
    band_right = content[2] - FRAME_INSET
    content_bottom = content[3] - FRAME_INSET
    band_top = max(0, content_bottom - band_height)
    band = image.crop((band_left, band_top, band_right, content_bottom))
    margin = 3
    jobs = [
        (
            band,
            (
                max(0, left - margin),
                max(0, top - margin),
                min(band.width, right + margin),
                min(band.height, bottom + margin),
            ),
            index,
            (band_left, band_top),
            langs,
            light,
        )
        for index, (left, top, right, bottom) in enumerate(text_blocks(band, light))
    ]
    if not jobs:
        return []
    with ThreadPoolExecutor(max_workers=BLOCK_WORKERS) as pool:
        results = pool.map(lambda job: _ocr_block(*job), jobs)
    return [w for words in results for w in words]
