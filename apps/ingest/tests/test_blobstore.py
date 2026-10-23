"""The BlobStore contract (FR-8), run unchanged against local disk and real S3.

Every test that takes ``blob_backend`` runs as ``[local]`` (a ``file://`` store
under ``tmp_path``) and ``[s3]`` (the real S3-compatible test server, by default
the ``earshot-dev-minio`` container: see ``blob_support.py``). Nothing is mocked:
leftovers are read back from the real spool directory and from the real
``ListMultipartUploads``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import subprocess
import threading

import pytest

from blob_support import PYTHON, BlobBackend, child_environment, imported_package_root
from earshot.blobstore import (
    BlobNotFoundError,
    ContentDigestError,
    ContentDigestMismatchError,
    open_blob_store,
    put_stream,
)
from earshot.server_config import load_server_settings

pytestmark = pytest.mark.integration

MIB = 1024 * 1024
PART = 8 * MIB


def _payload(size: int) -> bytes:
    return random.Random(size).randbytes(size)


def _chunks(data: bytes, size: int = MIB + 7):
    """Uneven chunks, so part boundaries fall inside a chunk."""
    for start in range(0, len(data), size):
        yield data[start : start + size]


def _put(backend: BlobBackend, key: str, data: bytes) -> tuple[str, int]:
    writer = backend.store.begin_put(key)
    for chunk in _chunks(data):
        writer.write(chunk)
    return writer.commit()


def _read(backend: BlobBackend, key: str, start: int, end: int) -> bytes:
    return b"".join(backend.store.open_range(key, start, end))


@pytest.mark.parametrize(
    "size",
    [0, 1, PART - 1, PART, PART + 1, 3 * PART + 5],
    ids=["empty", "one-byte", "part-minus-1", "one-part", "part-plus-1", "four-parts"],
)
def test_a_committed_blob_round_trips_with_its_exact_sha256_and_size(
    blob_backend: BlobBackend, size: int
) -> None:
    data = _payload(size)
    key = f"recordings/project-a/{size}"

    assert _put(blob_backend, key, data) == (hashlib.sha256(data).hexdigest(), size)

    assert blob_backend.store.head(key).size == size
    assert _read(blob_backend, key, 0, size) == data
    assert blob_backend.objects() == [key]
    assert blob_backend.leftovers() == []


def test_range_reads_are_exact_at_part_boundaries(blob_backend: BlobBackend) -> None:
    size = 2 * PART + 3
    data = _payload(size)
    _put(blob_backend, "range/blob", data)
    _put(blob_backend, "range/empty", b"")

    for start, end in [
        (0, 1),
        (0, PART),
        (PART - 1, PART + 1),
        (PART, 2 * PART),
        (2 * PART - 2, size),
        (size - 1, size),
        (17, 17),
        (size, size),
        (0, size),
    ]:
        assert _read(blob_backend, "range/blob", start, end) == data[start:end], (start, end)
    assert _read(blob_backend, "range/empty", 0, 0) == b""

    for start, end in [(0, size + 1), (size, size + 1), (5, 4), (-1, 3)]:
        with pytest.raises(ValueError):
            blob_backend.store.open_range("range/blob", start, end)
    with pytest.raises(ValueError):
        blob_backend.store.open_range("range/empty", 0, 1)


def test_a_missing_key_has_no_head_and_cannot_be_read(blob_backend: BlobBackend) -> None:
    assert blob_backend.store.head("recordings/none") is None
    for start, end in [(0, 1), (0, 0)]:
        with pytest.raises(BlobNotFoundError):
            blob_backend.store.open_range("recordings/none", start, end)


def test_delete_is_idempotent(blob_backend: BlobBackend) -> None:
    blob_backend.store.delete("recordings/never-written")
    _put(blob_backend, "recordings/doomed", b"x" * 10)

    blob_backend.store.delete("recordings/doomed")
    blob_backend.store.delete("recordings/doomed")

    assert blob_backend.store.head("recordings/doomed") is None
    assert blob_backend.objects() == []


@pytest.mark.parametrize(
    "key",
    ["", "/abs", "a//b", "a/", "../escape", "a/../b", ".tmp/x", "a/.hidden", "UPPER", "a b", "é"],
)
def test_keys_outside_the_portable_alphabet_are_refused(
    blob_backend: BlobBackend, key: str
) -> None:
    store = blob_backend.store
    for operation in (
        lambda: store.begin_put(key),
        lambda: store.head(key),
        lambda: store.open_range(key, 0, 0),
        lambda: store.delete(key),
    ):
        with pytest.raises(ValueError):
            operation()
    assert blob_backend.leftovers() == []


def test_a_committed_or_aborted_writer_is_closed(blob_backend: BlobBackend) -> None:
    committed = blob_backend.store.begin_put("writers/committed")
    committed.write(b"kept")
    committed.commit()
    committed.abort()  # a no-op after commit: the object stays
    for operation in (lambda: committed.write(b"more"), committed.commit):
        with pytest.raises(ValueError):
            operation()

    aborted = blob_backend.store.begin_put("writers/aborted")
    aborted.abort()
    aborted.abort()
    for operation in (lambda: aborted.write(b"more"), aborted.commit):
        with pytest.raises(ValueError):
            operation()

    assert _read(blob_backend, "writers/committed", 0, 4) == b"kept"
    assert blob_backend.objects() == ["writers/committed"]


def test_the_default_blob_url_keeps_blobs_inside_the_data_directory(tmp_path) -> None:
    data_dir = tmp_path / "data #1 100%"
    settings = load_server_settings({"EARSHOT_DATA_DIR": str(data_dir)})
    store = open_blob_store(settings.blob_url)

    writer = store.begin_put("recordings/default/attempt")
    writer.write(b"inside")
    writer.commit()

    assert (data_dir / "blobs" / "recordings" / "default" / "attempt").read_bytes() == b"inside"


def test_an_aborted_write_leaves_nothing_visible(blob_backend: BlobBackend) -> None:
    store = blob_backend.store
    writer = store.begin_put("aborted/streamed")
    for chunk in _chunks(_payload(PART + 10)):
        writer.write(chunk)
    # A part is already out: an S3 multipart upload or a local spool file is live.
    assert len(blob_backend.leftovers()) == 1

    writer.abort()

    with pytest.raises(RuntimeError), store.begin_put("aborted/by-exception") as failing:
        failing.write(b"partial body")
        raise RuntimeError("the client went away")

    assert store.head("aborted/streamed") is None
    assert store.head("aborted/by-exception") is None
    assert blob_backend.objects() == []
    assert blob_backend.leftovers() == []


CRASHING_WRITER = """
import os, sys, time
from earshot.blobstore import PART_SIZE, open_blob_store
from earshot.server_config import load_server_settings

