"""Regenerate the baseline's OCR fixtures (#908).

Two synthetic images of text, rendered with Pillow's bundled font so
the recipe needs no system font:

- ``chalkboard.png`` (t90): one line, the corpus's ``OCR_IMAGE_TEXT``,
  64 px tall on white, read by the image OCR extractor;
- ``scanned-notes.pdf`` (t91): one such line per page, the corpus's
  ``OCR_PDF_PAGES``, each page an 8-bit grey image with no text layer,
  so the PDF extractor OCRs every page and the build's lowered page cap
  drops the last.

The PDF is written by hand (an image XObject per page, Flate compressed,
a cross-reference table with the real offsets): Pillow's PDF writer
stamps a title and creation and modification dates into an ``/Info``
dictionary, and a committed fixture must carry no such metadata
(``AGENTS.md``). The PNG is Pillow's, which writes no text chunks. The
corpus attaches the committed bytes, so regenerating (a Pillow or zlib
change can move bytes) changes nothing until the files are committed.

Usage, from ``indexer/``:

    uv run python -m tests.baseline.fixtures.generate
"""

import ast
import zlib
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

_SIZE = (1000, 160)
_DPI = 200
_HERE = Path(__file__).parent
_CORPUS = _HERE.parent / "corpus.py"


def _corpus_constant(name: str) -> Any:
    """The literal ``corpus.py`` assigns to ``name``, read from its source:
    the corpus reads the committed images at import, so importing it
    could not run before they exist (review round 1 on #908)."""
    for node in ast.parse(_CORPUS.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and [
            t.id for t in node.targets if isinstance(t, ast.Name)
        ] == [name]:
            return ast.literal_eval(node.value)
    raise LookupError(f"{name} is not assigned a literal in {_CORPUS.name}")


OCR_IMAGE_FILENAME: str = _corpus_constant("OCR_IMAGE_FILENAME")
OCR_IMAGE_TEXT: str = _corpus_constant("OCR_IMAGE_TEXT")
OCR_CAPPED_PDF_FILENAME: str = _corpus_constant("OCR_CAPPED_PDF_FILENAME")
OCR_PDF_PAGES: tuple[str, ...] = _corpus_constant("OCR_PDF_PAGES")


def _render(text: str) -> Image.Image:
    image = Image.new("L", _SIZE, 255)
    ImageDraw.Draw(image).text((40, 40), text, fill=0, font=ImageFont.load_default(size=64))
    return image


def _image_pdf(pages: list[Image.Image]) -> bytes:
    """A PDF of ``pages``, one grey image each, at ``_DPI``."""
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # the page tree, filled in once the page numbers are known
    ]
    page_refs = []
    for image in pages:
        width, height = image.size
        pixels = zlib.compress(image.tobytes(), 9)
        page_number = len(objects) + 1
        page_refs.append(f"{page_number} 0 R")
        box = f"[0 0 {width * 72 / _DPI:g} {height * 72 / _DPI:g}]"
        content = f"q {width * 72 / _DPI:g} 0 0 {height * 72 / _DPI:g} 0 0 cm /Im0 Do Q".encode()
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox {box} /Resources << /XObject"
            f" << /Im0 {page_number + 1} 0 R >> >> /Contents {page_number + 2} 0 R >>".encode()
        )
        objects.append(
            f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height}"
            f" /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode"
            f" /Length {len(pixels)} >>\nstream\n".encode()
            + pixels
            + b"\nendstream"
        )
        objects.append(
            f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream"
        )
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(page_refs)}] /Count {len(pages)} >>".encode()

    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out


def write(out_dir: Path) -> tuple[Path, Path]:
    """Write both images under ``out_dir``; return their paths."""
    image, pdf = out_dir / OCR_IMAGE_FILENAME, out_dir / OCR_CAPPED_PDF_FILENAME
    _render(OCR_IMAGE_TEXT).save(image, "PNG")
    pdf.write_bytes(_image_pdf([_render(line) for line in OCR_PDF_PAGES]))
    return image, pdf


if __name__ == "__main__":
    print("wrote {} and {}".format(*write(_HERE)))
