"""Inventory the jlink runtime using its builder's Debian package metadata (#1008)."""

import json
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote


def read_release(path: Path) -> dict[str, str]:
    """Read trusted build metadata; never source it as shell code."""
    return dict(line.split("=", 1) for line in shlex.split(path.read_text(), comments=True))


def make_sbom(package: dict, os_release: dict, java_release: dict) -> dict:
    distro, distro_version = os_release["ID"], os_release["VERSION_ID"]
    name, version = package["Package"], package["Version"]
    source_version = package["SourceVersion"]
    if package["JdkVersion"] != version:
        raise ValueError("Java runtime and builder JDK package versions differ")
    epoch, sep, remainder = source_version.partition(":")
    epoch, remainder = (str(int(epoch)), remainder) if sep else ("0", source_version)
    upstream, sep, revision = remainder.rpartition("-")
    if not sep:
        upstream, revision = remainder, ""
    if not all(
        (
            name,
            version,
            upstream,
            package["Source"],
            java_release["JAVA_VERSION"],
            java_release["MODULES"],
            package["Maintainer"],
            distro,
            distro_version,
        )
    ):
        raise ValueError("incomplete Java runtime build metadata")
    purl = (
        f"pkg:deb/{quote(distro, safe='')}/{quote(name, safe='')}@"
        f"{quote(version, safe='')}?arch={quote(package['Architecture'], safe='')}"
        f"&distro={quote(distro + '-' + distro_version, safe='')}"
    )
    properties = {
        "aquasecurity:trivy:SrcName": package["Source"],
        "aquasecurity:trivy:SrcVersion": upstream,
        "aquasecurity:trivy:SrcRelease": revision,
        "aquasecurity:trivy:SrcEpoch": epoch,
        "protonmail-local-ai:derivation": "jlink",
        "protonmail-local-ai:runtime-path": "/opt/ppt/jre",
        "protonmail-local-ai:java-version": java_release["JAVA_VERSION"],
        "protonmail-local-ai:modules": java_release["MODULES"],
        "protonmail-local-ai:builder-jdk": "openjdk-21-jdk-headless@" + package["JdkVersion"],
    }
    runtime = {
        "type": "framework",
        "name": name,
        "version": version,
        "bom-ref": purl,
        "purl": purl,
        "supplier": {"name": package["Maintainer"]},
        "description": "Custom jlink runtime: a module subset derived from this Debian "
        "OpenJDK package, not the full installed package. Source-package "
        "CVE matching is conservative; omitted modules are not excluded.",
        "properties": [{"name": k, "value": v} for k, v in properties.items()],
    }
    os_ref = f"{distro}-{distro_version}"
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "components": [
            {
                "type": "operating-system",
                "name": distro,
                "version": distro_version,
                "bom-ref": os_ref,
            },
            runtime,
        ],
        "dependencies": [{"ref": os_ref, "dependsOn": [purl]}],
    }


def main() -> None:
    runtime = Path(sys.argv[1])
    fields = (
        "Package",
        "Version",
        "source:Package",
        "source:Version",
        "Architecture",
        "Maintainer",
    )
    values = subprocess.check_output(
        [
            "dpkg-query",
            "-W",
            "-f=" + "\\t".join("${" + f + "}" for f in fields),
            "openjdk-21-jre-headless",
        ],
        text=True,
    ).split("\t")
    package = dict(
        zip(
            ("Package", "Version", "Source", "SourceVersion", "Architecture", "Maintainer"),
            values,
            strict=True,
        )
    )
    package["JdkVersion"] = subprocess.check_output(
        ["dpkg-query", "-W", "-f=${Version}", "openjdk-21-jdk-headless"],
        text=True,
    )
    bom = make_sbom(
        package, read_release(Path("/etc/os-release")), read_release(runtime / "release")
    )
    (runtime / "runtime.cdx.json").write_text(json.dumps(bom, indent=2) + "\n")


if __name__ == "__main__":
    main()
