"""Legacy PowerPoint ``.ppt`` extractor (#957).

A legacy ``.ppt`` is an OLE2 compound file. The dispatcher routes an
OLE2 payload labelled ``.ppt`` / ``application/vnd.ms-powerpoint`` here;
a ``.ppt``-labelled payload that is not OLE2 is recorded ``unsupported``
before it gets here (``NON_OLE2_PPT_ERROR``).

The text comes from Apache POI's HSLF reader (``SlideShowExtractor``,
slides and speaker notes) in a Java process: ``indexer/java/PptText.java``
on a trimmed Java runtime, both installed under ``PPT_HOME`` by
``indexer/Dockerfile``. The owner chose it on 2026-10-07 (#957): catppt
reads no slide text from decks current PowerPoint or LibreOffice save
(#958).

The JVM is run by ``_runner.run_tool``, whose launcher (``_launcher.py``,
``sys.executable -I``) caps its own address space and CPU time and caps
glibc's malloc arenas, then ``execv``s Java: no shell, a minimal environment,
a wall-clock timeout, stdout read up to ``_MAX_OUTPUT_BYTES`` and stderr
discarded (POI's errors and Log4j's "no provider" line can quote the
deck or are noise). Output past the byte cap is not indexed: the text
before it is kept and the cap is reported through
``warn_extractor_cap``. Any other failure (a limit, a parse error, a
crash) raises a fixed-text error from ``_runner``, recorded by type name
as a ``failed`` row.

Each deck costs one JVM start-up: on the fixture decks, measured in the
indexer image, 0.15 to 0.35 s wall time, 0.2 to 0.4 s of CPU and
60 to 70 MB peak RSS.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from . import warn_extractor_cap
from ._runner import ToolNotFoundError, run_tool

log = logging.getLogger("indexer.extractor.ppt")

# Where indexer/Dockerfile installs the Java runtime (``jre/``) and the
# jars (``lib/``).
PPT_HOME = Path("/opt/ppt")

# Address space (``RLIMIT_AS``) and CPU seconds (``RLIMIT_CPU``) the JVM
# may use. Measured in the indexer image under the Compose hardening: a
# JVM with the options below uses 60 to 70 MB RSS on the fixture decks,
# and under 512 MiB of address space it starts only with every one of
# the memory options below and the launcher's malloc arena cap.
CHILD_MAX_ADDRESS_SPACE_BYTES = 512 * 1024 * 1024
CHILD_MAX_CPU_SECONDS = 30

# Wall-clock seconds the JVM may run, past its CPU limit so a CPU-bound
# run meets that limit first.
PPT_TIMEOUT_SECONDS = 45.0

# Bytes of output read, about four times the dispatcher's default
# ``max_extracted_chars`` (2,000,000) of mostly one-byte UTF-8, so the
# dispatcher's cap still decides the stored length.
_MAX_OUTPUT_BYTES = 8 * 1024 * 1024

# JVM options. The heap, code cache, class space and metaspace sizes,
# the serial collector, C1-only compilation and no class-data sharing
# keep the JVM inside ``CHILD_MAX_ADDRESS_SPACE_BYTES`` (with the malloc
# arena cap ``_launcher`` sets); the heap bounds what POI may hold for
# one deck (past it the JVM fails the deck). The JVM's own messages
# (unified logging, warnings) would go to stdout and be indexed as the
# deck's text, so logging is off and the rest goes to stderr. No
# perf-data file, crash report or core file is written: the image's
# root filesystem is read-only, and a crash report or core would copy
# the deck's text to /tmp. Output is UTF-8 whatever the locale.
_JVM_OPTIONS = (
    "-Xmx128m",
    "-XX:ReservedCodeCacheSize=32m",
    "-XX:CompressedClassSpaceSize=64m",
    "-XX:MaxMetaspaceSize=64m",
    "-XX:+UseSerialGC",
    "-XX:TieredStopAtLevel=1",
    "-Xshare:off",
    "-Xlog:disable",
    "-XX:+DisplayVMOutputToStderr",
    "-XX:-UsePerfData",
    "-XX:+ErrorFileToStderr",
    "-XX:-CreateCoredumpOnCrash",
    "-Djava.awt.headless=true",
    "-Dfile.encoding=UTF-8",
    "-Dstdout.encoding=UTF-8",
)


def extract(
    payload: bytes,
    *,
    ocr_enabled: bool = True,  # noqa: ARG001
    max_ocr_pages: int = 20,  # noqa: ARG001
    ocr_timeout_seconds: float | None = None,  # noqa: ARG001
    max_pdf_pages: int | None = None,  # noqa: ARG001
    on_progress: Callable[[], None] | None = None,  # noqa: ARG001
) -> tuple[str, str]:
    """Extract text from a legacy ``.ppt`` payload. Returns (text, "ppt")."""
    java = PPT_HOME / "jre" / "bin" / "java"
    if not java.is_file():
        raise ToolNotFoundError
    output = run_tool(
        [
            str(java),
            *_JVM_OPTIONS,
            "-cp",
            str(PPT_HOME / "lib" / "*"),
            "PptText",
        ],
        payload,
        timeout_seconds=PPT_TIMEOUT_SECONDS,
        max_output_bytes=_MAX_OUTPUT_BYTES,
        max_address_space_bytes=CHILD_MAX_ADDRESS_SPACE_BYTES,
        max_cpu_seconds=CHILD_MAX_CPU_SECONDS,
        suffix=".ppt",
    )
    if output.truncated:
        warn_extractor_cap(
            log,
            "ppt_output_bytes",
            "ppt output cut at %d bytes",
            _MAX_OUTPUT_BYTES,
        )
    # A cut can split a UTF-8 sequence; the reader writes valid UTF-8 otherwise.
    return output.data.decode("utf-8", errors="replace"), "ppt"
