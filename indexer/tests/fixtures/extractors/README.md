# Extractor fixtures

Every fixture here is synthetic. None comes from real mail.

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
