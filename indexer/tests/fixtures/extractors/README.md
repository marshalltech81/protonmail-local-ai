# Extractor fixtures

Every fixture here is synthetic. None comes from real mail.

`tests/test_fixture_authors.py` (#980) reads the author metadata of
every Office file under the indexer and mcp-server test trees (OLE2
Author and Last Saved By, OLE2 DocumentSummaryInformation Company and
Manager, OOXML `docProps/core.xml` creator and lastModifiedBy, OOXML
`docProps/app.xml` Company and Manager, ODF creator fields) and fails
unless each is empty or a synthetic name on its allowlist. Fix a
failure by regenerating the file with its recipe below or scrubbing it
the way `legacy-src/scrub-ppt-author.py` does. Names in binary records
(the PowerPoint Current User stream, Word's associated-strings,
revision and comment author tables, OOXML comment and revision
authors) are not read (#1010); check those by hand before committing.

## CFF-font PDF (#691)

`cff-font.pdf` (2,189 bytes) is the extraction canary for the fontTools
gap: pypdf reads the built-in encoding of an embedded CFF font only with
fontTools, which stays out of the image until py-pdf/pypdf#4156 bounds
its cost. `tests/test_extractors.py` `TestCffFontPdf` asserts the
sentence under a strict `xfail`, so the suite shows the gap today and
fails the day fontTools lands, which is also when `EXTRACTOR_VERSIONS["pdf"]`
must be bumped so cached rows re-extract.

- **Tool:** `cff-src/generate-cff-pdf.py`, run with
  `uv run cff-src/generate-cff-pdf.py` from this directory. Its inline
  script metadata pins `fonttools==4.66.1` and `pypdf==6.20.0`, and uv
  runs it in its own environment: fontTools is not an indexer dependency
  and does not touch `indexer/uv.lock`. The output is byte-for-byte
  reproducible with those versions.
- **Content:** one page whose only text is set in a Type1 font with an
  embedded CFF program (`/FontFile3`, `/Subtype /Type1C`) and no
  `/Encoding` or `/ToUnicode`. The font holds `.notdef`, `space` and the
  52 ASCII letters as plain boxes, and its built-in encoding places each
  letter's glyph at the letter's ROT13 code. The content stream holds the
  ROT13 of "Synthetic CFF marker: the quick brown fox jumps over the lazy
  dog." (66 characters, over the 40-character digital-text floor, so OCR
  never runs).
- **What extracts:** without fontTools, pypdf falls back to
  StandardEncoding and returns the ROT13 text as `success` /
  `pdf-digital@6`; with fontTools 4.66.1 it returns the sentence.
- **Metadata:** no document information dictionary (`writer.metadata =
  None`, so pypdf writes no `/Producer`), no XMP stream and no `/ID`.
  `TestCffFontPdf` checks the metadata and the font shape on every run.

## Legacy binary Office files (#935)

`legacy.doc`, `legacy.xls`, `legacy.ppt` and `legacy-lo.ppt` are real
OLE2 files written by a tool, not hand-made bytes.

- **Tool:** LibreOffice 26.8.0.3 (`soffice --version`:
  `LibreOffice 26.8.0.3 bce0998afefdbc355585ca324285661a2170ba77`),
  installed on macOS with `brew install --cask libreoffice` only to make
  these files.
- **Sources:** in `legacy-src/`.
  - `legacy-doc.txt`, a UTF-8 text memo, is exported with the
    `MS Word 97` filter.
  - `legacy-xls.fods`, a flat ODF spreadsheet with two sheets, is
    exported with the `MS Excel 97` filter.
  - `legacy-ppt.fodp`, a flat ODF presentation with one title-and-outline
    slide, is exported with the `MS PowerPoint 97` filter to
    `legacy-lo.ppt` (#957).
- **Command:** `legacy-src/generate.sh [path to soffice]`. It runs
  LibreOffice headless with a throwaway profile, so no user setting or
  name reaches the files:

  ```bash
  soffice -env:UserInstallation=file://<tmp>/profile --headless \
    --infilter="Text (encoded):UTF8,LF,,," \
    --convert-to 'doc:MS Word 97' --outdir <tmp> legacy-src/legacy-doc.txt
  soffice -env:UserInstallation=file://<tmp>/profile --headless \
    --convert-to 'xls:MS Excel 97' --outdir <tmp> legacy-src/legacy-xls.fods
  ```

  Running it again can change bytes such as timestamps, but not the text
  the tests check.

## Legacy PowerPoint deck from PowerPoint (#957)

`legacy.ppt` is the deck the `.ppt` tests rely on most: current
PowerPoint keeps all slide text in drawing records, which catppt never
read (#958).

- **Tool:** Microsoft PowerPoint for Mac 16.113.4, driven by AppleScript.
- **Command:** `legacy-src/generate-ppt.sh`. It runs
  `legacy-src/legacy-ppt.applescript`, which builds the deck and saves it
  as PowerPoint 97–2003 (`save as presentation`), with the Author set to
  "Synthetic Author". PowerPoint writes "Last Saved By" from the
  signed-in account on every save, so the script then runs
  `legacy-src/scrub-ppt-author.py` (needs `olefile`; run through
  `uvx --with olefile==0.47`), which overwrites that name with a
  synthetic one of the same length in the summary-information stream
  only. Check the result for personal data before committing it.
- **Content:**
  - slide 1, title and content: the title "Synthetic legacy slide deck";
    the body "The AMBER-KESTREL project code is 5129." and the non-ASCII
    line "Café crème at the Zürich office, naïve résumé.";
  - slide 2, blank, with a free text box: "The text box holds
    TEAL-MARMOT 3307."

`legacy-lo.ppt` has slide 1's text only. Most of its size is the
preview image LibreOffice writes into the summary information.

`legacy-encrypted.ppt` is a password-protected deck (#983), written by
the reader the indexer runs, so a test sees the exception that reader
raises for it.

- **Tool:** Apache POI 5.5.1 (the version `indexer/java/pom.xml` pins),
  on the JDK of the indexer's `ppt-builder` build stage.
- **Command:** `legacy-src/generate-encrypted-ppt.sh` (needs Docker). It
  builds that stage and runs `legacy-src/EncryptedPpt.java` in it with
  no network.
- **Content:** POI's blank deck plus one slide whose text box holds "The
  SYNTHETIC-ENCRYPTED-DECK code is 6021.", encrypted (RC4 CryptoAPI)
  with the synthetic password the script sets; the slide text and the
  summary information are in the encrypted streams only. The author,
  last-author and Current User names are "Synthetic Author": POI's
  blank-deck template carries a real name in its Current User stream,
  so the script replaces it. Checked by hand before committing: the
  only readable name in the file is "Synthetic Author".

What the tests rely on:

- **`legacy.doc`:** the attachment-only fact "The COBALT-LANTERN ledger
  code is 4471." and the non-ASCII line "Café crème at the Zürich
  office, naïve résumé."
- **`legacy.xls`:**
  - sheet `Summary` holds `COBALT-LANTERN` and "Café crème, Zürich";
  - sheet `Détails` holds `OBSIDIAN-HERON 8812`, which appears on the
    second sheet only;
  - the first shared string is `Item`.
- **Workbooks built from `legacy.xls` in `tests/test_legacy_office.py`:**
  the worst cases are made from this file by in-place edits of the same
  length, so the OLE2 container stays valid.
  - The shared-string loop sets the table's declared count to 2^31 − 1,
    and turns the 7-byte string `Item` into an empty string whose
    phonetic size is −7.
  - The directory cycle makes the root's first child its own left
    sibling.
