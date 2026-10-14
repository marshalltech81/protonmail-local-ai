"""Bounded raw-header metadata, owner-approved for #825.

Raw values map original octets through Latin-1. Never decode encoded-words
into syntax. Values are spoofable filter evidence, never authority signals.
"""

import json
import string

FIELD_BYTES = 16 * 1024
FIELD_COUNT = 256
TOTAL_BYTES = 1024 * 1024
COMMENT_DEPTH = 32
KNOWN_STATUSES = ("valid", "duplicate", "unsupported", "absent")
_ATEXT = frozenset(string.ascii_letters + string.digits + "!#$%&'*+-/=?^_`{|}~")


def canonical_list_id(value: str) -> str | None:
    """A bracketless ASCII RFC 2919 dot-atom identifier, not a DNS name."""
    if not 1 <= len(value) <= 255 or "." not in value:
        return None
    if any(not atom or any(c not in _ATEXT for c in atom) for atom in value.split(".")):
        return None
    return value.lower()


def _unfold(raw: str) -> str | None:
    if len(raw) > FIELD_BYTES or any(ord(c) > 255 or c == "\x00" for c in raw):
        return None
    out = []
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == "\r":
            if raw[i : i + 2] != "\r\n" or i + 2 == len(raw) or raw[i + 2] not in " \t":
                return None
            i += 2
            continue
        if c == "\n" or (ord(c) < 32 and c != "\t") or ord(c) == 127:
            return None
        out.append(c)
        i += 1
    return "".join(out)


def _list_id(value: str) -> str | None:
    # Scan the phrase once. Brackets in quotes/comments are descriptive.
    quoted = False
    depth = 0
    escaped = False
    opening = None
    phrase_word = False
    for i, c in enumerate(value):
        if escaped:
            escaped = False
        elif (quoted or depth) and c == "\\":
            escaped = True
        elif depth:
            if c == "(":
                depth += 1
                if depth > COMMENT_DEPTH:
                    return None
            elif c == ")":
                depth -= 1
        elif quoted:
            if c == '"':
                quoted = False
        elif c == '"':
            quoted = True
            phrase_word = True
        elif c == "(":
            depth = 1
        elif c == "<":
            opening = i
            break
        elif c == ".":
            if not phrase_word:
                return None
        elif c in _ATEXT or ord(c) >= 128:
            phrase_word = True
        elif c not in " \t":
            return None
    if opening is None:
        return None
    try:
        value[:opening].encode("latin-1").decode("utf-8")
    except UnicodeError:
        return None
    closing = value.find(">", opening + 1)
    if closing < 0 or any(c not in " \t" for c in value[closing + 1 :]):
        return None
    identifier = "".join(c for c in value[opening + 1 : closing] if c not in " \t")
    return canonical_list_id(identifier)


def normalize_fields(name: str, fields: list[str]) -> tuple[str | None, str]:
    """One scalar and fixed status; SQL guards run before Python sees fields."""
    if (
        len(fields) > FIELD_COUNT
        or any(len(f) > FIELD_BYTES for f in fields)
        or sum(len(f) for f in fields) > TOTAL_BYTES
    ):
        return None, "budget_exceeded"
    if not fields:
        return None, "absent"
    tokens = []
    for raw in fields:
        value = _unfold(raw)
        if value is None:
            return None, "malformed"
        if name == "list-id":
            token = _list_id(value)
        else:
            token = value.strip(" \t").lower()
            if (
                not token
                or token.startswith("=?")
                or any(c not in _ATEXT and c != "." for c in token)
            ):
                token = None
        if token is None:
            return None, "malformed"
        tokens.append(token)
    if any(token != tokens[0] for token in tokens[1:]):
        return None, "conflict"
    value = tokens[0]
    unsupported = name == "precedence" and value not in ("bulk", "list", "junk")
    if unsupported:
        value = "unsupported"
    return value, "duplicate" if len(tokens) > 1 else "unsupported" if unsupported else "valid"


def sqlite_normalize_fields(name: str, encoded: str) -> str:
    return json.dumps(normalize_fields(name, json.loads(encoded)))


METADATA_HEADERS = {"list_id": "list-id", "precedence": "precedence"}


def metadata_result_sql(name: str, claimant: str = "m.claimant_id") -> str:
    """Code-owned identifiers only; one materialized result per claimant."""
    assert name in METADATA_HEADERS
    # Both identifiers are code-owned expressions, never tool or mail values.
    return f"(SELECT result FROM {name}_values WHERE cid={claimant})"  # nosec B608


def metadata_value_sql(name: str, claimant: str = "m.claimant_id") -> str:
    return f"json_extract({metadata_result_sql(name, claimant)}, '$[0]')"


def metadata_known_sql(name: str, claimant: str = "m.claimant_id") -> str:
    return (
        f"json_extract({metadata_result_sql(name, claimant)}, '$[1]') "
        "IN ('valid','duplicate','unsupported','absent')"
    )


