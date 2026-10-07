# Extractor fixtures

Every fixture here is synthetic. None comes from real mail.

## Legacy binary Office files (#935)

`legacy.doc` and `legacy.xls` are real OLE2 files written by a tool, not
hand-made bytes.

- **Tool:** LibreOffice 26.8.0.3 (`soffice --version`:
  `LibreOffice 26.8.0.3 bce0998afefdbc355585ca324285661a2170ba77`),
  installed on macOS with `brew install --cask libreoffice` only to make
  these files.
- **Sources:** in `legacy-src/`.
  - `legacy-doc.txt`, a UTF-8 text memo, is exported with the
    `MS Word 97` filter.
  - `legacy-xls.fods`, a flat ODF spreadsheet with two sheets, is
    exported with the `MS Excel 97` filter.
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
