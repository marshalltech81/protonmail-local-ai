"""#1607 review round 7: scripts/config/operator_config.py mirrors the
address and domain rules of ``load_authority_rules`` and
``load_operator_identity`` so a translation the indexer would refuse is
refused first. This compares the two on a catalogue of shapes: the
translator's verdict on each value must equal the loader's on the file
the translator renders. Synthetic values only."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from src.entities import (
    AUTHORITY_CLASSES,
    AUTHORITY_RULES_MAX_BYTES,
    AUTHORITY_RULES_MAX_PATTERNS,
    IDENTITY_MAX_ADDRESSES,
    IDENTITY_MAX_BYTES,
    AuthorityRulesError,
    OperatorIdentityError,
    load_authority_rules,
    load_operator_identity,
)

_REPO = Path(__file__).resolve().parents[2]


def _operator_config():
    path = _REPO / "scripts" / "config" / "operator_config.py"
    spec = importlib.util.spec_from_file_location("operator_config", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses look the module up by name while the class is built.
    sys.modules["operator_config"] = module
    spec.loader.exec_module(module)
    return module


oc = _operator_config()

_LONG_LABEL = "a" * 63
DOMAINS = [
    "example.com",
    "Sub.Example.COM",
    " padded.example ",
    "a-b.example",
    "xn--bcher-kva.example",
    "localhost",
    "1.2.3.4",
    "*.example",
    "-lead.example",
    "trail-.example",
    "a..example",
    ".example",
    "example.",
    "under_score.example",
    "space d.example",
    "http://x.example",
    "x.example/path",
    "bücher.example",
    f"{_LONG_LABEL}.example",
    f"{_LONG_LABEL}a.example",
    ".".join(["a"] * 16),
    ".".join(["a"] * 17),
    ".".join([_LONG_LABEL] * 4)[:253],
    "a" * 254,
    "user@x.example",
]
ADDRESSES = [
    "me@mail.example",
    "Me@Mail.Example",
    " me@mail.example ",
    "me+tag@mail.example",
    "first.last@mail.example",
    '"quoted"@mail.example',
    '"a b"@mail.example',
    "Name <me@mail.example>",
    "<me@mail.example>",
    "me@mail.example (comment)",
    "me",
    "@mail.example",
    "me@",
    "me@@mail.example",
    "a@b@mail.example",
    "me@mail_example",
    "me@*.example",
    "me@mail.example.",
    "me@localhost",
    "me@[127.0.0.1]",
    "me @mail.example",
    "mé@mail.example",
    "me,you@mail.example",
    "me;x@mail.example",
    "(c)me@mail.example",
]


def _toml_list(name: str, values: list[str]) -> str:
    return f"{name} = [{', '.join(json.dumps(v) for v in values)}]\n"


def _indexer_accepts_authority(tmp_path: Path, text: str) -> bool:
    path = tmp_path / "authority.toml"
    path.write_text(text, encoding="utf-8")
    try:
        load_authority_rules(path)
    except AuthorityRulesError:
        return False
    return True


def _translator_accepts_authority(authority: dict) -> bool:
    try:
        oc._check_authority(authority)
    except oc.ConfigError:
        return False
    return True


def _authority(**tables: dict) -> dict:
    return {cls: tables.get(cls) for cls in AUTHORITY_CLASSES}


def test_the_mirrored_limits_are_the_indexers():
    assert oc.AUTHORITY_CLASSES == AUTHORITY_CLASSES
    assert oc.AUTHORITY_RULES_MAX_BYTES == AUTHORITY_RULES_MAX_BYTES
    assert oc.AUTHORITY_RULES_MAX_PATTERNS == AUTHORITY_RULES_MAX_PATTERNS
    assert oc.IDENTITY_MAX_BYTES == IDENTITY_MAX_BYTES
    assert oc.IDENTITY_MAX_ADDRESSES == IDENTITY_MAX_ADDRESSES


@pytest.mark.parametrize(
    "key, value", [("domains", d) for d in DOMAINS] + [("addresses", a) for a in ADDRESSES]
)
def test_an_authority_pattern_gets_the_indexers_verdict(tmp_path, key, value):
    authority = _authority(counsel={"domains": None, "addresses": None, key: [value]})
    expected = _indexer_accepts_authority(tmp_path, f"[counsel]\n{_toml_list(key, [value])}")
    assert _translator_accepts_authority(authority) == expected


@pytest.mark.parametrize(
    "first, second",
    [
        (("counsel", "domains", "a.example"), ("vendor", "domains", " A.Example")),
        (("counsel", "addresses", "me@a.example"), ("other", "addresses", "ME@a.example")),
        (("counsel", "domains", "a.example"), ("counsel", "addresses", "x@a.example")),
        (("counsel", "domains", "a.example"), ("counsel", "domains", "b.a.example")),
    ],
)
def test_repeated_patterns_get_the_indexers_verdict(tmp_path, first, second):
    tables: dict[str, dict] = {}
    for cls, key, value in (first, second):
        table = tables.setdefault(cls, {"domains": None, "addresses": None})
        table[key] = [*(table[key] or []), value]
    text = "".join(
        f"[{cls}]\n"
        + "".join(_toml_list(key, table[key]) for key in ("domains", "addresses") if table[key])
        for cls, table in sorted(tables.items(), key=lambda item: AUTHORITY_CLASSES.index(item[0]))
    )
    expected = _indexer_accepts_authority(tmp_path, text)
    assert _translator_accepts_authority(_authority(**tables)) == expected


@pytest.mark.parametrize("values", [[a] for a in ADDRESSES] + [["me@a.example", " Me@A.example"]])
def test_an_identity_address_gets_the_indexers_verdict(tmp_path, values):
    path = tmp_path / "identity.toml"
    path.write_text(_toml_list("addresses", values), encoding="utf-8")
    try:
        load_operator_identity(path)
        expected = True
    except OperatorIdentityError:
        expected = False
    try:
        oc._check_identity({"addresses": values}, "accounts[0]")
        actual = True
    except oc.ConfigError:
        actual = False
    assert actual == expected


def test_the_catalogue_holds_both_verdicts(tmp_path):
    """A catalogue the loader accepts (or refuses) entirely would compare nothing."""
    verdicts = {
        _indexer_accepts_authority(tmp_path, f"[counsel]\n{_toml_list(key, [value])}")
        for key, values in (("domains", DOMAINS), ("addresses", ADDRESSES))
        for value in values
    }
    assert verdicts == {True, False}
