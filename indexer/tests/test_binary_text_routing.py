"""Synthetic routing and bounded-work regressions for #969."""

import pytest
from src import extractors


@pytest.mark.parametrize(
    "payload, module, name",
    [
        (b"%PDF-1.7 synthetic", "pdf", "pdf-digital"),
        (b"\x89PNG\r\n\x1a\nsynthetic", "image", "image-ocr"),
        (b"\xff\xd8\xffsynthetic", "image", "image-ocr"),
        (b"GIF87asynthetic", "image", "image-ocr"),
        (b"GIF89asynthetic", "image", "image-ocr"),
    ],
)
def test_binary_text_label_uses_the_existing_format_reader(monkeypatch, payload, module, name):
    called = []

    def reader(selected):
        def run(raw, **kwargs):
            called.append((selected, raw))
            return "SYNTHETIC_ROUTED_TEXT", name

        return run

    monkeypatch.setattr(extractors, "_safe_import", reader)
    result = extractors.extract(
        content_type="text/plain", filename="synthetic.txt", payload=payload
    )
    assert result.status == extractors.STATUS_SUCCESS
    assert result.text == "SYNTHETIC_ROUTED_TEXT"
    assert result.extractor == f"{name}@{extractors.EXTRACTOR_VERSIONS[module]}"
    assert called == [(module, payload)]
    assert extractors.extraction_module("text/plain", "synthetic.txt", payload) == module


def test_binary_text_label_does_not_bypass_the_existing_ocr_off_gate(monkeypatch):
    monkeypatch.setattr(extractors, "_safe_import", lambda module: pytest.fail("reader called"))
    result = extractors.extract(
        content_type="text/plain",
        filename="synthetic.txt",
        payload=b"\x89PNG\r\n\x1a\nsynthetic",
        ocr_enabled=False,
    )
    assert result.status == extractors.STATUS_UNSUPPORTED
    assert result.error == extractors.OCR_DISABLED_ERROR
    assert result.extractor == f"image@{extractors.EXTRACTOR_VERSIONS['image']}"


@pytest.mark.serial
@pytest.mark.parametrize("magic", [b"%PDF-", b"\x89PNG\r\n\x1a\n"], ids=["pdf", "png"])
def test_dispatch_prefix_work_does_not_grow_with_payload_size(monkeypatch, magic):
    import time

    work = []

    class Measured(bytes):
        def startswith(self, prefix, *args):
            work.append(sum(map(len, prefix)) if isinstance(prefix, tuple) else len(prefix))
            return super().startswith(prefix, *args)

        def __getitem__(self, key):
            if isinstance(key, slice):
                start, stop, step = key.indices(len(self))
                assert len(range(start, stop, step)) <= 256
            return super().__getitem__(key)

    calls = []

    def reader(module):
        def run(raw, **kwargs):
            calls.append(module)
            return "synthetic words", "pdf-digital" if module == "pdf" else "image-ocr"

        return run

    monkeypatch.setattr(extractors, "_safe_import", reader)
    measurements = []
    for size in (1024, extractors.DEFAULT_MAX_BYTES):
        payload = Measured(magic + b"x" * (size - len(magic)))
        work.clear()
        start = time.perf_counter()
        result = extractors.extract(
            content_type="text/plain", filename="synthetic.txt", payload=payload
        )
        assert time.perf_counter() - start < 2
        assert result.status == extractors.STATUS_SUCCESS
        measurements.append((len(work), sum(work)))
    assert measurements[0] == measurements[1]
    assert measurements[1][1] < 4000
    assert calls == (["pdf"] if magic == b"%PDF-" else ["image"]) * 2
