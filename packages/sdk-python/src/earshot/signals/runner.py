"""The analysis child process, seen from the server: start it, bound it, read its answer.

Each job runs in a new process, ``python -m earshot.signals.child``, in its own
session (``start_new_session=True``, so its process group is its own), with no
``preexec_fn`` (unsafe in a threaded server): the child applies its own limits
(``signals.child``). The server waits at most the job's timeout, then kills the
whole process group. Closing (the server stopping) kills a running child; that job
ends with ``AnalysisClosedError`` and its lease is released at the next start.

This module imports only the standard library.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from . import (
    ANALYSIS_CRASHED,
    ANALYSIS_DECODE_FAILED,
    ANALYSIS_MODEL_MISMATCH,
    ANALYSIS_NO_AUDIO,
    ANALYSIS_OUT_OF_MEMORY,
    ANALYSIS_TIMEOUT,
    ANALYSIS_UNSUPPORTED_CODEC,
    SIGNALS_EXTRA_REQUIRED,
)

# The child's exit statuses (``sysexits.h`` where one fits).
EXIT_OK = 0
EXIT_UNAVAILABLE = 65  # EX_DATAERR: the recording cannot be analysed; error.json names why
EXIT_EXTRA_REQUIRED = 69  # EX_UNAVAILABLE: the signals extra is not installed
EXIT_OUT_OF_MEMORY = 71  # EX_OSERR: an allocation failed under the memory limit
EXIT_PARENT_GONE = 75  # EX_TEMPFAIL: the server that started it is gone
# A Linux child may use this much address space (``RLIMIT_AS``). macOS cannot cap it
# (``setrlimit(RLIMIT_AS)`` is refused there), so a macOS child is not capped.
MEMORY_LIMIT_BYTES = 4 * 1024**3
# What a child may report as the reason a recording is unavailable.
UNAVAILABLE_CODES = frozenset(
    {
        ANALYSIS_DECODE_FAILED,
        ANALYSIS_UNSUPPORTED_CODEC,
        ANALYSIS_NO_AUDIO,
        ANALYSIS_MODEL_MISMATCH,
        "EARSHOT_ANALYSIS_CHANNEL_MISMATCH",
    }
)
# One thread per numeric library: the analysis runs on one core, deterministically.
_ONE_THREAD = {
    name: "1"
    for name in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    )
}


class AnalysisClosedError(RuntimeError):
    """A run after the analysis process was closed (the server is stopping)."""


@dataclass(frozen=True, slots=True)
class ChildOutcome:
    """How one child ended: ``error_code`` is ``None`` for a result; ``terminal``
    says whether the job is over (``unavailable``) or retried."""

    error_code: str | None
    terminal: bool = False
    returncode: int | None = None


def _package_root() -> str:
    """The directory holding the ``earshot`` package this process runs, so the child
    imports the same code."""

    return str(Path(__file__).resolve().parents[2])


def child_outcome(returncode: int, job_dir: Path) -> ChildOutcome:
    """What a child's exit status (and, for 65, its ``error.json``) means."""

    if returncode == EXIT_OK:
        if (job_dir / "result.json").is_file() and (job_dir / "peaks.dat").is_file():
            return ChildOutcome(None, returncode=returncode)
        return ChildOutcome(ANALYSIS_CRASHED, returncode=returncode)
    if returncode == EXIT_UNAVAILABLE:
        code = ANALYSIS_DECODE_FAILED
        with contextlib.suppress(OSError, ValueError, TypeError, AttributeError):
            reported = json.loads((job_dir / "error.json").read_text()).get("code")
            if reported in UNAVAILABLE_CODES:
                code = reported
        return ChildOutcome(code, terminal=True, returncode=returncode)
    if returncode == EXIT_EXTRA_REQUIRED:
        return ChildOutcome(SIGNALS_EXTRA_REQUIRED, terminal=True, returncode=returncode)
    if returncode == EXIT_OUT_OF_MEMORY:
        return ChildOutcome(ANALYSIS_OUT_OF_MEMORY, returncode=returncode)
    return ChildOutcome(ANALYSIS_CRASHED, returncode=returncode)


class AnalysisProcess:
    """Runs one analysis child at a time."""

    def __init__(
        self,
        *,
        memory_limit_bytes: int = MEMORY_LIMIT_BYTES,
        max_timeout_s: float | None = None,
    ) -> None:
        self.memory_limit_bytes = memory_limit_bytes
        # A bound on every run's timeout, whatever its job's duration.
        self.max_timeout_s = max_timeout_s
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._closed = False

    @property
    def pid(self) -> int | None:
        """The running child's process ID, if one runs."""

        process = self._process
        return None if process is None else process.pid

    def run(self, job_dir: Path, *, timeout_s: float) -> ChildOutcome:
        """Run the child on ``job_dir`` and wait for it, at most ``timeout_s``."""

        environment = {**os.environ, **_ONE_THREAD}
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(None, [_package_root(), environment.get("PYTHONPATH")])
        )
        command = [
            sys.executable,
            "-m",
            "earshot.signals.child",
            "--memory-limit-bytes",
            str(self.memory_limit_bytes),
            "--parent-pid",
            str(os.getpid()),
            str(job_dir),
        ]
        with self._lock:
            if self._closed:
                raise AnalysisClosedError("the analysis process is closed")
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                # Its messages could quote a path or a decoder's view of the audio.
                stderr=subprocess.DEVNULL,
                cwd=job_dir,
                env=environment,
                start_new_session=True,
                close_fds=True,
            )
            self._process = process
        limit = timeout_s if self.max_timeout_s is None else min(timeout_s, self.max_timeout_s)
        try:
            returncode = process.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            process.wait()
            outcome = ChildOutcome(ANALYSIS_TIMEOUT, returncode=process.returncode)
        else:
            outcome = child_outcome(returncode, job_dir)
        finally:
            with self._lock:
                self._process = None
        if self._closed:
            raise AnalysisClosedError("the analysis process was closed during a run")
        return outcome

    def close(self) -> None:
        """Refuse new runs and kill a running child's process group."""

        with self._lock:
            self._closed = True
            process = self._process
        if process is not None:
            _kill_group(process)


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    """Kill the child's whole process group (it is its session's leader)."""

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
