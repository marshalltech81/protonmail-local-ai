"""Launcher that starts every external extraction tool under limits.

Run by ``_runner.run_tool`` as
``python -I _launcher.py <address space> <cpu seconds> <tool> <args...>``.
It lowers its own address-space and CPU limits to the values the
caller passes (each extractor's ``CHILD_MAX_ADDRESS_SPACE_BYTES`` and
``CHILD_MAX_CPU_SECONDS``), then replaces itself with the tool through
``os.execve``, so the limits hold for the tool from its first
instruction, before it reads the payload (#957, #995). A launcher
rather than ``preexec_fn``: the indexer is multi-threaded, and code
between ``fork`` and ``exec`` in a threaded parent can deadlock. It
imports only the standard library (``-I`` keeps this directory off
``sys.path``).
"""

from __future__ import annotations

import os
import resource
import sys


def limit_resources(address_space: int, cpu_seconds: int) -> None:  # pragma: no cover — child only
    """Lower this process's limits; raises when Linux refuses one.

    macOS refuses to lower ``RLIMIT_AS`` from its unlimited default
    (``ValueError``), and the image runs on Linux only, so there the
    address-space limit is skipped for local test runs; CI and the
    image apply it."""
    try:
        resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
    except ValueError:
        if sys.platform == "linux":
            raise
    # Past the soft limit the kernel sends SIGXCPU; past the hard one,
    # SIGKILL. CPU time counts every thread of the tool.
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))


def tool_environment(inherited: dict[str, str]) -> dict[str, str]:  # pragma: no cover — child only
    """The tool's environment: the runner's minimal one, plus two glibc
    malloc arenas. glibc reserves address space for an arena per thread
    up to eight per CPU, so on a many-core host the JVM's native
    allocations failed under the address-space limit in about one start
    in twenty (measured: 9 of 150 on 18 CPUs; 0 of 150 with the cap).
    A single-threaded tool (catdoc, the xls child) uses one arena
    either way."""
    return {**inherited, "MALLOC_ARENA_MAX": "2"}


def main(argv: list[str]) -> None:  # pragma: no cover — runs only in the child
    limit_resources(int(argv[1]), int(argv[2]))
    # The caller's fixed absolute tool path and arguments; no shell.
    os.execve(argv[3], argv[3:], tool_environment(dict(os.environ)))  # nosec B606


if __name__ == "__main__":  # pragma: no cover — runs only in the child
    main(sys.argv)
