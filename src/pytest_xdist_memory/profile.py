"""The memory profile file and the worker-count calculation.

The profile is per machine (keep it out of version control). It holds, per
"suite" (the ``-m`` marker expression, or ``default``), the last few runs.
The estimate takes the MAXIMUM over them: one lucky light run should not let
the next one spawn too many workers.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VERSION = 1
KEEP_RUNS = 5


def suite_key(markexpr: str | None) -> str:
    return (markexpr or "").strip() or "default"


def load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {"version": VERSION, "suites": {}}
    except (OSError, ValueError) as exc:
        raise ProfileError(f"cannot read memory profile {path}: {exc}") from exc
    if data.get("version") != VERSION:
        return {"version": VERSION, "suites": {}}
    return data


class ProfileError(Exception):
    pass


def record_run(path: Path, key: str, run: dict[str, Any]) -> None:
    data = load(path)
    runs = data["suites"].setdefault(key, {"runs": []})["runs"]
    runs.append({"measured_at": time.strftime("%Y-%m-%d %H:%M:%S"), **run})
    del runs[:-KEEP_RUNS]
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)


@dataclass
class Estimate:
    workers: int
    from_profile: bool
    worker_mb: int
    controller_mb: int
    extra_per_worker_mb: int
    per_worker_mb: int
    available_mb: int
    reserve_mb: int
    budget_mb: int
    cpu_cap: int
    runs: int = 0
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        src = f"profile ({self.runs} runs)" if self.from_profile else "defaults"
        return (
            f"{self.workers} workers: available {self.available_mb} MB"
            f" - reserve {self.reserve_mb} - controller {self.controller_mb}"
            f" = {self.budget_mb} MB; per worker {self.per_worker_mb} MB"
            f" ({self.worker_mb} x safety + extra {self.extra_per_worker_mb});"
            f" cpu cap {self.cpu_cap}; from {src}"
        )


def _peak(runs: list[dict[str, Any]], name: str) -> int | None:
    values = [r[name] for r in runs if r.get(name)]
    return max(values) if values else None


def estimate(
    data: dict[str, Any],
    key: str,
    *,
    available_mb: int,
    reserve_mb: int,
    safety: float,
    cpu_cap: int,
    default_worker_mb: int,
    default_controller_mb: int,
    extra_per_worker_mb: int,
) -> Estimate:
    runs = data.get("suites", {}).get(key, {}).get("runs", [])
    worker = _peak(runs, "worker_peak_mb") or default_worker_mb
    controller = _peak(runs, "controller_peak_mb") or default_controller_mb
    measured_extra = _peak(runs, "extra_per_worker_mb")
    extra = max(extra_per_worker_mb, measured_extra or 0)
    per_worker = int(worker * safety) + extra
    budget = available_mb - reserve_mb - controller
    n = max(1, min(budget // max(per_worker, 1), cpu_cap))
    return Estimate(
        workers=n,
        from_profile=bool(runs),
        worker_mb=worker,
        controller_mb=controller,
        extra_per_worker_mb=extra,
        per_worker_mb=per_worker,
        available_mb=available_mb,
        reserve_mb=reserve_mb,
        budget_mb=budget,
        cpu_cap=cpu_cap,
        runs=len(runs),
    )
