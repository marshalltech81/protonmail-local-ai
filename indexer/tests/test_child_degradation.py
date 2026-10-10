"""The degradation an extraction records in the extractor child reaches
the parent (#1314).

An extraction in the child records its degradation (text lost, OCR
pages skipped, the module counters) in the child's memory, and the
lines its helpers log go to the stderr the runner discards. The child
sends what it recorded as ``N <key> <count>`` frames under fixed keys;
the parent re-applies them and logs one rate-limited line. Payloads
and markers are synthetic.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest
from src import extractors
from src.extractors import (
    CHILD_DEGRADATION_KEYS,
    STATUS_SUCCESS,
    _runner,
    extract,
    extractor_child,
)

from tests.test_extractor_child import _MODULES, _extract, _module, stub_child_output

MARKER = "SYNTHETIC_DEGRADATION_MARKER"
_SRC = Path(extractors.__file__).resolve().parent.parent


class TestChildSends:
    def test_recorded_degradation_is_sent_as_count_frames(self, monkeypatch):
        """Counts past the child's own line budget all cross, with the
        attempt's text loss and OCR pages skipped."""
        extractors.drain_counters()
        log = logging.getLogger("test.child")
        cuts = extractors._WARNINGS_PER_WINDOW + 5

        def extract_text(_payload):
            for _ in range(cuts):
                extractors.warn_extractor_cap(log, "synthetic_cap", MARKER)
            extractors.note_ocr_capped_image()
            extractors.record_ocr_pages_skipped(3)
            return "text", []

        monkeypatch.setattr(_module("docx"), "extract_text", extract_text)
        out = extractor_child.run("docx", b"x")
        head, _, body = out.partition(b"T 4\n")
        assert body == b"text"
        assert sorted(head.decode().splitlines()) == sorted(
            [
                f"N extractor_caps {cuts}",
                "N ocr_capped_images 1",
                "N text_lost 1",
                "N result_ocr_pages_skipped 3",
            ]
        )
        assert MARKER.encode() not in out
        # Drained: nothing is counted twice when the parent re-applies.
        assert extractors.drain_counters()["extractor_caps"] == 0

    def test_the_attempts_state_is_cleared_first(self, monkeypatch):
        """A loss recorded before the run (the dispatcher's or an earlier
        attempt's) is not reported for this one."""
        extractors.drain_counters()
        extractors.note_text_lost()
        extractors.record_ocr_pages_skipped(2)
        monkeypatch.setattr(_module("docx"), "extract_text", lambda _p: ("text", []))
        assert extractor_child.run("docx", b"x") == b"T 4\ntext"

    def test_the_keys_are_the_counters_and_the_attempt(self):
        """Derived from the counts the indexer reports, not from the list
        under test: every counter crosses; the suppressed-line count does
        not (the child's lines never reach the log)."""
        counters = set(extractors.drain_extractor_counts()) - {"warnings_suppressed"}
        assert (
            frozenset(
                counters
                | {"text_lost", "result_ocr_pages_skipped", "image_scale_factor", "pdf_ocr_dpi"}
            )
            == CHILD_DEGRADATION_KEYS
        )


