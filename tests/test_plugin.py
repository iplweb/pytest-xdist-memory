import json

import pytest

from pytest_xdist_memory import profile

TESTS = """
import pytest

@pytest.mark.parametrize("i", range(6))
def test_many(i):
    pass

@pytest.mark.heavy
@pytest.mark.parametrize("i", range(3))
def test_heavy(i):
    pass
"""

INI = """
[pytest]
markers = heavy
"""


def _profile(pytester) -> dict:
    return json.loads((pytester.path / ".pytest-xdist-memory.json").read_text())


def test_inactive_by_default(pytester):
    pytester.makepyfile(TESTS)
    pytester.makeini(INI)
    result = pytester.runpytest("-n", "2")
    result.assert_outcomes(passed=9)
    result.stdout.no_fnmatch_line("*= xdist-memory =*")
    assert not (pytester.path / ".pytest-xdist-memory.json").exists()


def test_records_worker_peaks_per_suite(pytester):
    pytester.makepyfile(TESTS)
    pytester.makeini(INI)
    args = ("-n", "2", "--xdist-memory", "--xdist-memory-min-tests", "1")
    pytester.runpytest(*args).assert_outcomes(passed=9)
    # (xdist's controller does not report the deselected count)
    pytester.runpytest(*args, "-m", "heavy").assert_outcomes(passed=3)

    suites = _profile(pytester)["suites"]
    assert set(suites) == {"default", "heavy"}
    run = suites["default"]["runs"][0]
    assert run["workers"] == 2
    assert run["tests"] == 9
    assert len(run["worker_peaks_mb"]) == 2
    # A Python interpreter with pytest loaded: tens of MB at the very least.
    assert run["worker_peak_mb"] > 10
    assert run["controller_peak_mb"] > 10


def test_too_few_tests_are_not_recorded(pytester):
    pytester.makepyfile(TESTS)
    pytester.makeini(INI)
    result = pytester.runpytest("-n", "2", "--xdist-memory")  # min-tests 20
    result.assert_outcomes(passed=9)
    result.stdout.fnmatch_lines(["*= xdist-memory =*", "*not recorded (9 tests < 20*"])
    assert not (pytester.path / ".pytest-xdist-memory.json").exists()


def test_auto_uses_profile(pytester, monkeypatch):
    monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)
    pytester.makepyfile(TESTS)
    pytester.makeini(INI)
    # A worker "needing" more than this machine has: -n auto must give 1.
    profile.record_run(
        pytester.path / ".pytest-xdist-memory.json",
        "default",
        {"worker_peak_mb": 10_000_000, "controller_peak_mb": 100},
    )
    result = pytester.runpytest("-n", "auto", "--xdist-memory")
    result.assert_outcomes(passed=9)
    result.stdout.fnmatch_lines(["*-n auto [[]default[]]: 1 workers*from profile*"])


def test_env_override_wins(pytester, monkeypatch):
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", "2")
    pytester.makepyfile(TESTS)
    pytester.makeini(INI)
    result = pytester.runpytest("-n", "auto", "--xdist-memory")
    result.assert_outcomes(passed=9)
    result.stdout.fnmatch_lines(["*2 workers*"])
    result.stdout.no_fnmatch_line("*-n auto [[]default[]]*")


def test_child_processes_count_towards_the_worker(pytester):
    # Each test keeps a ~200 MB child process alive for a while: the worker
    # peak must include it (that is what Chromium / Daphne look like).
    pytester.makepyfile(
        """
        import subprocess, sys, time

        # Touch every page so it is really resident, then hold it.
        CHILD = (
            "b = bytearray(200 * 1024 * 1024)\\n"
            "for i in range(0, len(b), 4096): b[i] = 1\\n"
            "import time; time.sleep(3)"
        )

        def test_child():
            p = subprocess.Popen([sys.executable, "-c", CHILD])
            time.sleep(2.5)
            p.wait()
        """
    )
    result = pytester.runpytest(
        "-n",
        "1",
        "--xdist-memory",
        "--xdist-memory-min-tests",
        "1",
        "--xdist-memory-interval",
        "0.5",
    )
    result.assert_outcomes(passed=1)
    run = _profile(pytester)["suites"]["default"]["runs"][0]
    assert run["worker_peak_mb"] >= 200


