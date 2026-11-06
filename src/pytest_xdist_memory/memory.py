"""Measuring memory: process subtrees, available RAM, docker containers."""

from __future__ import annotations

import shutil
import subprocess
import sys

import psutil

MB = 1024 * 1024

_UNITS = (("GiB", 1024.0), ("MiB", 1.0), ("KiB", 1 / 1024), ("GB", 1000.0))


def process_mb(proc: psutil.Process) -> int:
    """Memory of one process in MB.

    PSS on Linux: a fair share of pages shared with other processes. Browsers
    (Chromium runs several processes sharing most of their pages) would be
    counted several times over by summing RSS. Elsewhere RSS.
    """
    if sys.platform.startswith("linux"):
        try:
            return proc.memory_full_info().pss // MB
        except psutil.AccessDenied:
            pass  # no access to smaps -- fall back to RSS below
    return proc.memory_info().rss // MB


def subtree_mb(proc: psutil.Process) -> int:
    """Memory of a process and ALL its descendants (browsers, servers...)."""
    try:
        procs = [proc, *proc.children(recursive=True)]
    except psutil.NoSuchProcess:
        return 0
    total = 0
    for p in procs:
        try:
            total += process_mb(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue  # exited between listing and measuring
    return total


def available_mb() -> int:
    return psutil.virtual_memory().available // MB


def _parse_usage(usage: str) -> int | None:
    used = usage.split("/")[0].strip()
    for unit, mult in _UNITS:
        if used.endswith(unit):
            try:
                return int(float(used[: -len(unit)]) * mult)
            except ValueError:
                return None
    return None


def _docker(*args: str) -> str | None:
    try:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=20
        ).stdout
    except (subprocess.TimeoutExpired, OSError):
        return None


def docker_mb(match: str = "", port: str | None = None) -> int | None:
    """Total memory of running containers whose name contains ``match``
    and/or which publish host ``port``.

    ``None`` when docker is unavailable or nothing matches. Containers are
    not children of pytest (e.g. a database started by testcontainers), so
    they cannot be found by walking the process tree. Matching by the
    published port picks exactly THIS run's container even when parallel
    runs (other checkouts) have containers with similar names.
    """
    if not shutil.which("docker") or not (match or port):
        return None
    names: list[str] = []
    if port:
        out = _docker("ps", "--filter", f"publish={port}", "--format", "{{.Names}}")
        names = (out or "").split()
        if not names:
            return None
    out = _docker("stats", "--no-stream", "--format", "{{.Name}} {{.MemUsage}}", *names)
    if out is None:
        return None
    total = None
    for line in out.splitlines():
        name, _, usage = line.partition(" ")
        if match not in name:
            continue
        mb = _parse_usage(usage)
        if mb is not None:
            total = (total or 0) + mb
    return total


def cpu_cap(performance_cores: bool) -> int:
    """Upper bound on workers from the CPU.

    With ``performance_cores`` on Apple Silicon: only the performance cores.
    Efficiency cores are too slow to drive e.g. their own browser and only
    add contention.
    """
    cpus = psutil.cpu_count(logical=True) or 1
    if performance_cores and sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["sysctl", "-n", "hw.perflevel0.logicalcpu"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        except (subprocess.TimeoutExpired, OSError):
            return cpus
        if out.isdigit():
            return int(out)
    return cpus