class TestParentApplies:
    @pytest.mark.parametrize(
        "key", sorted(set(extractors.drain_extractor_counts()) - {"warnings_suppressed"})
    )
    def test_each_counter_is_re_applied_once(self, key, monkeypatch, caplog):
        extractors.drain_extractor_counts()
        caplog.set_level("DEBUG")
        stub_child_output(monkeypatch, f"N {key} 7\nT 4\ntext".encode())
        result = _extract("docx")
        assert result.status == STATUS_SUCCESS
        # A count alone is not a loss: the child reports that apart.
        assert result.text_complete is True
        assert extractors.drain_extractor_counts()[key] == 7
        lines = [r for r in caplog.records if "degraded in the child" in r.getMessage()]
        assert [(r.levelno, r.getMessage()) for r in lines] == [
            (logging.WARNING, f"extractor docx degraded in the child: {key}=7")
        ]

    @pytest.mark.parametrize("module", _MODULES)
    def test_parent_suppression_keeps_completeness_and_counts(self, module, monkeypatch, caplog):
        """Past the parent's line budget the line is withheld and counted
        as suppressed, but every result still records its loss and every
        count is kept."""
        extractors.drain_extractor_counts()
        caplog.set_level("DEBUG")
        stub_child_output(
            monkeypatch,
            b"N text_lost 1\nN extractor_caps 2\nN result_ocr_pages_skipped 4\nT 4\ntext",
        )
        attempts = extractors._WARNINGS_PER_WINDOW + 5
        results = [_extract(module) for _ in range(attempts)]
        assert all(r.status == STATUS_SUCCESS and r.text_complete is False for r in results)
        assert all(r.ocr_pages_skipped == 4 for r in results)
        lines = [r for r in caplog.records if "degraded in the child" in r.getMessage()]
        assert len(lines) == extractors._WARNINGS_PER_WINDOW
        assert lines[0].getMessage() == (
            f"extractor {module} degraded in the child: "
            "extractor_caps=2 result_ocr_pages_skipped=4 text_lost=1"
        )
        counts = extractors.drain_extractor_counts()
        assert counts["extractor_caps"] == 2 * attempts
        assert counts["warnings_suppressed"] == 5

    def test_a_clean_child_logs_nothing(self, monkeypatch, caplog):
        caplog.set_level("DEBUG")
        stub_child_output(monkeypatch, b"T 4\ntext")
        result = _extract("xlsx")
        assert result.text_complete is True
        assert "degraded in the child" not in caplog.text

    @pytest.mark.parametrize(
        "frame",
        [
            pytest.param(b"N warnings_suppressed 3\n", id="suppressed-lines-not-a-key"),
            pytest.param(b"N text_lost -1\n", id="negative"),
            pytest.param(b"N text_lost " + MARKER.encode() + b"\n", id="not-a-count"),
            pytest.param(b"N " + MARKER.encode() + b" 1\n", id="unknown-key"),
        ],
    )
    def test_anything_but_a_key_and_a_count_fails_the_extraction(self, frame, monkeypatch, caplog):
        caplog.set_level("DEBUG")
        stub_child_output(monkeypatch, frame + b"T 4\ntext")
        result = _extract("pptx")
        assert (result.status, result.error) == ("failed", "ChildOutputError")
        assert MARKER not in caplog.text


@pytest.mark.real_extractor_child
class TestRealProcess:
    def test_degradation_crosses_a_real_child(self, tmp_path, monkeypatch, caplog):
        """A child whose extraction counts more caps than its own line
        budget allows, and logs a synthetic marker: every count, the
        text loss and the OCR pages skipped reach the parent through the
        launcher; the marker reaches no log line or error."""
        cuts = extractors._WARNINGS_PER_WINDOW + 5
        child = tmp_path / "degrading_child.py"
        child.write_text(
            "import logging, sys\n"
            f"sys.path.insert(0, {str(_SRC.parent)!r})\n"
            "from src import extractors\n"
            "from src.extractors import docx, extractor_child\n"
            "log = logging.getLogger('child')\n"
            "def extract_text(payload):\n"
            f"    for _ in range({cuts}):\n"
            f"        extractors.warn_extractor_cap(log, 'synthetic_cap', {MARKER!r})\n"
            f"    extractors.warn_rate_limited(log, {MARKER!r})\n"
            f"    log.error({MARKER!r})\n"
            "    extractors.note_ocr_capped_image()\n"
            "    extractors.record_ocr_pages_skipped(3)\n"
            "    return 'text', []\n"
            "docx.extract_text = extract_text\n"
            "sys.exit(extractor_child.main(sys.argv))\n"
        )
        monkeypatch.setattr(_runner, "_CHILD", child)
        extractors.drain_extractor_counts()
        caplog.set_level("DEBUG")
        result = extract(
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename="a.docx",
            payload=_ooxml_payload(),
        )
        assert (result.status, result.text) == (STATUS_SUCCESS, "text")
        assert result.text_complete is False
        assert result.ocr_pages_skipped == 3
        counts = extractors.drain_extractor_counts()
        assert (counts["extractor_caps"], counts["ocr_capped_images"]) == (cuts, 1)
        lines = [r for r in caplog.records if "degraded in the child" in r.getMessage()]
        assert [(r.levelno, r.getMessage()) for r in lines] == [
            (
                logging.WARNING,
                f"extractor docx degraded in the child: extractor_caps={cuts} "
                "ocr_capped_images=1 result_ocr_pages_skipped=3 text_lost=1",
            )
        ]
        assert MARKER not in caplog.text
        assert MARKER not in (result.error or "")


