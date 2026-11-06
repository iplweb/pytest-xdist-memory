# pytest-xdist-memory

Size [pytest-xdist](https://github.com/pytest-dev/pytest-xdist) by **available
RAM**, not by CPU count.

On a developer laptop the bottleneck of a parallel test run is usually memory.
`pytest -n auto` starts one worker per core; with heavy workers (Django and
a database, a browser per worker for Playwright, a live server...) a 14-core,
14 GB laptop goes into swap thrashing and freezes. This plugin measures how
much a worker of *your* test suite needs on *this* machine and answers
`-n auto` with as many workers as fit into the memory available right now.

> Status: alpha.

## What it does

With `--xdist-memory` (or `xdist_memory = true` in the ini file):

- **Measures** the peak memory of every worker **together with all its child
  processes**: browsers, live servers, subprocesses. Linux uses PSS (a fair
  share of pages shared between processes, so the several processes of one
  Chromium are not counted several times over); other systems use RSS.
- **Measures containers** too, if you ask it to (`--xdist-memory-docker
  NAME`): a test database started by testcontainers is not a child of pytest.
  The peak is divided by the number of workers.
- **Records** the peaks in a per-machine profile, `.pytest-xdist-memory.json`.
  Each `-m` marker expression is a separate "suite": `-m playwright` workers
  are much heavier than `-m "not playwright"` ones. The last 5 runs are kept,
  and the estimate uses the maximum. Runs with fewer than 20 tests, or
  interrupted runs, are not recorded.
- **Answers `-n auto` / `-n logical`**:
  `workers = (available - reserve - controller) / (worker_peak × safety + extra)`,
  capped by the CPU count. With `--xdist-memory-performance-cores`, Apple
  Silicon is capped at the number of performance cores.
- **Optional watchdog** (`--xdist-memory-watchdog`): kills the run when the
  whole process tree, plus the measured containers, exceeds the memory
  available at start minus the reserve.

Nothing changes until you enable it. `PYTEST_XDIST_AUTO_NUM_WORKERS` still
wins over the estimate.

## Usage

```bash
pip install pytest-xdist-memory

# First run: no profile yet, so it assumes 1024 MB per worker; the run is measured.
pytest -n auto --xdist-memory

# Later runs use what was measured (per -m expression):
pytest -n auto --xdist-memory -m "not playwright"
pytest -n auto --xdist-memory -m playwright --xdist-memory-performance-cores
```

The terminal summary shows the measured peaks and the calculation:

```
================================ xdist-memory =================================
-n auto [default]: 3 workers: available 7282 MB - reserve 2048 - controller 215 = 5019 MB; per worker 1357 MB (910 x safety + extra 220); cpu cap 14; from profile (4 runs)
peak per worker 905 MB (all: [905, 871, 860]), controller 210 MB, containers 214 MB/worker
recorded in .pytest-xdist-memory.json [default]
```

Add `.pytest-xdist-memory.json` to `.gitignore`: the profile describes one
machine.

## Options

| Option | ini | Default | |
|---|---|---|---|
| `--xdist-memory` | `xdist_memory` | off | enable |
| `--xdist-memory-profile PATH` | `xdist_memory_profile` | `.pytest-xdist-memory.json` | relative to rootdir |
| `--xdist-memory-reserve MB` | `xdist_memory_reserve` | 2048 | left for the desktop/system |
| `--xdist-memory-safety F` | `xdist_memory_safety` | 1.25 | multiplier on the worker peak |
| `--xdist-memory-default-worker MB` | `xdist_memory_default_worker` | 1024 | used until a run is recorded |
| `--xdist-memory-extra MB` | `xdist_memory_extra` | 0 | fixed extra per worker (e.g. its DB) |
| `--xdist-memory-docker SUBSTR` | `xdist_memory_docker` | | measure matching containers |
| `--xdist-memory-performance-cores` | `xdist_memory_performance_cores` | off | Apple Silicon: P-cores only |
| `--xdist-memory-min-tests N` | `xdist_memory_min_tests` | 20 | smaller runs are not recorded |
| `--xdist-memory-watchdog` | `xdist_memory_watchdog` | off | kill the run over budget |
| `--xdist-memory-interval S` | `xdist_memory_interval` | 2 | sampling interval |

## Watchdog vs. a cgroup limit

The watchdog is portable but **best effort**: it checks once per sampling
interval, so a very fast allocation spike can get past it. When it fires, it
kills the workers and exits with code 3, without pytest's teardown: containers
are left to their own reaper (Ryuk for testcontainers).

On Linux with systemd, a cgroup limit is the hard guarantee: the kernel kills
the run instead of swapping the desktop to death. You can use both:

```bash
systemd-run --user --scope -q -p MemoryMax=6G -p MemorySwapMax=0 \
    pytest -n auto --xdist-memory
```

**Neither is needed for measuring.** Measuring uses only `psutil` (and
`docker stats`, if you ask for containers).

## Requirements

Python ≥ 3.10, pytest ≥ 7, psutil. pytest-xdist is optional: without it, the
plugin only reports the peak of the whole process tree.

## License

MIT
