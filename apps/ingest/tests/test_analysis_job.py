"""The analysis job end to end in one server's terms.

``AnalysisRunner`` claims a job, copies the recording out of the blob store into
its spool directory, runs the real child process (``python -m
earshot.signals.child``: libsndfile, the vendored Silero model), and stores the
result and its peaks. These tests run it on a real catalog of the dialect under
test, real blob stores (local disk and MinIO), the real fixture audio and real child
processes; failures are made with the declared, env-gated seams
(``crash_points``), which are inert outside ``EARSHOT_TEST_MODE=1``.
"""

from __future__ import annotations

import io
import json
import os
import signal
import stat
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("numpy")
pytest.importorskip("onnxruntime")
soundfile = pytest.importorskip("soundfile")

from earshot.blobstore import put_stream  # noqa: E402
from earshot.clock import ManualClock  # noqa: E402
from earshot.maintenance import MaintenanceLoop  # noqa: E402
from earshot.signals import (  # noqa: E402
    ANALYSIS_CRASHED,
    ANALYSIS_DECODE_FAILED,
    ANALYSIS_LEASE_EXPIRED,
    ANALYSIS_OUT_OF_MEMORY,
    ANALYSIS_SPOOL_FAILED,
    ANALYSIS_TIMEOUT,
    ANALYSIS_UNSUPPORTED_CODEC,
    analyze,
)
from earshot.signals.runner import AnalysisProcess  # noqa: E402
from earshot.storage_analysis import (  # noqa: E402
    BUSY_WRITE_PAUSES_SECONDS,
    AnalysisRunner,
    AnalysisStore,
    analysis_job,
)
from earshot.storage_call_purge import CallPurger  # noqa: E402
from earshot.storage_calls import CallStore  # noqa: E402
from earshot.storage_recordings import RecordingManifest, RecordingStore  # noqa: E402
from lock_holders import store_write_lock_held_elsewhere  # noqa: E402
from store_support import catalog_rows, make_store  # noqa: E402

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
PIPECAT_AUDIO = ROOT / "fixtures" / "captured" / "pipecat-otlp-1.5" / "audio"
SECOND = 1_000_000_000
MINUTE = 60 * SECOND
START = 1_800_000_000 * SECOND
TEST_SEAMS = (
    "EARSHOT_TEST_MODE",
    "EARSHOT_TEST_CRASH_AT",
    "EARSHOT_TEST_STALL_AT",
    "EARSHOT_TEST_HOLD_AT",
    "EARSHOT_TEST_HOLD_FILE",
)


class World:
    """One store, its recordings, its analyses and a runner, on one blob store."""

    def __init__(self, path: Path, blobs, *, process: AnalysisProcess | None = None) -> None:
        self.path = path
        self.clock = ManualClock(wall=START, monotonic=START)
        self.store = make_store(path, clock=self.clock)
        self.calls = CallStore(self.store)
        self.recordings = RecordingStore(self.store, calls=self.calls, blobs=blobs)
        self.analyses = AnalysisStore(self.store)
        self.spool = path / "analysis-tmp"
        self.runner = AnalysisRunner(
            self.analyses, blobs=blobs, spool_root=self.spool, process=process
        )

    def store_recording(
        self,
        recording_id: str,
        body: bytes,
        *,
        speaker: str = "user",
        content_type: str = "audio/wav",
        duration_ms: int | None = 32_486,
        session_key: str = "call-1",
    ):
        self.recordings.create(
            "default",
            RecordingManifest(
                recording_id=recording_id,
                session_key=session_key,
                speaker=speaker,
                content_type=content_type,
                started_at_unix_nano=str(START),
            ),
        )
        attempt = self.recordings.begin_upload("default", recording_id)
        try:
            sha256, size = put_stream(self.recordings.blobs, attempt.blob_key, [body])
            return self.recordings.finish_upload(
                "default",
                recording_id,
                attempt.blob_key,
                sha256=sha256,
                size=size,
                duration_ms=duration_ms,
            )
        finally:
            self.recordings.end_upload("default", attempt.blob_key)

    def job(self, recording_id: str) -> tuple:
        [row] = catalog_rows(
            self.path,
            "SELECT state, attempts, error_code, peaks_blob_key, rendition_blob_key "
            "FROM audio_analysis WHERE recording_id = ?",
            (recording_id,),
        )
        return row

    def result(self, recording_id: str) -> dict:
        [(document,)] = catalog_rows(
            self.path,
            "SELECT result_json FROM audio_analysis WHERE recording_id = ?",
            (recording_id,),
        )
        return json.loads(document)

    def spooled(self) -> list[str]:
        return sorted(entry.name for entry in self.spool.iterdir()) if self.spool.is_dir() else []


