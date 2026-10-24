"""The analysis child: ``python -m earshot.signals.child [options] JOB_DIR``.

One process per job, started by ``signals.runner.AnalysisProcess`` in a session of
its own. Before anything else it caps itself: no core files (a core of this process
holds call audio), and on Linux ``RLIMIT_AS`` (macOS refuses an address-space limit,
so a macOS child is not capped). It lowers its priority (``nice 10``) so the server
answers first, and exits as soon as its parent is gone.

``JOB_DIR`` holds ``job.json`` and the recording(s) the server copied there. The
child writes ``result.json``, ``peaks.dat`` and, for a mono pair, ``rendition.flac``
next to them, and exits 0; a recording it cannot analyse exits 65 with
``error.json`` naming the code; a missing ``signals`` extra exits 69; a failed
allocation exits 71. Anything else (a crash, a signal) is a failed attempt the
server retries.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

from ..crash_points import crash_point, hold_point, stall_point
from . import ANALYSIS_MODEL_MISMATCH, extra_available
from .runner import (
    EXIT_EXTRA_REQUIRED,
    EXIT_OK,
    EXIT_OUT_OF_MEMORY,
    EXIT_PARENT_GONE,
    EXIT_UNAVAILABLE,
)

PARENT_POLL_SECONDS = 1.0


def _limit(memory_limit_bytes: int) -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if sys.platform.startswith("linux") and memory_limit_bytes > 0:
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit_bytes, memory_limit_bytes))


def _watch_parent(parent_pid: int) -> None:
    """Exit at once if the server that started this child is gone (re-parented)."""

    if os.getppid() != parent_pid:
        os._exit(EXIT_PARENT_GONE)

    def watch() -> None:
        while True:
            time.sleep(PARENT_POLL_SECONDS)
            if os.getppid() != parent_pid:
                os._exit(EXIT_PARENT_GONE)

    threading.Thread(target=watch, name="earshot-analysis-parent", daemon=True).start()


def _write_json(path: Path, value: object) -> None:
    partial = path.with_suffix(".partial")
    partial.write_text(json.dumps(value, separators=(",", ":"), sort_keys=True))
    partial.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="earshot.signals.child")
    parser.add_argument("--memory-limit-bytes", type=int, default=0)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("job_dir", type=Path)
    arguments = parser.parse_args(argv)
    _limit(arguments.memory_limit_bytes)
    os.nice(10)
    _watch_parent(arguments.parent_pid)
    crash_point("analysis_child_running")
    stall_point("analysis_child_running")
    job_dir: Path = arguments.job_dir
    try:
        from .analyzer import analyze_file
        from .audio import DecodeError
        from .rendition import write_rendition
        from .vad import ModelMismatchError, SileroModel
    except ImportError:
        if not extra_available():
            return EXIT_EXTRA_REQUIRED
        raise  # found but not importable (a broken install, or no memory): a crash
    job = json.loads((job_dir / "job.json").read_text())
    try:
        model = SileroModel()
        result = analyze_file(
            job_dir / job["input"],
            speakers=list(job["speakers"]),
            peaks_path=job_dir / "peaks.dat",
            content_type=job.get("content_type"),
            model=model,
        )
        rendition = job.get("rendition")
        if rendition is not None:
            try:
                write_rendition(
                    [job_dir / name for name in rendition["inputs"]],
                    list(rendition["content_types"]),
                    job_dir / "rendition.flac",
                    tolerance_ms=int(rendition["tolerance_ms"]),
                )
            except DecodeError:
                (job_dir / "rendition.flac").unlink(missing_ok=True)
    except DecodeError as error:
        _write_json(job_dir / "error.json", {"code": error.code})
        return EXIT_UNAVAILABLE
    except ModelMismatchError:
        _write_json(job_dir / "error.json", {"code": ANALYSIS_MODEL_MISMATCH})
        return EXIT_UNAVAILABLE
    except MemoryError:
        return EXIT_OUT_OF_MEMORY
    _write_json(job_dir / "result.json", result)
    hold_point("analysis_child_finished")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
