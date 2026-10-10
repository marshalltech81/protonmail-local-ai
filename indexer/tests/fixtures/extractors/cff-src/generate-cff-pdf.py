# /// script
# requires-python = ">=3.14"
# dependencies = ["fonttools==4.66.1", "pypdf==6.20.0"]
# ///
"""Write ``cff-font.pdf``, the CFF-font extraction canary for #691.

The page's only text is set in an embedded CFF (``/FontFile3`` with
``/Subtype /Type1C``) Type1 font that has no ``/Encoding`` and no
``/ToUnicode``, so the character codes resolve through the font
program's built-in encoding alone. That encoding places each letter's
glyph at the letter's ROT13 code, and the content stream holds the
sentence in ROT13: a reader that parses the CFF encoding recovers the
sentence, while pypdf without fontTools falls back to StandardEncoding
and returns the ROT13 text as ``success``. The font's glyphs are plain
boxes; only its encoding matters.

Everything in the file is synthetic, and no document information
dictionary or XMP stream is written, so no author or producer name
reaches the fixture. Output is deterministic for a given pypdf and
fontTools version.

Usage, from ``indexer/tests/fixtures/extractors``::

    uv run cff-src/generate-cff-pdf.py

``uv`` reads the pinned dependencies from the inline metadata above and
runs the script in its own environment; fontTools is not an indexer
dependency and must stay out of ``indexer/uv.lock`` until #691 is
resolved.
"""

import codecs
import io
import string
from pathlib import Path

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    ContentStream,
    DictionaryObject,
    NameObject,
    NumberObject,
    StreamObject,
)

SENTENCE = "Synthetic CFF marker: the quick brown fox jumps over the lazy dog."
LETTERS = string.ascii_uppercase + string.ascii_lowercase
GLYPH_WIDTH = 600
OUTPUT = Path(__file__).resolve().parent.parent / "cff-font.pdf"


def build_cff() -> bytes:
    """A bare CFF font program: ``.notdef``, ``space`` and the 52 ASCII
    letters, each letter's glyph at its ROT13 code in the built-in
    encoding."""
    glyph_order = [".notdef", "space", *LETTERS]
    builder = FontBuilder(1000, isTTF=False)
    builder.setupGlyphOrder(glyph_order)
    charstrings = {}
    for name in glyph_order:
        pen = T2CharStringPen(GLYPH_WIDTH, None)
        if name in LETTERS:
            pen.moveTo((50, 0))
            pen.lineTo((550, 0))
            pen.lineTo((550, 700))
            pen.lineTo((50, 700))
            pen.closePath()
        charstrings[name] = pen.getCharString()
    builder.setupCFF(
        "SyntheticCFF",
        {"FullName": "Synthetic CFF", "FamilyName": "Synthetic CFF"},
        charstrings,
        {},
    )
    encoding = [".notdef"] * 256
    encoding[ord(" ")] = "space"
    for letter in LETTERS:
        encoding[ord(codecs.encode(letter, "rot13"))] = letter
    builder.font["CFF "].cff.topDictIndex[0].Encoding = encoding
    return builder.font["CFF "].compile(builder.font)


def build_pdf(cff: bytes) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    content = ContentStream(None, writer)
    encoded = codecs.encode(SENTENCE, "rot13").encode("ascii")
    content._data = b"BT /F1 12 Tf 72 720 Td (" + encoded + b") Tj ET"
    page[NameObject("/Contents")] = content

    font_file = StreamObject()
    font_file.set_data(cff)
    font_file[NameObject("/Subtype")] = NameObject("/Type1C")
    descriptor = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/FontDescriptor"),
            NameObject("/FontName"): NameObject("/SyntheticCFF"),
            NameObject("/Flags"): NumberObject(32),
            NameObject("/FontBBox"): ArrayObject(NumberObject(n) for n in (0, 0, 600, 700)),
            NameObject("/ItalicAngle"): NumberObject(0),
            NameObject("/Ascent"): NumberObject(700),
            NameObject("/Descent"): NumberObject(0),
            NameObject("/CapHeight"): NumberObject(700),
            NameObject("/StemV"): NumberObject(80),
            NameObject("/FontFile3"): writer._add_object(font_file),
        }
    )
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/SyntheticCFF"),
            NameObject("/FirstChar"): NumberObject(32),
            NameObject("/LastChar"): NumberObject(122),
            NameObject("/Widths"): ArrayObject(NumberObject(GLYPH_WIDTH) for _ in range(32, 123)),
            NameObject("/FontDescriptor"): writer._add_object(descriptor),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    # No /Info dictionary: pypdf would otherwise write /Producer.
    writer.metadata = None
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def main() -> None:
    OUTPUT.write_bytes(build_pdf(build_cff()))
    print(f"wrote {OUTPUT} ({OUTPUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