@pytest.fixture(autouse=True)
def _no_seams(monkeypatch) -> None:
    for name in TEST_SEAMS:
        monkeypatch.delenv(name, raising=False)


def _expected(tmp_path: Path, speaker: str) -> tuple[dict, bytes]:
    peaks = tmp_path / f"expected-{speaker}.dat"
    result = analyze(PIPECAT_AUDIO / f"{speaker}.wav", speakers=[speaker], peaks_path=peaks)
    return result, peaks.read_bytes()


def _read_blob(blobs, key: str) -> bytes:
    info = blobs.head(key)
    assert info is not None, key
    with blobs.open_range(key, 0, info.size) as reader:
        return b"".join(reader)


def test_a_stored_recording_is_analysed_in_a_child_and_its_peaks_stored(
    tmp_path, blob_backend
) -> None:
    world = World(tmp_path, blob_backend.store)
    body = (PIPECAT_AUDIO / "user.wav").read_bytes()
    world.store_recording("call-1.user", body)
    expected, expected_peaks = _expected(tmp_path, "user")
    [(seq_before,)] = catalog_rows(tmp_path, "SELECT projection_seq FROM calls")

    outcome = world.runner.run_next()

    assert outcome is not None and outcome.result == "stored"
    state, attempts, error_code, peaks_key, rendition_key = world.job("call-1.user")
    assert (state, attempts, error_code, rendition_key) == ("stored", 0, None, None)
    assert peaks_key.startswith("analysis/default/") and peaks_key.endswith("-peaks.dat")
    assert world.result("call-1.user") == expected
    assert _read_blob(blob_backend.store, peaks_key) == expected_peaks
    assert catalog_rows(tmp_path, "SELECT projection_seq FROM calls") == [(seq_before + 1,)]
    assert catalog_rows(tmp_path, "SELECT count(*) FROM pending_blob_deletions") == [(0,)]
    assert world.spooled() == []  # the job's copy of the call audio is gone
    assert stat.S_IMODE(world.spool.stat().st_mode) == 0o700  # it held call audio
    assert world.runner.run_next() is None


@pytest.mark.sqlite_only
def test_the_analysis_never_runs_in_the_serving_process(tmp_path) -> None:
    """A fresh interpreter runs one job; the numeric libraries load only in its child."""

    probe = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path
        from earshot.blobstore import LocalBlobStore, put_stream
        from earshot.storage import IncidentStore
        from earshot.storage_analysis import AnalysisRunner, AnalysisStore
        from earshot.storage_calls import CallStore
        from earshot.storage_recordings import RecordingManifest, RecordingStore

        path = Path({str(tmp_path)!r})
        store = IncidentStore(path)
        blobs = LocalBlobStore(path / "blobs")
        recordings = RecordingStore(store, calls=CallStore(store), blobs=blobs)
        recordings.create("default", RecordingManifest(
            recording_id="r1", session_key="k", speaker="user", content_type="audio/wav",
            started_at_unix_nano="1800000000000000000"))
        attempt = recordings.begin_upload("default", "r1")
        body = Path({str(PIPECAT_AUDIO / "user.wav")!r}).read_bytes()
        sha, size = put_stream(blobs, attempt.blob_key, [body])
        recordings.finish_upload(
            "default", "r1", attempt.blob_key, sha256=sha, size=size, duration_ms=32486)
        recordings.end_upload("default", attempt.blob_key)
        runner = AnalysisRunner(AnalysisStore(store), blobs=blobs, spool_root=path / "spool")
        outcome = runner.run_next()
        loaded = sorted(n for n in ("numpy", "onnxruntime", "soundfile") if n in sys.modules)
        print(outcome.result, ",".join(loaded))
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "packages" / "sdk-python" / "src")},
    )

    assert completed.stdout.split() == ["stored"]


