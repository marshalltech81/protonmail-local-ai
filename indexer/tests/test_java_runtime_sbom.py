"""The shipped jlink runtime's inventory follows its build, never a version pin."""

import copy
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "java" / "runtime_sbom.py"


@pytest.fixture
def runtime_sbom():
    spec = importlib.util.spec_from_file_location("runtime_sbom", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _package(version, arch="arm64"):
    return {
        "Package": "openjdk-21-jre-headless",
        "Version": version,
        "Source": "openjdk-21",
        "SourceVersion": version,
        "Architecture": arch,
        "Maintainer": "Synthetic Debian Java Team",
        "JdkVersion": version,
    }


@pytest.mark.parametrize("version", ["21.0.1+12-2", "21.0.12.1+1-1~deb13u1", "1:21.0.12+1-2"])
def test_version_and_provenance_follow_the_builder(runtime_sbom, version):
    bom = runtime_sbom.make_sbom(
        _package(version),
        {"ID": "debian", "VERSION_ID": "13"},
        {"JAVA_VERSION": "21.0.12.1", "MODULES": "java.base java.desktop"},
    )
    (runtime,) = [c for c in bom["components"] if c["type"] != "operating-system"]
    assert runtime["version"] == version
    assert runtime["supplier"]["name"] == "Synthetic Debian Java Team"
    assert "jlink" in runtime["description"] and "subset" in runtime["description"]
    assert runtime["purl"].startswith("pkg:deb/debian/openjdk-21-jre-headless@")
    props = {p["name"]: p["value"] for p in runtime["properties"]}
    assert props["aquasecurity:trivy:SrcName"] == "openjdk-21"
    assert props["protonmail-local-ai:java-version"] == "21.0.12.1"
    assert props["protonmail-local-ai:modules"] == "java.base java.desktop"
    assert props["protonmail-local-ai:builder-jdk"] == "openjdk-21-jdk-headless@" + version
    assert len(bom["components"]) == 2  # no Maven, compiler or other builder packages
    assert bom["dependencies"][0]["dependsOn"] == [runtime["bom-ref"]]


def test_missing_build_metadata_fails_instead_of_inventing_provenance(runtime_sbom):
    with pytest.raises((KeyError, ValueError)):
        runtime_sbom.make_sbom(_package("21.0.1+12-2"), {"ID": "debian"}, {})


def test_mismatched_builder_jdk_is_not_claimed_as_provenance(runtime_sbom):
    package = _package("21.0.1+12-2")
    package["JdkVersion"] = "21.0.2+13-1"
    with pytest.raises(ValueError, match="package versions differ"):
        runtime_sbom.make_sbom(
            package,
            {"ID": "debian", "VERSION_ID": "13"},
            {"JAVA_VERSION": "21.0.1", "MODULES": "java.base"},
        )


@pytest.fixture
def sbom_check():
    path = SCRIPT.parents[2] / "scripts/tests/jlink_sbom_check.py"
    spec = importlib.util.spec_from_file_location("jlink_sbom_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_enrichment_keeps_every_existing_image_component_and_dependency(runtime_sbom, sbom_check):
    runtime = runtime_sbom.make_sbom(
        _package("21.0.1+12-2"),
        {"ID": "debian", "VERSION_ID": "13"},
        {"JAVA_VERSION": "21.0.1", "MODULES": "java.base"},
    )
    image = copy.deepcopy(runtime)
    unrelated = {"type": "library", "name": "synthetic-python-package", "version": "1"}
    image["components"].append(unrelated)
    found = sbom_check.runtime_component(image)
    found.pop("description")
    found.pop("supplier")
    found["properties"] = [p for p in found["properties"] if p["name"].startswith("aquasecurity:")]
    dependencies = copy.deepcopy(image["dependencies"])
    sbom_check.enrich(image, runtime)
    assert image["components"][-1] == unrelated
    assert image["dependencies"] == dependencies
    assert found == sbom_check.runtime_component(runtime)
    before = copy.deepcopy(image)
    sbom_check.enrich(image, runtime)
    assert image == before


@pytest.mark.parametrize("change", ["missing", "duplicate", "version", "source", "architecture"])
def test_ci_check_refuses_missing_or_different_inventory(runtime_sbom, sbom_check, change):
    runtime = runtime_sbom.make_sbom(
        _package("21.0.1+12-2"),
        {"ID": "debian", "VERSION_ID": "13"},
        {"JAVA_VERSION": "21.0.1", "MODULES": "java.base"},
    )
    image = copy.deepcopy(runtime)
    found = sbom_check.runtime_component(image)
    if change == "missing":
        image["components"].remove(found)
    elif change == "duplicate":
        image["components"].append(copy.deepcopy(found))
    elif change == "version":
        found["version"] = "21.0.2+13-1"
    elif change == "architecture":
        found["purl"] = found["purl"].replace("arm64", "amd64")
    else:
        found["properties"][0]["value"] = "synthetic-wrong-source"
    with pytest.raises(ValueError):
        sbom_check.enrich(image, runtime)
