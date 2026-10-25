"""The isync configuration the driver uses is the repository's own render,
with every difference from production listed (#1759, procedure v5 1.8)."""

import difflib
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "evidence-run" / "evidence_run" / "render_isync_config.sh"
GOLDEN = REPO / "mbsync" / "tests" / "mbsyncrc.golden"
USER = "synthetic@example.com"  # the golden file's synthetic user

# The complete list of differences from production's render: the Tunnel
# (Bridge listens on the guest's own loopback, no socat), the secret file,
# the certificate file, and the Maildir the run writes to.
EXPECTED_DIFFERENCES = [
    (
        'Tunnel "exec socat - TCP:host.docker.internal:1143"',
        "Port 1143",
    ),
    ('PassCmd "cat /run/secrets/bridge_pass"', 'PassCmd "cat /scratch/imap_pass"'),
    ("CertificateFile /tmp/mbsync/bridge-cert.pem", "CertificateFile /scratch/bridge-cert.pem"),
    ("Path /maildir/", "Path /scratch/maildir/"),
    ("Inbox /maildir/INBOX", "Inbox /scratch/maildir/INBOX"),
]


def render(tmp_path: Path, **overrides: str) -> subprocess.CompletedProcess:
    out = tmp_path / "isyncrc"
    env = {
        "PATH": os.environ["PATH"],
        "BRIDGE_USER": USER,
        "EVIDENCE_IMAP_PORT": "1143",
        "EVIDENCE_PASS_FILE": "/scratch/imap_pass",
        "EVIDENCE_CERT_FILE": "/scratch/bridge-cert.pem",
        "EVIDENCE_MAILDIR": "/scratch/maildir",
        **overrides,
    }
    return subprocess.run(
        ["bash", str(SCRIPT), str(out)], env=env, capture_output=True, text=True, check=False
    )


def test_the_only_differences_from_the_production_render_are_the_listed_five(tmp_path):
    done = render(tmp_path)
    assert done.returncode == 0, done.stderr
    golden = GOLDEN.read_text().splitlines()
    ours = (tmp_path / "isyncrc").read_text().splitlines()
    removed, added = [], []
    for line in difflib.unified_diff(golden, ours, lineterm="", n=0):
        if line.startswith("---") or line.startswith("+++") or line.startswith("@@"):
            continue
        (removed if line.startswith("-") else added).append(line[1:])
    assert list(zip(removed, added, strict=True)) == EXPECTED_DIFFERENCES
    assert len(removed) == len(added) == len(EXPECTED_DIFFERENCES)


def test_the_safety_settings_of_production_are_kept(tmp_path):
    assert render(tmp_path).returncode == 0
    text = (tmp_path / "isyncrc").read_text()
    for line in (
        "TLSType IMAPS",
        "Sync Pull",
        "Expunge None",
        "SyncState *",
        "SubFolders Legacy",
        "CopyArrivalDate yes",
    ):
        assert line in text.splitlines(), line
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert not any("starttls" in line.lower() for line in code)
    assert (tmp_path / "isyncrc").stat().st_mode & 0o777 == 0o600


def test_a_template_change_that_moves_a_replaced_line_is_refused(tmp_path):
    template = tmp_path / "mbsyncrc.template"
    template.write_text(
        (REPO / "mbsync" / "mbsyncrc.template").read_text().replace("Inbox /maildir/INBOX\n", "")
    )
    done = render(tmp_path, TEMPLATE_FILE=str(template))
    assert done.returncode != 0
    assert not (tmp_path / "isyncrc").exists()


@pytest.mark.parametrize(
    "name, value",
    [
        ("EVIDENCE_IMAP_PORT", "0"),
        ("EVIDENCE_IMAP_PORT", "70000"),
        ("EVIDENCE_IMAP_PORT", "11a"),
        ("EVIDENCE_PASS_FILE", "relative/path"),
        ("EVIDENCE_PASS_FILE", '/scratch/a" b'),
        ("EVIDENCE_CERT_FILE", "/scratch/with space"),
        ("EVIDENCE_MAILDIR", "/scratch/a\nb"),
        ("BRIDGE_USER", ""),
    ],
)
def test_unsafe_values_are_refused_before_anything_is_written(tmp_path, name, value):
    done = render(tmp_path, **{name: value})
    assert done.returncode != 0
    assert not (tmp_path / "isyncrc").exists()
    assert value not in done.stderr or value == ""  # a refusal never repeats the value