def test_a_malformed_recording_is_unavailable_and_the_server_survives(
    tmp_path, blob_backend
) -> None:
    world = World(tmp_path, blob_backend.store)
    world.store_recording("call-1.user", b"RIFF\x24\x00\x00\x00WAVEfmt " + os.urandom(4096))

    outcome = world.runner.run_next()

    assert outcome is not None
    assert (outcome.result, outcome.error_code) == ("unavailable", ANALYSIS_DECODE_FAILED)
    assert world.job("call-1.user") == ("unavailable", 0, ANALYSIS_DECODE_FAILED, None, None)
    assert world.spooled() == [] and world.runner.run_next() is None


def test_a_codec_the_decoder_cannot_read_is_unavailable_never_a_fake_result(tmp_path) -> None:
    world = World(tmp_path, _local(tmp_path))
    world.store_recording(
        "call-1.user", b"\x1a\x45\xdf\xa3" + bytes(512), content_type="audio/webm"
    )

    outcome = world.runner.run_next()

    assert outcome is not None and outcome.error_code == ANALYSIS_UNSUPPORTED_CODEC
    assert world.job("call-1.user")[:3] == ("unavailable", 0, ANALYSIS_UNSUPPORTED_CODEC)
    assert catalog_rows(tmp_path, "SELECT result_json FROM audio_analysis") == [(None,)]


def _local(tmp_path: Path):
    from earshot.blobstore import LocalBlobStore

    return LocalBlobStore(tmp_path / "blobs")


def test_a_crashing_child_is_retried_after_1_4_16_minutes_then_failed(
    tmp_path, monkeypatch
) -> None:
    world = World(tmp_path, _local(tmp_path))
    world.store_recording("call-1.user", (PIPECAT_AUDIO / "user.wav").read_bytes())
    monkeypatch.setenv("EARSHOT_TEST_MODE", "1")
    monkeypatch.setenv("EARSHOT_TEST_CRASH_AT", "analysis_child_running")

    results = []
    for delay in (1, 4, 16, 0):
        outcome = world.runner.run_next()
        assert outcome is not None
        results.append((outcome.result, outcome.error_code))
        assert world.runner.run_next() is None  # put off
        world.clock.advance(delay * MINUTE)

    assert results == [("pending", ANALYSIS_CRASHED)] * 3 + [("failed", ANALYSIS_CRASHED)]
    assert world.job("call-1.user") == ("failed", 4, ANALYSIS_CRASHED, None, None)
    assert world.spooled() == []


def test_a_hung_child_is_killed_with_its_process_group_at_the_timeout(
    tmp_path, monkeypatch
) -> None:
    process = AnalysisProcess(max_timeout_s=3)
    world = World(tmp_path, _local(tmp_path), process=process)
    world.store_recording("call-1.user", (PIPECAT_AUDIO / "user.wav").read_bytes())
    monkeypatch.setenv("EARSHOT_TEST_MODE", "1")
    monkeypatch.setenv("EARSHOT_TEST_STALL_AT", "analysis_child_running")
    pids: list[int] = []
    watcher = threading.Thread(target=lambda: _record_pid(process, pids), daemon=True)
    watcher.start()

    began = time.monotonic()
    outcome = world.runner.run_next()
    elapsed = time.monotonic() - began

    assert outcome is not None
    assert (outcome.result, outcome.error_code) == ("pending", ANALYSIS_TIMEOUT)
    assert 3 <= elapsed < 20
    assert pids and not _alive(pids[0])
    assert world.spooled() == []


