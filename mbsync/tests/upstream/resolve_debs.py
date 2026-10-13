"""Resolve the Depends/Pre-Depends closure of the requested Debian
packages on the host, for mbsync/tests/upstream/run.sh.

Inputs are files only: the base image's dpkg status and the Packages
indexes run.sh has already authenticated (InRelease signature, then the
signed SHA256). Packages already installed in the base at a version
that satisfies a dependency are skipped. Prints one line per .deb to
fetch:

    <suite-dir> <archive> <Filename> <SHA256> <Size> <Package> <Version>

It also writes the chosen packages' index stanzas, verbatim, to
<stanzas-out>: the Packages file of the flat local repository the
assembly container installs from. Recommends are not followed (as
apt-get --no-install-recommends).
Usage: resolve_debs.py <dpkg-status> <arch> <stanzas-out> <suite-dir>... -- <pkg>...
"""

import re
import sys
from pathlib import Path


def parse_stanzas(text):
    for block in text.split("\n\n"):
        fields = {}
        key = None
        for line in block.splitlines():
            if line.startswith((" ", "\t")) and key:
                fields[key] += "\n" + line
            elif ":" in line:
                key, _, val = line.partition(":")
                fields[key] = val.strip()
        if "Package" in fields:
            fields["_raw"] = block.strip("\n")
            yield fields


def _order(c):
    if c == "~":
        return -1
    if c.isdigit():
        return 0
    if c.isalpha():
        return ord(c)
    return ord(c) + 256


def _cmp_part(a, b):
    """dpkg's verrevcmp on one version part."""
    while a or b:
        na = re.match(r"^[^\d]*", a).group(0)
        nb = re.match(r"^[^\d]*", b).group(0)
        a, b = a[len(na) :], b[len(nb) :]
        for i in range(max(len(na), len(nb))):
            ca = _order(na[i]) if i < len(na) else 0
            cb = _order(nb[i]) if i < len(nb) else 0
            if ca != cb:
                return -1 if ca < cb else 1
        da = re.match(r"^\d*", a).group(0)
        db = re.match(r"^\d*", b).group(0)
        a, b = a[len(da) :], b[len(db) :]
        if int(da or 0) != int(db or 0):
            return -1 if int(da or 0) < int(db or 0) else 1
    return 0


def _split(v):
    epoch, rest = v.split(":", 1) if ":" in v else ("0", v)
    upstream, rev = rest.rsplit("-", 1) if "-" in rest else (rest, "0")
    return int(epoch), upstream, rev


def vcmp(a, b):
    ea, ua, ra = _split(a)
    eb, ub, rb = _split(b)
    if ea != eb:
        return -1 if ea < eb else 1
    return _cmp_part(ua, ub) or _cmp_part(ra, rb)


def satisfies(have, op, want):
    if op is None:
        return True
    c = vcmp(have, want)
    return {"<<": c < 0, "<=": c <= 0, "=": c == 0, ">=": c >= 0, ">>": c > 0}[op]


def parse_dep(s):
    out = []
    for clause in s.split(","):
        alts = []
        for alt in clause.split("|"):
            m = re.match(r"\s*([^\s(:]+)(?::\S+)?\s*(?:\(\s*([<>=]+)\s*([^)\s]+)\s*\))?", alt)
            if m:
                alts.append((m.group(1), m.group(2), m.group(3)))
        if alts:
            out.append(alts)
    return out


def main(argv):
    sep = argv.index("--")
    status_path, arch, stanzas_out = argv[0], argv[1], argv[2]
    suites, wanted = argv[3:sep], argv[sep + 1 :]

    installed, inst_provides = {}, set()
    for st in parse_stanzas(Path(status_path).read_text()):
        if st.get("Status", "").endswith(" installed"):
            installed[st["Package"]] = st["Version"]
            for p in parse_dep(st.get("Provides", "")):
                inst_provides.add(p[0][0])

    avail, provides = {}, {}
    for suite in suites:
        for st in parse_stanzas((Path(suite) / "Packages").read_text()):
            if st.get("Architecture") not in (arch, "all"):
                continue
            st["_suite"] = suite
            cur = avail.get(st["Package"])
            if cur is None or vcmp(st["Version"], cur["Version"]) > 0:
                avail[st["Package"]] = st
    for name, st in avail.items():
        for p in parse_dep(st.get("Provides", "")):
            provides.setdefault(p[0][0], []).append(name)

    chosen = {}

    def ok(name, op, ver):
        if name in chosen:
            return satisfies(chosen[name]["Version"], op, ver)
        if name in installed and satisfies(installed[name], op, ver):
            return True
        if op is None:
            return name in inst_provides or any(p in chosen for p in provides.get(name, []))
        return False

    pending = []
    for w in wanted:
        if w not in avail:
            sys.exit(f"not available: {w}")
        pending.append((w, None, None))

    while pending:
        name, op, ver = pending.pop()
        if ok(name, op, ver):
            continue
        st = avail.get(name)
        if st is None or not satisfies(st["Version"], op, ver):
            prov = provides.get(name, []) if op is None else []
            if not prov:
                sys.exit(f"unsatisfiable: {name} {op} {ver}")
            st = avail[prov[0]]
        chosen[st["Package"]] = st
        for field in ("Pre-Depends", "Depends"):
            for alts in parse_dep(st.get(field, "")):
                if any(ok(*a) for a in alts):
                    continue
                pick = next(
                    (
                        a
                        for a in alts
                        if (a[0] in avail and satisfies(avail[a[0]]["Version"], a[1], a[2]))
                        or (a[1] is None and provides.get(a[0]))
                    ),
                    None,
                )
                if pick is None:
                    sys.exit(f"unsatisfiable dependency of {st['Package']}")
                pending.append(pick)

    Path(stanzas_out).write_text("\n\n".join(chosen[n]["_raw"] for n in sorted(chosen)) + "\n")
    for name in sorted(chosen):
        st = chosen[name]
        archive = "debian-security" if "security" in Path(st["_suite"]).name else "debian"
        print(
            Path(st["_suite"]).name,
            archive,
            st["Filename"],
            st["SHA256"],
            st["Size"],
            name,
            st["Version"],
        )


if __name__ == "__main__":
    main(sys.argv[1:])
