"""pytest plugin: size pytest-xdist by RAM, not by CPU count.

Why this exists: on a developer machine (laptop with a desktop) the
bottleneck of a parallel test run is usually memory, not CPU. ``-n auto``
spawns one worker per core; with heavy workers (Django + a database,
a browser per worker for Playwright) that is enough to push the machine
into swap thrashing and freeze it.

The plugin, once enabled with ``--xdist-memory``:

* measures, in every worker, the peak memory of the worker AND all of its
  descendants (browsers, live servers...) -- PSS on Linux, RSS elsewhere;
* optionally measures docker containers by name or by published port (a test
  database started by testcontainers is not a child of pytest);
* records the peaks in a per-machine profile file, separately per ``-m``
  marker expression ("suite");
* answers ``-n auto`` / ``-n logical`` with as many workers as fit into the
  currently available RAM (capped by CPU count);
* optionally (``--xdist-memory-watchdog``) aborts the run when the whole
  process tree exceeds the budget -- a portable, best-effort stand-in for a
  cgroup limit (``systemd-run --scope -p MemoryMax=...``).

Nothing changes until you enable it.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any

import psutil
import pytest

from . import memory, profile

_KEY = "xdist_memory"


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("xdist-memory", "size pytest-xdist by available RAM")
    group.addoption(
        "--xdist-memory",
        action="store_true",
        default=None,
        help="measure worker memory, record it in the profile and answer "
        "-n auto from it (ini: xdist_memory).",
    )
    group.addoption(
        "--xdist-memory-profile",
        default=None,
        metavar="PATH",
        help="profile file, relative to rootdir; keep it out of version "
        "control (ini: xdist_memory_profile; default .pytest-xdist-memory.json).",
    )
    group.addoption(
        "--xdist-memory-reserve",
        type=int,
        default=None,
        metavar="MB",
        help="memory to leave for the desktop / system "
        "(ini: xdist_memory_reserve; default 2048).",
    )
    group.addoption(
        "--xdist-memory-safety",
        type=float,
        default=None,
        metavar="FACTOR",
        help="multiplier on the measured worker peak "
        "(ini: xdist_memory_safety; default 1.25).",
    )
    group.addoption(
        "--xdist-memory-default-worker",
        type=int,
        default=None,
        metavar="MB",
        help="worker peak assumed while the profile has no runs for this suite "
        "(ini: xdist_memory_default_worker; default 1024).",
    )
    group.addoption(
        "--xdist-memory-extra",
        type=int,
        default=None,
        metavar="MB",
        help="fixed extra memory per worker outside the process tree, e.g. "
        "its database (ini: xdist_memory_extra; default 0).",
    )
    group.addoption(
        "--xdist-memory-docker",
        default=None,
        metavar="SUBSTRING",
        help="also measure running docker containers whose name contains "
        "SUBSTRING and count their peak / workers as extra per worker "
        "(ini: xdist_memory_docker).",
    )
    group.addoption(
        "--xdist-memory-docker-port-env",
        default=None,
        metavar="VAR",
        help="also measure the docker container publishing the host port in "
        "environment variable VAR, e.g. the one testcontainers exports for "
        "the test database; exact even with parallel runs "
        "(ini: xdist_memory_docker_port_env).",
    )
    group.addoption(
        "--xdist-memory-performance-cores",
        action="store_true",
        default=None,
        help="on Apple Silicon cap workers at the number of performance cores "
        "(ini: xdist_memory_performance_cores).",
    )
    group.addoption(
        "--xdist-memory-min-tests",
        type=int,
        default=None,
        metavar="N",
        help="record a run in the profile only if at least N tests ran "
        "(ini: xdist_memory_min_tests; default 20).",
    )
    group.addoption(
        "--xdist-memory-watchdog",
        action="store_true",
        default=None,
        help="kill the run when the process tree (+ measured containers) "
        "exceeds the memory available at start minus the reserve "
        "(ini: xdist_memory_watchdog).",
    )
    group.addoption(
        "--xdist-memory-interval",
        type=float,
        default=None,
        metavar="SECONDS",
        help="sampling interval (ini: xdist_memory_interval; default 2).",
    )
    for name, kind, default in (
        ("xdist_memory", "bool", False),
        ("xdist_memory_profile", "string", ".pytest-xdist-memory.json"),
        ("xdist_memory_reserve", "string", "2048"),
        ("xdist_memory_safety", "string", "1.25"),
        ("xdist_memory_default_worker", "string", "1024"),
        ("xdist_memory_extra", "string", "0"),
        ("xdist_memory_docker", "string", ""),
        ("xdist_memory_docker_port_env", "string", ""),
        ("xdist_memory_performance_cores", "bool", False),
        ("xdist_memory_min_tests", "string", "20"),
        ("xdist_memory_watchdog", "bool", False),
        ("xdist_memory_interval", "string", "2"),
    ):
        help_text = f"see --{name.replace('_', '-')}"
        parser.addini(name, help_text, type=kind, default=default)


class Settings:
    def __init__(self, config: pytest.Config) -> None:
        def opt(name: str, cast):
            value = config.getoption(name)
            if value is None:
                value = config.getini(name)
            return cast(value)

        self.enabled = opt("xdist_memory", bool)
        self.profile = config.rootpath / opt("xdist_memory_profile", str)
        self.reserve_mb = opt("xdist_memory_reserve", int)
        self.safety = opt("xdist_memory_safety", float)
        self.default_worker_mb = opt("xdist_memory_default_worker", int)
        self.extra_mb = opt("xdist_memory_extra", int)
        self.docker = opt("xdist_memory_docker", str)
        self.docker_port_env = opt("xdist_memory_docker_port_env", str)
        self.performance_cores = opt("xdist_memory_performance_cores", bool)
        self.min_tests = opt("xdist_memory_min_tests", int)
        self.watchdog = opt("xdist_memory_watchdog", bool)
        self.interval = opt("xdist_memory_interval", float)
        self.suite = profile.suite_key(config.getoption("markexpr", None))

    def estimate(self) -> profile.Estimate:
        return profile.estimate(
            profile.load(self.profile),
            self.suite,
            available_mb=memory.available_mb(),
            reserve_mb=self.reserve_mb,
            safety=self.safety,
            cpu_cap=memory.cpu_cap(self.performance_cores),
            default_worker_mb=self.default_worker_mb,
            # Unknown before the first measured run; the controller is a
            # plain pytest process, rarely more than a few hundred MB.
            default_controller_mb=250,
            extra_per_worker_mb=self.extra_mb,
        )


@pytest.hookimpl(tryfirst=True, optionalhook=True)
def pytest_xdist_auto_num_workers(config: pytest.Config) -> int | None:
    if os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS"):
        return None  # explicit override wins; let xdist's own hook read it
    settings = Settings(config)
    if not settings.enabled:
        return None
    est = settings.estimate()
    config.stash[_ESTIMATE] = est
    return est.workers


class Sampler:
    """Background thread recording the peak memory of a process."""

    def __init__(
        self,
        interval: float,
        subtree: bool,
        docker: str = "",
        docker_port_env: str = "",
    ) -> None:
        self.proc = psutil.Process()
        self.interval = interval
        self.subtree = subtree
        self.docker = docker
        self.docker_port_env = docker_port_env
        self.peak_mb = 0
        self.docker_peak_mb = 0
        self.on_sample = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="xdist-memory", daemon=True
        )

    def start(self) -> None:
        self.sample()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)
        self.sample()

    def sample(self) -> int:
        if self.subtree:
            mb = memory.subtree_mb(self.proc)
        else:
            mb = memory.process_mb(self.proc)
        self.peak_mb = max(self.peak_mb, mb)
        return mb

    def sample_docker(self) -> None:
        # Read the port at sample time: plugins (testcontainers) may export
        # it after we were configured.
        port = os.environ.get(self.docker_port_env) if self.docker_port_env else None
        if self.docker_port_env and not port:
            return
        mb = memory.docker_mb(self.docker, port) or 0
        self.docker_peak_mb = max(self.docker_peak_mb, mb)

    def _run(self) -> None:
        # docker stats is slow (~1 s); sample containers less often.
        docker_every = max(1, int(10 / max(self.interval, 0.1)))
        tick = 0
        while not self._stop.wait(self.interval):
            mb = self.sample()
            if (self.docker or self.docker_port_env) and tick % docker_every == 0:
                self.sample_docker()
            tick += 1
            if self.on_sample is not None:
                self.on_sample(mb)


class Watchdog:
    """Kill the run before it takes the machine down.

    Best effort: it reacts within one sampling interval, so a very fast
    allocation spike can still get through. A cgroup limit (``systemd-run
    --user --scope -p MemoryMax=...``) is the hard guarantee on Linux.
    """

    def __init__(self, settings: Settings, sampler: Sampler) -> None:
        self.sampler = sampler
        own = memory.process_mb(sampler.proc)
        self.limit_mb = max(memory.available_mb() - settings.reserve_mb + own, 512)

    def __call__(self, _own_mb: int) -> None:
        total = memory.subtree_mb(self.sampler.proc) + self.sampler.docker_peak_mb
        if total <= self.limit_mb:
            return
        sys.stderr.write(
            f"\npytest-xdist-memory: process tree uses {total} MB, over the "
            f"limit of {self.limit_mb} MB -- killing the test run.\n"
        )
        sys.stderr.flush()
        for child in self.sampler.proc.children(recursive=True):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                continue  # already gone
        os._exit(3)


def _is_worker(config: pytest.Config) -> bool:
    return hasattr(config, "workerinput")


def _is_controller(config: pytest.Config) -> bool:
    # Same test as xdist's own is_xdist_controller(); the "dsession" plugin is
    # not registered yet when our pytest_configure runs.
    return (
        not _is_worker(config)
        and getattr(config.option, "dist", "no") != "no"
        and bool(getattr(config.option, "tx", None))
    )


class Runtime:
    def __init__(self, config: pytest.Config, settings: Settings) -> None:
        self.config = config
        self.settings = settings
        self.worker = _is_worker(config)
        self.controller = _is_controller(config)
        # A worker (or a plain non-xdist run) measures its whole subtree; the
        # controller only itself, its children are the workers.
        self.sampler = Sampler(
            settings.interval,
            subtree=not self.controller,
            docker=settings.docker if not self.worker else "",
            docker_port_env=settings.docker_port_env if not self.worker else "",
        )
        self.workers: dict[str, dict[str, Any]] = {}
        self.tests = 0
        self.watchdog = None
        if settings.watchdog and not self.worker:
            self.watchdog = Watchdog(settings, self.sampler)
            self.sampler.on_sample = self.watchdog

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when == "call" or (report.when == "setup" and report.skipped):
            self.tests += 1

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node, error) -> None:
        data = getattr(node, "workeroutput", {}).get(_KEY)
        if data:
            self.workers[node.gateway.id] = data

    def finish(self, session: pytest.Session) -> None:
        self.sampler.stop()
        if self.worker:
            self.config.workeroutput[_KEY] = {"peak_mb": self.sampler.peak_mb}
            return
        if not self.controller:
            return  # plain run: nothing to size, just report in the summary
        peaks = sorted((w["peak_mb"] for w in self.workers.values()), reverse=True)
        if not peaks:
            return
        n = len(peaks)
        self.run = {
            "host": os.uname().nodename if hasattr(os, "uname") else "",
            "metric": "pss" if sys.platform.startswith("linux") else "rss",
            "workers": n,
            "tests": self.tests,
            "worker_peak_mb": peaks[0],
            "worker_peaks_mb": peaks,
            "controller_peak_mb": self.sampler.peak_mb,
            "extra_per_worker_mb": (self.sampler.docker_peak_mb // n) or None,
        }
        interrupted = session.shouldstop or session.shouldfail
        if self.tests >= self.settings.min_tests and not interrupted:
            profile.record_run(self.settings.profile, self.settings.suite, self.run)
            self.recorded = True


_RUNTIME = pytest.StashKey[Runtime]()
_ESTIMATE = pytest.StashKey[profile.Estimate]()


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    settings = Settings(config)
    if not settings.enabled:
        return
    runtime = Runtime(config, settings)
    config.stash[_RUNTIME] = runtime
    config.pluginmanager.register(runtime, "xdist-memory-runtime")
    runtime.sampler.start()


def pytest_sessionfinish(session: pytest.Session) -> None:
    runtime = session.config.stash.get(_RUNTIME, None)
    if runtime is not None:
        runtime.finish(session)


def pytest_terminal_summary(terminalreporter, exitstatus, config: pytest.Config):
    runtime = config.stash.get(_RUNTIME, None)
    if runtime is None or runtime.worker:
        return
    tr = terminalreporter
    tr.write_sep("=", "xdist-memory")
    est = config.stash.get(_ESTIMATE, None)
    if est is not None:
        tr.write_line(f"-n auto [{runtime.settings.suite}]: {est.describe()}")
    run = getattr(runtime, "run", None)
    if run is None:
        tr.write_line(f"peak (whole tree, no xdist): {runtime.sampler.peak_mb} MB")
        return
    extra = run["extra_per_worker_mb"]
    tr.write_line(
        f"peak per worker {run['worker_peak_mb']} MB (all: {run['worker_peaks_mb']}),"
        f" controller {run['controller_peak_mb']} MB"
        + (f", containers {extra} MB/worker" if extra else "")
    )
    path = Path(runtime.settings.profile)
    if getattr(runtime, "recorded", False):
        tr.write_line(f"recorded in {path.name} [{runtime.settings.suite}]")
    else:
        tr.write_line(
            f"not recorded ({runtime.tests} tests < "
            f"{runtime.settings.min_tests} or run interrupted)"
        )
    after = runtime.settings.estimate()
    tr.write_line(f"next -n auto would give: {after.describe()}")