def _record_pid(process: AnalysisProcess, pids: list[int]) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.pid is not None:
            pids.append(process.pid)
            return
        time.sleep(0.01)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_the_memory_cap_applies_on_linux_and_is_a_retried_failure(tmp_path) -> None:
    """``RLIMIT_AS`` caps a Linux child; macOS refuses it, so there the child runs
    uncapped. 256 MiB is too little for Python, numpy and onnxruntime to load on
    Linux (a Docker probe on linux/arm64 failed at 512 MiB and ran at 768 MiB)."""

    world = World(
        tmp_path, _local(tmp_path), process=AnalysisProcess(memory_limit_bytes=256 * 2**20)
    )
    world.store_recording("call-1.user", (PIPECAT_AUDIO / "user.wav").read_bytes())

    outcome = world.runner.run_next()

    assert outcome is not None
    if sys.platform.startswith("linux"):
        assert outcome.result == "pending"
        assert outcome.error_code in {ANALYSIS_OUT_OF_MEMORY, ANALYSIS_CRASHED}
    else:
        assert outcome.result == "stored"


@pytest.mark.parametrize(
    ("environment"),
    [
        {"EARSHOT_TEST_CRASH_AT": "analysis_child_running"},
        {"EARSHOT_TEST_STALL_AT": "analysis_child_running"},
        {"EARSHOT_TEST_HOLD_AT": "analysis_child_finished", "EARSHOT_TEST_HOLD_FILE": "x"},
        {"EARSHOT_TEST_MODE": "0", "EARSHOT_TEST_CRASH_AT": "analysis_child_running"},
    ],
    ids=["crash", "stall", "hold", "mode-0"],
)
def test_the_seams_do_nothing_outside_test_mode(tmp_path, monkeypatch, environment) -> None:
    world = World(tmp_path, _local(tmp_path), process=AnalysisProcess(max_timeout_s=60))
    world.store_recording("call-1.user", (PIPECAT_AUDIO / "user.wav").read_bytes())
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    outcome = world.runner.run_next()

    assert outcome is not None and outcome.result == "stored"


# --- failures after the claim, outside the child ------------------------------------------------


def _hold_child(monkeypatch, tmp_path: Path) -> Path:
    """Hold each child once it has written its outputs, until the returned file exists."""

    release = tmp_path / "release"
    monkeypatch.setenv("EARSHOT_TEST_MODE", "1")
    monkeypatch.setenv("EARSHOT_TEST_HOLD_AT", "analysis_child_finished")
    monkeypatch.setenv("EARSHOT_TEST_HOLD_FILE", str(release))
    return release


def _wait_held(release: Path) -> None:
    deadline = time.monotonic() + 60
    while not Path(f"{release}.held").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert Path(f"{release}.held").exists(), "the child never reached its hold point"


def _in_thread(run, **kwargs) -> tuple[threading.Thread, list]:
    """Start ``run(**kwargs)``; the list gets what it returned or raised."""

    results: list = []

    def target() -> None:
        try:
            results.append(run(**kwargs))
        except Exception as error:
            results.append(error)

    thread = threading.Thread(target=target)
    thread.start()
    return thread, results


def _store_busy_elsewhere(path: Path, free: threading.Event) -> threading.Thread:
    """Another process takes the store's write lock and holds it until ``free``."""

    held = threading.Event()

    def hold() -> None:
        with store_write_lock_held_elsewhere(path):
            held.set()
            free.wait(60)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(30)
    return holder