def _ooxml_payload() -> bytes:
    from tests.test_ooxml_child import _payload

    return _payload("docx")


# ---------------------------------------------------------------------------
# Coverage: every degradation site the child runs reports through state
# that crosses.
# ---------------------------------------------------------------------------

# Calls that record degradation in the state the child sends.
_REPORTERS = frozenset({"warn_extractor_cap", "record_ocr_pages_skipped"})
# Calls that only log a line, which in the child goes to the discarded
# stderr and records nothing that crosses.
_LINE_ONLY = frozenset({"warn_rate_limited", "_warn_failed"})
_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)

# Line-only sites the child runs that are acceptable, keyed by
# (module, function), each with its reason. The child extractors return
# the names of the budgets that cut their text, which the parent logs
# through ``warn_extractor_cap``. The ``eml`` child (#922) reaches the
# parser's header decoders and part-filename helpers.
_HEADER_COUNTED = (
    "the eml child passes ``degraded``, so each fallback is counted into "
    "eml_headers_degraded, which crosses as an N frame (TestReviewRound1 in "
    "test_eml_extractor); the line is for the in-process parser's callers"
)
_FILENAME_COUNTED = (
    "the eml child's body walk passes ``degraded``, so each fallback is counted "
    "into eml_filenames_degraded, which crosses as an N frame (TestReviewRound2 "
    "in test_eml_extractor): inside a leaf .eml no other walk logs it; the line "
    "is for the in-process parser's callers"
)
_EXCLUDED: dict[tuple[str, str], str] = {
    ("src.parser", "_decode_text_header"): _HEADER_COUNTED,
    ("src.parser", "_decode_header_parts"): _HEADER_COUNTED,
    ("src.parser", "_decode_raw_8bit"): _HEADER_COUNTED,
    ("src.parser", "_decode_filename_words"): _FILENAME_COUNTED,
    ("src.parser", "_raw_part_filename"): _FILENAME_COUNTED,
}


def _is_reporter(name: str) -> bool:
    return name.startswith("note_") or name in _REPORTERS


class _Module:
    """One parsed module of the ``src`` tree: its functions and methods
    by name, the names it imports from ``src`` and its loggers."""

    def __init__(self, root: Path, dotted: str) -> None:
        self.dotted = dotted
        parts = dotted.split(".")
        path = root.joinpath(*parts)
        self.is_package = path.is_dir()
        file = path / "__init__.py" if self.is_package else path.with_suffix(".py")
        tree = ast.parse(file.read_text())
        self.functions: dict[str, list[ast.AST]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                self.functions.setdefault(node.name, []).append(node)
        package = dotted if self.is_package else dotted.rpartition(".")[0]
        # name -> (module, attribute or None for a module)
        self.imports: dict[str, tuple[str, str | None]] = {}
        self.loggers: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.level:
                base = package.split(".")
                base = base[: len(base) - node.level + 1]
                origin = ".".join(base + ([node.module] if node.module else []))
                for alias in node.names:
                    as_module = f"{origin}.{alias.name}"
                    target = root.joinpath(*as_module.split("."))
                    if target.is_dir() or target.with_suffix(".py").is_file():
                        self.imports[alias.asname or alias.name] = (as_module, None)
                    else:
                        self.imports[alias.asname or alias.name] = (origin, alias.name)
            elif (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr == "getLogger"
            ):
                self.loggers |= {t.id for t in node.targets if isinstance(t, ast.Name)}


def degradation_sites(root: Path, roots: list[tuple[str, str]]) -> dict:
    """Walk the calls from each ``(module, function)`` in ``roots``
    through the ``src`` tree under ``root``. Returns the functions
    reached and the line-only call sites found in them, as
    ``(module, function, line)``. A reporter is not entered: it is the
    channel that crosses."""
    modules: dict[str, _Module] = {}

    def module(name: str) -> _Module:
        if name not in modules:
            modules[name] = _Module(root, name)
        return modules[name]

    seen: set[tuple[str, str]] = set()
    sites: set[tuple[str, str, int]] = set()
    todo = list(roots)
    while todo:
        mod_name, func = todo.pop()
        if (mod_name, func) in seen or _is_reporter(func):
            continue
        seen.add((mod_name, func))
        mod = module(mod_name)
        for fn in mod.functions.get(func, []):
            for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call)):
                target = call.func
                if isinstance(target, ast.Name):
                    name = target.id
                    if name in _LINE_ONLY:
                        sites.add((mod_name, func, call.lineno))
                    elif name in mod.imports and (attr := mod.imports[name][1]) is not None:
                        todo.append((mod.imports[name][0], attr))
                    elif name in mod.functions:
                        todo.append((mod_name, name))
                elif isinstance(target, ast.Attribute):
                    owner = target.value
                    owner_name = owner.id if isinstance(owner, ast.Name) else None
                    if target.attr in _LINE_ONLY:
                        sites.add((mod_name, func, call.lineno))
                    elif target.attr in _LOG_METHODS and (
                        owner_name in mod.loggers or owner_name == "logging"
                    ):
                        sites.add((mod_name, func, call.lineno))
                    elif owner_name in mod.imports and mod.imports[owner_name][1] is None:
                        todo.append((mod.imports[owner_name][0], target.attr))
                    elif target.attr in mod.functions:
                        todo.append((mod_name, target.attr))
    return {"reached": seen, "sites": sites}