store = open_blob_store(load_server_settings().blob_url)
writer = store.begin_put("crash/victim")
writer.write(os.urandom(PART_SIZE + 10))
print("streaming", flush=True)
time.sleep(120)
"""


def test_a_crashed_writer_leaves_only_what_the_startup_sweep_removes(
    blob_backend: BlobBackend,
) -> None:
    child = subprocess.Popen(
        [PYTHON, "-c", CRASHING_WRITER],
        env=child_environment(blob_backend),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline() == "streaming\n", child.stderr.read()
    finally:
        child.kill()
        child.wait(timeout=30)
        child.stdout.close()
        child.stderr.close()
    assert blob_backend.store.head("crash/victim") is None
    assert len(blob_backend.leftovers()) == 1

    restarted = blob_backend.reopen()
    survivor = restarted.begin_put("crash/survivor")
    for chunk in _chunks(_payload(PART + 1)):
        survivor.write(chunk)
    assert restarted.sweep() == 1  # the crashed write, not the live one
    survivor.commit()

    assert blob_backend.objects() == ["crash/survivor"]
    assert blob_backend.leftovers() == []
    assert restarted.sweep() == 0


def test_a_reader_never_sees_a_partial_object(blob_backend: BlobBackend) -> None:
    store = blob_backend.store
    data = _payload(3 * PART + 5)
    whole = (len(data), hashlib.sha256(data).hexdigest())
    observed: list[tuple[int, str] | None] = []
    first_look, committed = threading.Event(), threading.Event()

    def look() -> None:
        info = store.head("atomic/blob")
        if info is None:
            observed.append(None)
        else:
            content = _read(blob_backend, "atomic/blob", 0, info.size)
            observed.append((info.size, hashlib.sha256(content).hexdigest()))

    def reader() -> None:
        look()
        first_look.set()
        while not committed.is_set():
            look()
        look()

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        assert first_look.wait(30)
        writer = store.begin_put("atomic/blob")
        for chunk in _chunks(data):
            writer.write(chunk)
        assert store.head("atomic/blob") is None  # every byte written, nothing visible
        with pytest.raises(BlobNotFoundError):
            store.open_range("atomic/blob", 0, 1)
        writer.commit()
    finally:
        committed.set()
        thread.join(60)

    assert observed[0] is None and observed[-1] == whole
    assert set(observed) == {None, whole}


def _content_digest(data: bytes, algorithm: str = "sha256") -> str:
    name = {"sha256": "sha-256", "sha512": "sha-512"}[algorithm]
    return f"{name}=:{base64.b64encode(hashlib.new(algorithm, data).digest()).decode()}:"


def test_a_matching_content_digest_commits(blob_backend: BlobBackend) -> None:
    data = _payload(PART + 10)
    # A dictionary may carry other algorithms; the sha-256 member is the one verified.
    header = f"{_content_digest(data, 'sha512')}, {_content_digest(data)}"

    committed = put_stream(blob_backend.store, "digest/ok", _chunks(data), content_digest=header)

    assert committed == (hashlib.sha256(data).hexdigest(), len(data))
    assert _read(blob_backend, "digest/ok", 0, len(data)) == data


def test_a_mismatched_content_digest_commits_nothing(blob_backend: BlobBackend) -> None:
    data = _payload(PART + 10)
    live_before_commit: list[int] = []

    def body():
        yield from _chunks(data)
        live_before_commit.append(len(blob_backend.leftovers()))

    with pytest.raises(ContentDigestMismatchError):
        put_stream(
            blob_backend.store, "digest/bad", body(), content_digest=_content_digest(data + b"!")
        )

    assert live_before_commit == [1]  # the body was streamed out before the check

    # A writer used directly (no context manager) aborts itself on a mismatch too.
    writer = blob_backend.store.begin_put("digest/bad-direct")
    for chunk in _chunks(data):
        writer.write(chunk)
    with pytest.raises(ContentDigestMismatchError):
        writer.commit(expected_sha256=hashlib.sha256(data + b"!").digest())

    assert blob_backend.store.head("digest/bad") is None
    assert blob_backend.store.head("digest/bad-direct") is None
    assert blob_backend.objects() == []
    assert blob_backend.leftovers() == []


@pytest.mark.parametrize(
    "header",
    [
        "",
        "sha-256",
        "sha-256=abc",
        "sha-256=:not base64!:",
        f"sha-256=:{base64.b64encode(bytes(31)).decode()}:",
        f"SHA-256=:{base64.b64encode(bytes(32)).decode()}:",
        f"sha-256=:{base64.b64encode(bytes(32)).decode()}: trailing",
        f"sha-256=:{base64.b64encode(bytes(32)).decode()}:, not-a-digest",
        _content_digest(b"no sha-256 member", "sha512"),
    ],
    ids=[
        "empty",
        "no-value",
        "not-bytes",
        "bad-base64",
        "short",
        "uppercase",
        "trailing",
        "junk-member",
        "sha512",
    ],
)
def test_a_malformed_content_digest_is_refused_before_anything_is_written(
    tmp_path, header: str
) -> None:
    store = open_blob_store((tmp_path / "blobs").as_uri())

    def unread():
        raise AssertionError("the body was read")
        yield b""

    with pytest.raises(ContentDigestError):
        put_stream(store, "digest/malformed", unread(), content_digest=header)

    assert list((tmp_path / "blobs").rglob("*")) == [tmp_path / "blobs" / ".tmp"]


ONE_GIB_WRITER = """
import hashlib, json, os, resource, sys, time
from earshot.blobstore import open_blob_store
from earshot.server_config import load_server_settings

