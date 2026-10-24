"""Source guards for decision 42's one audited in-process extractor (#1295)."""

import ast
import hashlib
from pathlib import Path

import pytest
from src.extractors import EXTRACTOR_VERSIONS

ROOT = Path(__file__).resolve().parents[1] / "src" / "extractors"
IN_PROCESS = {
    "text": "Fixed codec set, bounded BOM loop and constant-pass byte decoding; no structural parser."
}
AUDITED_TEXT_SHA256 = (
    "51dc673b389de3000e851b150f945c700cbafbc4db305af780b2ee255fccc62a"  # pragma: allowlist secret
)


def _executable_hash(source):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
            value = body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                del body[0]
    return hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()


def _uses_runner(source):
    tree = ast.parse(source)
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    pending, seen = ["extract"], set()
    while pending:
        name = pending.pop()
        if name in seen or name not in functions:
            continue
        seen.add(name)
        calls = {
            node.func.id
            for node in ast.walk(functions[name])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        runner_attributes = {
            node.func.attr
            for node in ast.walk(functions[name])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "_runner"
        }
        if (calls | runner_attributes) & {"run_child", "run_tool"}:
            return True
        pending.extend(calls & functions.keys())
    return False


def _assert_text_audit(source):
    assert _executable_hash(source) == AUDITED_TEXT_SHA256, (
        "text.py changed: re-audit its fixed codecs/constant-pass decoding or move it into a child"
    )


def test_every_registered_extractor_dispatches_through_the_runner_unless_audited():
    assert set(IN_PROCESS) == {"text"}
    assert set(IN_PROCESS) <= EXTRACTOR_VERSIONS.keys()
    assert all(IN_PROCESS.values())
    for module in EXTRACTOR_VERSIONS:
        source = (ROOT / f"{module}.py").read_text()
        if module in IN_PROCESS:
            _assert_text_audit(source)
        else:
            assert _uses_runner(source), module


@pytest.mark.parametrize(
    "addition",
    [
        "\nimport csv\n",
        "\ndef unbounded():\n    while True:\n        pass\n",
        "\ndef recursive():\n    return recursive()\n",
    ],
)
def test_text_audit_rejects_import_loop_and_recursive_helper_mutations(addition):
    with pytest.raises(AssertionError, match="re-audit"):
        _assert_text_audit((ROOT / "text.py").read_text() + addition)


def test_text_audit_rejects_a_codec_change():
    with pytest.raises(AssertionError, match="re-audit"):
        _assert_text_audit((ROOT / "text.py").read_text().replace('"utf-8"', '"utf-7"'))


def test_comment_and_docstring_changes_do_not_require_an_executable_reaudit():
    source = (ROOT / "text.py").read_text()
    assert _executable_hash(source) == _executable_hash(
        source.replace("Plain-text / CSV", "Text / CSV") + "\n# editorial\n"
    )


def test_dispatch_guard_rejects_a_new_in_process_extractor():
    assert not _uses_runner("def extract(payload):\n    return payload.decode(), 'new'\n")
