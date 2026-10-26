"""Verify runtime inventory and real Trivy CVE matching, then retain its provenance."""

import copy
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote


def runtime_component(bom: dict) -> dict:
    matches = [c for c in bom["components"] if c["name"] == "openjdk-21-jre-headless"]
    if len(matches) != 1:
        raise ValueError("expected exactly one Java runtime component")
    return matches[0]


def enrich(image: dict, runtime: dict) -> None:
    """Keep Trivy's package identifiers and every existing image component."""
    expected = runtime_component(runtime)
    found = runtime_component(image)
    if found["version"] != expected["version"] or found["purl"] != expected["purl"]:
        raise ValueError("Java runtime SBOM version differs from the build")
    properties = {p["name"]: p["value"] for p in found.get("properties", [])}
    source = {p["name"]: p["value"] for p in expected["properties"]}
    for name in ("SrcName", "SrcVersion", "SrcRelease", "SrcEpoch"):
        key = "aquasecurity:trivy:" + name
        if properties.get(key, "0" if name == "SrcEpoch" else "") != source[key]:
            raise ValueError("Java runtime SBOM source provenance differs from the build")
    # Trivy's package model does not retain arbitrary component descriptions
    # and derivation properties when it re-emits embedded inventory.
    found["description"] = expected["description"]
    found["supplier"] = expected["supplier"]
    found["properties"] = [
        p for p in found.get("properties", []) if not p["name"].startswith("protonmail-local-ai:")
    ]
    found["properties"].extend(
        p for p in expected["properties"] if p["name"].startswith("protonmail-local-ai:")
    )


def matching_probe(runtime: dict, trivy: str) -> None:
    """A synthetic old version must match a known fixed OpenJDK CVE."""
    probe = copy.deepcopy(runtime)
    component = runtime_component(probe)
    old_ref = component["bom-ref"]
    old_version = "21.0.1+12-2"
    prefix, suffix = component["purl"].split("@", 1)
    purl = prefix + "@" + quote(old_version, safe="") + "?" + suffix.split("?", 1)[1]
    component.update(version=old_version, purl=purl)
    component["bom-ref"] = purl
    for dependency in probe["dependencies"]:
        dependency["dependsOn"] = [
            purl if ref == old_ref else ref for ref in dependency["dependsOn"]
        ]
    versions = {"SrcVersion": "21.0.1+12", "SrcRelease": "2", "SrcEpoch": "0"}
    for prop in component["properties"]:
        for name, value in versions.items():
            if prop["name"] == "aquasecurity:trivy:" + name:
                prop["value"] = value
    with tempfile.TemporaryDirectory(prefix="jlink-sbom-") as tmp:
        path, report = Path(tmp) / "probe.cdx.json", Path(tmp) / "report.json"
        path.write_text(json.dumps(probe))
        subprocess.run(
            [
                trivy,
                "sbom",
                "--skip-db-update",
                "--scanners",
                "vuln",
                "--format",
                "json",
                "--output",
                str(report),
                "--exit-code",
                "0",
                str(path),
            ],
            check=True,
            timeout=120,
        )
        findings = [
            v
            for result in json.loads(report.read_text()).get("Results", [])
            for v in result.get("Vulnerabilities") or []
        ]
        if not any(
            v["VulnerabilityID"] == "CVE-2024-20918"
            and v["PkgName"] == component["name"]
            and v["InstalledVersion"] == old_version
            and v.get("FixedVersion")
            for v in findings
        ):
            raise ValueError("Trivy did not match the synthetic obsolete runtime's fixed JDK CVE")
    print("Java runtime inventory and Debian OpenJDK CVE matching verified")


def main() -> None:
    image_path, runtime_path = map(Path, sys.argv[1:3])
    image, runtime = json.loads(image_path.read_text()), json.loads(runtime_path.read_text())
    enrich(image, runtime)
    matching_probe(runtime, sys.argv[3])
    image_path.write_text(json.dumps(image, indent=2) + "\n")


if __name__ == "__main__":
    main()