def peak_rss():
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024  # Linux reports KiB

store = open_blob_store(load_server_settings().blob_url)
store.head("memory/warm-up")  # credentials, connection pool and models load before the baseline
block = os.urandom(1024 * 1024)
expected = hashlib.sha256()
baseline = peak_rss()
started = time.monotonic()

def body():
    for _ in range(1024):
        expected.update(block)
        yield block

writer = store.begin_put("memory/one-gib")
for chunk in body():
    writer.write(chunk)
sha256, size = writer.commit()
print(json.dumps({
    "growth": peak_rss() - baseline,
    "seconds": time.monotonic() - started,
    "matches": (sha256, size) == (expected.hexdigest(), 1024 ** 3),
    "stored": store.head("memory/one-gib").size,
}))
"""


@pytest.mark.slow
def test_streaming_one_gib_grows_peak_rss_by_at_most_64_mib(blob_backend: BlobBackend) -> None:
    try:
        run = subprocess.run(
            [PYTHON, "-c", ONE_GIB_WRITER],
            env=child_environment(blob_backend),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert run.returncode == 0, run.stderr[-2000:]
        result = json.loads(run.stdout)
    finally:
        blob_backend.store.delete("memory/one-gib")

    print(
        f"[{blob_backend.name}] 1 GiB in {result['seconds']:.1f} s, "
        f"peak RSS growth {result['growth'] / MIB:.1f} MiB"
    )
    assert result["matches"] and result["stored"] == 1024**3
    assert result["growth"] <= 64 * MIB


LAZY_BOTO3 = """
import sys
from earshot.blobstore import open_blob_store
store = open_blob_store(sys.argv[1])
writer = store.begin_put("lazy/blob")
writer.write(b"no s3 extra needed")
writer.commit()
print(sorted(name for name in sys.modules if name.split(".")[0] in {"boto3", "botocore"}))
"""


def test_a_file_store_never_imports_boto3(tmp_path) -> None:
    run = subprocess.run(
        [PYTHON, "-c", LAZY_BOTO3, (tmp_path / "blobs").as_uri()],
        env={**os.environ, "PYTHONPATH": imported_package_root()},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert run.returncode == 0, run.stderr[-2000:]
    assert run.stdout.strip() == "[]"