def test_a_spool_that_cannot_be_created_is_retried_then_failed(tmp_path) -> None:
    world = World(tmp_path, _local(tmp_path))
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the spool directory's parent should be")
    world.runner = AnalysisRunner(
        world.analyses, blobs=world.recordings.blobs, spool_root=blocker / "analysis-tmp"
    )
    world.store_recording("call-1.user", (PIPECAT_AUDIO / "user.wav").read_bytes())

    results = []
    for delay in (1, 4, 16, 0):
        outcome = world.runner.run_next()
        assert outcome is not None
        results.append((outcome.result, outcome.error_code))
        assert world.runner.run_next() is None  # put off
        world.clock.advance(delay * MINUTE)

    assert results == [("pending", ANALYSIS_SPOOL_FAILED)] * 3 + [("failed", ANALYSIS_SPOOL_FAILED)]
    assert world.job("call-1.user") == ("failed", 4, ANALYSIS_SPOOL_FAILED, None, None)


def test_a_child_answer_the_server_cannot_read_is_a_retried_failure(tmp_path, monkeypatch) -> None:
    world = World(tmp_path, _local(tmp_path))
    world.store_recording("call-1.user", (PIPECAT_AUDIO / "user.wav").read_bytes())
    release = _hold_child(monkeypatch, tmp_path)
    job, results = _in_thread(world.runner.run_next)
    _wait_held(release)
    [answer] = world.spool.glob("*/result.json")
    answer.write_text('{"algorithm_version": 1, "chan')  # cut short, as a full disk does

    release.touch()
    job.join(60)

    assert [(outcome.result, outcome.error_code) for outcome in results] == [
        ("pending", ANALYSIS_CRASHED)
    ]
    assert world.job("call-1.user") == ("pending", 1, ANALYSIS_CRASHED, None, None)
    assert [key for key in _blob_keys(tmp_path) if key.startswith("analysis/")] == []
    assert catalog_rows(tmp_path, "SELECT count(*) FROM pending_blob_deletions") == [(0,)]
    assert world.spooled() == []


def test_a_store_busy_when_the_result_is_ready_delays_its_write_and_loses_nothing(
    tmp_path, monkeypatch
) -> None:
    world = World(tmp_path, _local(tmp_path))
    world.store_recording("call-1.user", (PIPECAT_AUDIO / "user.wav").read_bytes())
    release = _hold_child(monkeypatch, tmp_path)
    job, results = _in_thread(world.runner.run_next, budget_s=0.2)
    _wait_held(release)
    free = threading.Event()
    holder = _store_busy_elsewhere(tmp_path, free)

    release.touch()
    time.sleep(2.0)  # the result is ready while another process holds the store
    free.set()
    holder.join(30)
    job.join(60)

    assert [outcome.result for outcome in results] == ["stored"]
    assert world.job("call-1.user")[:3] == ("stored", 0, None)


def test_a_store_busy_past_every_retry_leaves_a_lease_whose_expiry_counts(
    tmp_path, monkeypatch
) -> None:
    world = World(tmp_path, _local(tmp_path))
    world.store_recording("call-1.user", (PIPECAT_AUDIO / "user.wav").read_bytes())
    release = _hold_child(monkeypatch, tmp_path)
    job, results = _in_thread(world.runner.run_next, budget_s=0.2)
    _wait_held(release)
    free = threading.Event()
    holder = _store_busy_elsewhere(tmp_path, free)
    release.touch()
    job.join(60)  # it gave up while the store stayed busy
    free.set()
    holder.join(30)
    assert [type(error).__name__ for error in results] == ["StorageBusyError"]
    assert world.job("call-1.user")[:2] == ("running", 0)  # nothing could be written
    assert world.spooled() == []

    world.clock.advance(20 * MINUTE)  # past its lease

    assert world.runner.run_next() is None  # the expired lease is a failed attempt, put off
    assert world.job("call-1.user")[:3] == ("pending", 1, ANALYSIS_LEASE_EXPIRED)
    world.clock.advance(MINUTE)
    outcome = world.runner.run_next()
    assert outcome is not None and outcome.result == "stored"