def test_watchdog_kills_run_over_budget(pytester):
    # Reserve > all RAM: the limit drops to its 512 MB floor, and a 700 MB
    # child process must trip it.
    pytester.makepyfile(
        """
        import subprocess, sys, time

        CHILD = (
            "b = bytearray(700 * 1024 * 1024)\\n"
            "for i in range(0, len(b), 4096): b[i] = 1\\n"
            "import time; time.sleep(30)"
        )

        def test_hog():
            subprocess.Popen([sys.executable, "-c", CHILD]).wait()
        """
    )
    result = pytester.runpytest_subprocess(
        "-n",
        "1",
        "--xdist-memory",
        "--xdist-memory-watchdog",
        "--xdist-memory-reserve",
        "100000000",
        "--xdist-memory-interval",
        "0.5",
        timeout=60,
    )
    assert result.ret == 3
    result.stderr.fnmatch_lines(["*over the limit of 512 MB*killing the test run*"])


def test_estimate_takes_max_of_recent_runs():
    data = {
        "suites": {
            "s": {
                "runs": [
                    {"worker_peak_mb": 500, "controller_peak_mb": 100},
                    {"worker_peak_mb": 900, "controller_peak_mb": 150},
                    {"worker_peak_mb": 600, "controller_peak_mb": 120},
                ]
            }
        }
    }
    est = profile.estimate(
        data,
        "s",
        available_mb=10_000,
        reserve_mb=2_000,
        safety=1.0,
        cpu_cap=64,
        default_worker_mb=1,
        default_controller_mb=1,
        extra_per_worker_mb=100,
    )
    assert est.worker_mb == 900
    assert est.controller_mb == 150
    assert est.per_worker_mb == 1000
    assert est.workers == (10_000 - 2_000 - 150) // 1000


@pytest.mark.parametrize(
    "available, cap, expected", [(100, 8, 1), (100_000, 3, 3), (6_000, 64, 3)]
)
def test_estimate_bounds(available, cap, expected):
    est = profile.estimate(
        {},
        "default",
        available_mb=available,
        reserve_mb=1_000,
        safety=1.0,
        cpu_cap=cap,
        default_worker_mb=1_500,
        default_controller_mb=0,
        extra_per_worker_mb=0,
    )
    assert est.workers == expected
    assert not est.from_profile


def test_profile_keeps_last_runs(tmp_path):
    path = tmp_path / "p.json"
    for i in range(profile.KEEP_RUNS + 3):
        profile.record_run(path, "default", {"worker_peak_mb": i})
    runs = profile.load(path)["suites"]["default"]["runs"]
    assert [r["worker_peak_mb"] for r in runs] == list(range(3, profile.KEEP_RUNS + 3))


def test_docker_matching_by_port_and_name(monkeypatch):
    from pytest_xdist_memory import memory

    calls = []

    def fake_docker(*args):
        calls.append(args)
        if args[0] == "ps":
            return "mine-psql\n" if args[2] == "publish=5432" else ""
        names = args[4:] or ("mine-psql", "other-psql", "redis")
        usage = {"mine-psql": "300MiB", "other-psql": "1.5GiB", "redis": "20MiB"}
        return "".join(f"{n} {usage[n]} / 14GiB\n" for n in names)

    monkeypatch.setattr(memory.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(memory, "_docker", fake_docker)

    assert memory.docker_mb(port="5432") == 300
    assert calls[-1][4:] == ("mine-psql",)  # stats only for that container
    assert memory.docker_mb(match="psql") == 300 + 1536
    assert memory.docker_mb(port="9999") is None
    assert memory.docker_mb() is None
