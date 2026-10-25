"""The shared OOXML size guard runs under the child's limits (#1438)."""

import importlib
import io
import zipfile

import pytest
from src import extractors
from src.extractors import extractor_child

from tests.test_ooxml_child import _MIME, _payload


@pytest.mark.real_extractor_child
@pytest.mark.parametrize("module", sorted(extractors.OOXML_MODULES))
def test_dispatch_never_opens_zip_metadata_in_the_parent(monkeypatch, module):
    payload = _payload(module)

    def forbidden(*args, **kwargs):
        pytest.fail("ZIP metadata was opened in the parent")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden)
    result = extractors.extract(content_type=_MIME[module], filename=f"a.{module}", payload=payload)
    assert result.status == extractors.STATUS_SUCCESS
    assert result.text


@pytest.mark.parametrize("module", sorted(extractors.OOXML_MODULES))
@pytest.mark.parametrize("sizes", [(17,), (9, 9)])
def test_child_checks_member_and_total_sizes_before_the_decoder(monkeypatch, module, sizes):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for i, size in enumerate(sizes):
            archive.writestr(f"SYNTHETIC_PRIVATE_MEMBER_{i}", b"x" * size)
    monkeypatch.setattr(extractors, "ZIP_MAX_UNCOMPRESSED_BYTES", 16)
    calls = []
    decoder = importlib.import_module(f"src.extractors.{module}")

    def forbidden(*args, **kwargs):
        calls.append(1)
        pytest.fail("decoder opened an oversized package")

    monkeypatch.setattr(decoder, "extract_text", forbidden)
    assert extractor_child.run(module, data.getvalue()) == b"E ZipUncompressedSizeError\n"
    assert calls == []


@pytest.mark.real_extractor_child
@pytest.mark.parametrize("members", [10_000, 300_000])
def test_many_member_directory_never_spends_parent_zip_work(monkeypatch, members):
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for i in range(members):
            archive.writestr(str(i), b"")
    payload = data.getvalue()
    assert len(payload) < extractors.DEFAULT_MAX_BYTES
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(1)
        pytest.fail("ZIP metadata was opened in the parent")

    monkeypatch.setattr(zipfile, "ZipFile", forbidden)
    result = extractors.extract(content_type=_MIME["docx"], filename="a.docx", payload=payload)
    assert result.status in {extractors.STATUS_UNSUPPORTED, extractors.STATUS_FAILED}
    assert calls == []