def _child_roots() -> list[tuple[str, str]]:
    return [(f"src.extractors.{name}", "extract_text") for name in extractor_child.MODULES.values()]


class TestEveryChildDegradationCrosses:
    def test_no_child_site_only_logs(self):
        """Every degradation site the child runs, in the modules
        ``extractor_child.MODULES`` lists and the ``src`` functions they
        call, records through ``note_*`` / ``warn_extractor_cap`` /
        ``record_ocr_pages_skipped`` (state that crosses), or is in the
        reasoned exclusion list."""
        found = degradation_sites(_SRC.parent, _child_roots())
        unreported = {site for site in found["sites"] if (site[0], site[1]) not in _EXCLUDED}
        assert unreported == set()
        # An exclusion no longer reached is stale.
        assert {(m, f) for m, f, _ in found["sites"]} >= set(_EXCLUDED)

    def test_the_walk_reaches_the_child_code(self):
        """Not vacuous: the walk follows calls within a module, into the
        package and into another module."""
        reached = degradation_sites(_SRC.parent, _child_roots())["reached"]
        for name in extractor_child.MODULES.values():
            assert (f"src.extractors.{name}", "extract_text") in reached
        assert ("src.extractors", "over_package_budget") in reached
        assert ("src.extractors.xls_child", "_walk") in reached

    def test_a_line_only_site_fails_and_a_reporter_passes(self, tmp_path):
        """The check catches the known-bad shape: a helper the extraction
        calls that only logs, directly or through ``warn_rate_limited``,
        in its own module or another."""
        package = tmp_path / "src" / "extractors"
        package.mkdir(parents=True)
        (tmp_path / "src" / "__init__.py").write_text("")
        (package / "__init__.py").write_text(
            "def note_text_lost():\n    pass\n"
            "def warn_rate_limited(logger, msg):\n    pass\n"
            "def helper():\n    warn_rate_limited(None, 'x')\n"
        )
        (package / "other.py").write_text(
            "import logging\nlog = logging.getLogger('x')\ndef walk():\n    log.warning('x')\n"
        )
        (package / "bad.py").write_text(
            "import logging\n"
            "from . import helper, note_text_lost\n"
            "from . import other\n"
            "log = logging.getLogger('x')\n"
            "def extract_text(payload):\n"
            "    note_text_lost()\n"
            "    _cut()\n"
            "    helper()\n"
            "    other.walk()\n"
            "    return '', []\n"
            "def _cut():\n"
            "    log.info('x')\n"
        )
        found = degradation_sites(tmp_path, [("src.extractors.bad", "extract_text")])
        assert {(m, f) for m, f, _ in found["sites"]} == {
            ("src.extractors.bad", "_cut"),
            ("src.extractors", "helper"),
            ("src.extractors.other", "walk"),
        }