def test_a_store_that_frees_just_after_the_writes_gave_up_still_costs_no_attempt(
    tmp_path, monkeypatch
) -> None:
    """The result's writes give up while the store is busy; the store being free a moment
    later must not turn that into a failed attempt (and a finished analysis thrown
    away): the job stays leased and only its lease's expiry counts."""

    world = World(tmp_path, _local(tmp_path))
    world.store_recording("call-1.user", (PIPECAT_AUDIO / "user.wav").read_bytes())
    release = _hold_child(monkeypatch, tmp_path)
    job, results = _in_thread(world.runner.run_next, budget_s=0.2)
    _wait_held(release)
    free = threading.Event()
    holder = _store_busy_elsewhere(tmp_path, free)
    started = time.monotonic()
    release.touch()
    # Every pause (3.75 s) and every attempt's own wait (0.2 s each) pass before the
    # first write gives up; a moment later the store is free.
    time.sleep(sum(BUSY_WRITE_PAUSES_SECONDS) + 0.2 * (len(BUSY_WRITE_PAUSES_SECONDS) + 1) + 0.8)
    free.set()
    holder.join(30)
    job.join(60)

    assert time.monotonic() - started < 30
    assert [type(error).__name__ for error in results] == ["StorageBusyError"]
    assert world.job("call-1.user")[:2] == ("running", 0)  # no attempt was counted
    assert world.spooled() == []
    assert [key for key in _blob_keys(tmp_path) if key.startswith("analysis/")] == []


def _blob_keys(path: Path) -> list[str]:
    root = path / "blobs"
    return sorted(str(item.relative_to(root)) for item in root.rglob("*") if item.is_file())


# --- a purge while the child runs ----------------------------------------------------------------


def test_purging_a_call_during_its_analysis_leaves_no_derived_blob(
    tmp_path, blob_backend, monkeypatch
) -> None:
    world = World(tmp_path, blob_backend.store)
    stored = world.store_recording("call-1.user", (PIPECAT_AUDIO / "user.wav").read_bytes())
    release = tmp_path / "release"
    monkeypatch.setenv("EARSHOT_TEST_MODE", "1")
    monkeypatch.setenv("EARSHOT_TEST_HOLD_AT", "analysis_child_finished")
    monkeypatch.setenv("EARSHOT_TEST_HOLD_FILE", str(release))
    outcomes = []
    job = threading.Thread(target=lambda: outcomes.append(world.runner.run_next()))
    job.start()
    deadline = time.monotonic() + 60
    while not Path(f"{release}.held").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert Path(f"{release}.held").exists(), "the child never reached its hold point"

    CallPurger(world.store, calls=world.calls, recordings=world.recordings).purge_call(
        "default", stored.call_id
    )
    release.touch()
    job.join(60)
    world.recordings.drain_blob_deletions(now_unix_nano=world.clock.unix_nano())

    assert [outcome.result for outcome in outcomes] == ["lost"]
    assert [key for key in blob_backend.objects() if key.startswith("analysis/")] == []
    assert blob_backend.objects() == [] and blob_backend.leftovers() == []
    assert catalog_rows(tmp_path, "SELECT count(*) FROM pending_blob_deletions") == [(0,)]
    assert world.spooled() == []


def test_purging_an_analysed_call_deletes_its_derived_blobs(tmp_path, blob_backend) -> None:
    world = World(tmp_path, blob_backend.store)
    stored = world.store_recording("call-1.user", (PIPECAT_AUDIO / "user.wav").read_bytes())
    assert world.runner.run_next().result == "stored"
    assert len(blob_backend.objects()) == 2  # the recording and its peaks

    CallPurger(world.store, calls=world.calls, recordings=world.recordings).purge_call(
        "default", stored.call_id
    )
    world.recordings.drain_blob_deletions(now_unix_nano=world.clock.unix_nano())

    assert blob_backend.objects() == []


# --- a mono pair's stereo rendition --------------------------------------------------------------