def metadata_ctes(names: tuple[str, ...]) -> str:
    """Guard all dimensions before JSON aggregation or a Python UDF call.

    SQL TEXT length alone truncates at NUL. BLOB bytes bound allocation,
    and the NUL-free TEXT count bounds original Latin-1 octets exactly.
    UTF-8 storage may double the Latin-1 size. LIMIT bounds field fanout.
    """
    parts = [
        "metadata_limits AS MATERIALIZED (SELECT m.claimant_id AS cid, "  # nosec B608
        "CASE WHEN m.headers_complete=1 THEN (SELECT json_array(COUNT(*), "
        "COALESCE(SUM(b),0),COALESCE(SUM(n),0)) FROM (SELECT "
        "CASE WHEN typeof(raw_value)='text' THEN length(CAST(raw_value AS BLOB)) ELSE 0 END AS b, "
        "CASE WHEN typeof(raw_value)='text' AND instr(raw_value,char(0))=0 "
        "THEN length(raw_value) ELSE 0 END AS n FROM (SELECT raw_value FROM message_headers h "
        "WHERE h.claimant_id=m.claimant_id AND h.name_normalized IN ('list-id','precedence') "
        f"LIMIT {FIELD_COUNT + 1}))) ELSE '[]' END AS global_budget FROM messages m)"
    ]
    for name in names:
        header = METADATA_HEADERS[name]
        parts.append(
            f"{name}_budget AS MATERIALIZED (SELECT m.claimant_id AS cid, "  # nosec B608
            "m.headers_complete AS complete, CASE WHEN m.headers_complete=1 THEN "
            "(SELECT json_array(COUNT(*), COALESCE(SUM(b),0), COALESCE(SUM(n),0), "
            "COALESCE(MAX(b),0), COALESCE(MAX(n),0), COALESCE(MAX(bad),0), "
            "COALESCE(MAX(nul),0)) FROM (SELECT "
            "CASE WHEN typeof(raw_value)='text' THEN length(CAST(raw_value AS BLOB)) ELSE 0 END AS b, "
            "CASE WHEN typeof(raw_value)='text' AND instr(raw_value,char(0))=0 "
            "THEN length(raw_value) ELSE 0 END AS n, "
            "typeof(raw_value)!='text' AS bad, instr(raw_value,char(0))>0 AS nul "
            "FROM (SELECT raw_value FROM message_headers h WHERE h.claimant_id=m.claimant_id "
            f"AND h.name_normalized='{header}' LIMIT {FIELD_COUNT + 1}))) ELSE '[]' END AS budget "
            "FROM messages m), "
            f"{name}_values AS MATERIALIZED (SELECT cid, CASE "
            "WHEN complete IS NULL THEN '[null,\"unassessed\"]' "
            "WHEN complete IS NOT 1 THEN '[null,\"incomplete\"]' "
            f"WHEN json_extract(global_budget,'$[0]')>{FIELD_COUNT} "
            f"OR json_extract(global_budget,'$[1]')>{TOTAL_BYTES * 2} "
            f"OR json_extract(global_budget,'$[2]')>{TOTAL_BYTES} "
            f"OR json_extract(budget,'$[0]')>{FIELD_COUNT} "
            f"OR json_extract(budget,'$[1]')>{TOTAL_BYTES * 2} "
            f"OR json_extract(budget,'$[2]')>{TOTAL_BYTES} "
            f"OR json_extract(budget,'$[3]')>{FIELD_BYTES * 2} "
            f"OR json_extract(budget,'$[4]')>{FIELD_BYTES} THEN '[null,\"budget_exceeded\"]' "
            "WHEN json_extract(budget,'$[5]') OR json_extract(budget,'$[6]') "
            "THEN '[null,\"malformed\"]' "
            "WHEN json_extract(budget,'$[0]')=0 THEN '[null,\"absent\"]' "
            f"ELSE mcp_list_metadata('{header}', (SELECT json_group_array(raw_value) "
            f"FROM message_headers h WHERE h.claimant_id=cid AND h.name_normalized='{header}')) "
            f"END AS result FROM {name}_budget JOIN metadata_limits USING(cid))"
        )
    return ", ".join(parts)


def with_metadata(sql: str, names: tuple[str, ...]) -> str:
    if not names:
        return sql
    prefix = "WITH " + metadata_ctes(names) + ", "
    return prefix + sql[5:] if sql.startswith("WITH ") else prefix[:-2] + " " + sql


def requested_metadata(leaves, where, fields=(), group_by=None) -> tuple[str, ...]:
    # Avoid importing the predicate registry here (it imports this module).
    names = {
        leaf.name.removesuffix("_is")
        for leaf in [*leaves, *(w.leaf for w in where)]
        if leaf.name in ("list_id_is", "precedence_is")
    }
    names.update(
        name.removesuffix("_status")
        for name in fields
        if name.removesuffix("_status") in METADATA_HEADERS
    )
    if group_by in METADATA_HEADERS:
        names.add(group_by)
    return tuple(sorted(names))


def metadata_diagnostics(conn, names: tuple[str, ...]) -> dict[str, dict[str, int]]:
    if not names:
        return {}
    sql = " UNION ALL ".join(
        f"SELECT '{name}', json_extract(result,'$[1]') AS status, COUNT(*) "  # nosec B608
        f"FROM {name}_values GROUP BY status"
        for name in names
    )
    result: dict[str, dict[str, int]] = {name: {} for name in names}
    for name, status, count in conn.execute(with_metadata(sql, names)):
        result[name][status] = count
    return result