def test_a_mono_pair_on_one_timeline_gets_one_stereo_flac_rendition(tmp_path) -> None:
    world = World(tmp_path, _local(tmp_path))
    world.store_recording("call-1.user", (PIPECAT_AUDIO / "user.wav").read_bytes())
    world.store_recording(
        "call-1.agent", (PIPECAT_AUDIO / "agent.wav").read_bytes(), speaker="agent"
    )

    outcomes = [world.runner.run_next(), world.runner.run_next()]

    assert [outcome.result for outcome in outcomes] == ["stored", "stored"]
    rendition_key = world.job("call-1.user")[4]
    assert rendition_key is not None and world.job("call-1.agent")[4] is None
    flac = _read_blob(world.recordings.blobs, rendition_key)
    samples, rate = soundfile.read(io.BytesIO(flac), dtype="int16", always_2d=True)
    user, _ = soundfile.read(PIPECAT_AUDIO / "user.wav", dtype="int16")
    agent, _ = soundfile.read(PIPECAT_AUDIO / "agent.wav", dtype="int16")
    assert rate == 16000 and samples.shape == (len(user), 2)
    assert (samples[:, 0] == user).all() and (samples[:, 1] == agent).all()
    assert catalog_rows(tmp_path, "SELECT count(*) FROM pending_blob_deletions") == [(0,)]


# --- the start: spool sweep, released leases, requeue --------------------------------------------


def test_a_start_sweeps_the_spool_and_runs_what_a_dead_process_left(tmp_path) -> None:
    world = World(tmp_path, _local(tmp_path))
    world.store_recording("call-1.user", (PIPECAT_AUDIO / "user.wav").read_bytes())
    left = world.spool / "0123abcd"
    left.mkdir(parents=True)
    (left / "source.wav").write_bytes(b"call audio a crash left behind")
    assert world.analyses.claim() is not None  # a process claims it, then dies
    loop = MaintenanceLoop(clock=world.clock)
    loop.add_job("recording-analysis", analysis_job(world.runner))

    import asyncio

    asyncio.run(loop.run_once())  # sweep, release, requeue (nothing), run the job

    assert world.job("call-1.user")[0] == "stored"
    assert world.spooled() == []
    asyncio.run(loop.aclose())


@pytest.mark.sqlite_only
def test_the_child_exits_when_its_server_dies(tmp_path) -> None:
    """A server killed outright (the lock watchdog's exit, an OOM kill) leaves no
    analysis running: the child sees its parent gone and exits."""

    world = World(tmp_path, _local(tmp_path))
    world.store_recording("call-1.user", (PIPECAT_AUDIO / "user.wav").read_bytes())
    world.store.close()
    release, pid_file = tmp_path / "release", tmp_path / "child.pid"
    server = textwrap.dedent(
        f"""
        import threading, time
        from pathlib import Path
        from earshot.blobstore import LocalBlobStore
        from earshot.storage import IncidentStore
        from earshot.storage_analysis import AnalysisRunner, AnalysisStore
        path = Path({str(tmp_path)!r})
        runner = AnalysisRunner(
            AnalysisStore(IncidentStore(path)),
            blobs=LocalBlobStore(path / "blobs"),
            spool_root=path / "analysis-tmp",
        )
        def note_pid():
            while runner.process.pid is None:
                time.sleep(0.01)
            Path({str(pid_file)!r}).write_text(str(runner.process.pid))
        threading.Thread(target=note_pid, daemon=True).start()
        runner.run_next()
        """
    )
    environment = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "packages" / "sdk-python" / "src"),
        "EARSHOT_TEST_MODE": "1",
        "EARSHOT_TEST_HOLD_AT": "analysis_child_finished",
        "EARSHOT_TEST_HOLD_FILE": str(release),
    }
    parent = subprocess.Popen([sys.executable, "-c", server], env=environment)
    try:
        deadline = time.monotonic() + 60
        while not Path(f"{release}.held").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        child = int(pid_file.read_text())
        assert _alive(child)

        parent.send_signal(signal.SIGKILL)
        parent.wait(10)
        deadline = time.monotonic() + 10
        while _alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)

        assert not _alive(child)
    finally:
        if parent.poll() is None:
            parent.kill()
        release.touch()
