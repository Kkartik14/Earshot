"""Streaming blob storage for large evidence such as call recordings (FR-8).

One interface, two backends, chosen by ``EARSHOT_BLOB_URL``
(``server_config.parse_blob_url``):

``file:///absolute/root`` (``LocalBlobStore``; the default is ``<data-dir>/blobs``)
    A write spools into ``<root>/.tmp`` on the same filesystem. ``commit`` fsyncs
    the file, renames it over ``<root>/<key>`` and fsyncs the directories, as the
    incident CAS does, so a reader sees no object or the whole object.
``s3://bucket/prefix?endpoint=…&region=…&path_style=1`` (``S3BlobStore``)
    boto3 (the ``[s3]`` extra, imported only for an ``s3://`` URL) with SigV4. A
    write buffers at most one 8 MiB part: it becomes one ``PutObject`` when the
    blob fits in it, otherwise a sequential multipart upload of 8 MiB parts that
    ``commit`` completes and ``abort`` aborts. Credentials come from the standard
    AWS chain (``AWS_ACCESS_KEY_ID``, ``AWS_SECRET_ACCESS_KEY``, …), never the URL.

The contract, identical on both backends:

- ``begin_put(key)`` returns a ``BlobWriter``: ``write(chunk)``, then
  ``commit() -> (sha256_hex, size)`` or ``abort()``. The sha256 is computed while
  streaming. Nothing is visible before ``commit`` returns; after ``abort`` (or a
  failed ``write`` or ``commit``) nothing is left but what ``sweep`` removes.
  Committing to an existing key replaces it atomically.
- ``open_range(key, start, end)`` returns the bytes ``[start, end)`` as a
  ``BlobReader`` (an iterator of chunks with ``close``). It requires
  ``0 <= start <= end <= size`` and raises ``BlobNotFoundError`` for a missing key.
- ``head(key)`` returns ``BlobInfo(size)``, or ``None`` for a missing key.
- ``delete(key)`` is idempotent.
- ``sweep()`` removes what crashed writers left (spool files, in-progress multipart
  uploads under the prefix), except the live writers of this store object. Run it
  at startup; one server owns a blob location.

Keys are server-generated: ``/``-separated components of lowercase letters,
digits, ``.``, ``_`` and ``-``, none starting with ``.``. Lowercase keeps the
backends identical on case-insensitive filesystems. On local disk a key cannot
also be a directory of another key (``a`` and ``a/b``); Earshot's keys
(``recordings/<project>/<attempt>``) never nest that way.

``put_stream`` streams an iterable of chunks into a key and verifies an optional
``Content-Digest`` (RFC 9530) ``sha-256`` value before committing (AC8.3).
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import hmac
import os
import re
import stat
import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .server_config import FileBlobLocation, S3BlobLocation, parse_blob_url
from .storage import _fsync_directory

__all__ = [
    "PART_SIZE",
    "BlobInfo",
    "BlobNotFoundError",
    "BlobReader",
    "BlobStore",
    "BlobStoreError",
    "BlobWriter",
    "ContentDigestError",
    "ContentDigestMismatchError",
    "LocalBlobStore",
    "S3BlobStore",
    "open_blob_store",
    "parse_content_digest",
    "put_stream",
]

# The S3 part size, and so the most a writer buffers.
PART_SIZE = 8 * 1024 * 1024
# S3 allows at most 10,000 parts: 78 GiB at 8 MiB.
_MAX_PARTS = 10_000
_READ_CHUNK = 256 * 1024
_KEY_COMPONENT = re.compile(r"[a-z0-9_-][a-z0-9._-]{0,127}")
_MAX_KEY_LENGTH = 512
_SPOOL_DIRECTORY = ".tmp"
_CONTENT_DIGEST_MEMBER = re.compile(
    r"([a-z*][a-z0-9_.*-]*)[ \t]*=[ \t]*:([A-Za-z0-9+/]*={0,2}):(?:;[^,]*)?"
)


class BlobStoreError(Exception):
    """A blob store operation failed."""


class BlobNotFoundError(BlobStoreError):
    """The key holds no committed blob."""


class ContentDigestError(BlobStoreError):
    """A ``Content-Digest`` value is malformed or asserts no sha-256 digest."""


class ContentDigestMismatchError(ContentDigestError):
    """The streamed bytes do not match the asserted ``Content-Digest``; nothing was committed."""


@dataclass(frozen=True, slots=True)
class BlobInfo:
    """What ``head`` reports about a committed blob."""

    size: int


class BlobWriter(Protocol):
    """One streaming write. Use as a context manager to abort on any exit without commit."""

    def write(self, chunk: bytes) -> None: ...

    def commit(self, *, expected_sha256: bytes | None = None) -> tuple[str, int]: ...

    def abort(self) -> None: ...

    def __enter__(self) -> BlobWriter: ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...


class BlobStore(Protocol):
    """The FR-8 interface, implemented by ``LocalBlobStore`` and ``S3BlobStore``."""

    def begin_put(self, key: str) -> BlobWriter: ...

    def open_range(self, key: str, start: int, end: int) -> BlobReader: ...

    def head(self, key: str) -> BlobInfo | None: ...

    def delete(self, key: str) -> None: ...

    def sweep(self) -> int: ...


class BlobReader(Iterator[bytes]):
    """The chunks of one range read; ``close`` releases the file or HTTP stream."""

    def __init__(self, chunks: Iterator[bytes], close: Callable[[], None]) -> None:
        self._chunks = chunks
        self._close: Callable[[], None] | None = close

    def __next__(self) -> bytes:
        try:
            return next(self._chunks)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        close, self._close = self._close, None
        if close is not None:
            close()

    def __enter__(self) -> BlobReader:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def open_blob_store(url: str) -> LocalBlobStore | S3BlobStore:
    """The blob store an ``EARSHOT_BLOB_URL`` value names (see ``ServerSettings.blob_url``)."""

    location = parse_blob_url(url)
    if isinstance(location, FileBlobLocation):
        return LocalBlobStore(location.root)
    return S3BlobStore(location)


def parse_content_digest(value: str) -> bytes:
    """The sha-256 digest a ``Content-Digest`` field value asserts (RFC 9530).

    The value is a structured-field dictionary such as ``sha-256=:<base64>:``;
    other algorithms may appear and are ignored. A value that is malformed or has
    no ``sha-256`` member raises ``ContentDigestError``: Earshot verifies only
    sha-256, and an assertion it cannot check is refused rather than skipped.
    """

    digest: bytes | None = None
    for member in value.split(","):
        match = _CONTENT_DIGEST_MEMBER.fullmatch(member.strip(" \t"))
        if match is None:
            raise ContentDigestError("Content-Digest is not a valid digest dictionary")
        if match.group(1) != "sha-256":
            continue
        try:
            decoded = base64.b64decode(match.group(2), validate=True)
        except binascii.Error:
            raise ContentDigestError("Content-Digest sha-256 is not valid base64") from None
        if len(decoded) != hashlib.sha256().digest_size:
            raise ContentDigestError("Content-Digest sha-256 must be 32 bytes")
        digest = decoded  # RFC 8941: a repeated key keeps its last value
    if digest is None:
        raise ContentDigestError("Content-Digest has no sha-256 digest")
    return digest


def put_stream(
    store: BlobStore,
    key: str,
    chunks: Iterable[bytes],
    *,
    content_digest: str | None = None,
) -> tuple[str, int]:
    """Stream ``chunks`` into ``key`` and commit; ``(sha256_hex, size)``.

    With ``content_digest`` (a ``Content-Digest`` header value), a malformed value
    fails before anything is written, and a digest that does not match the
    streamed bytes aborts the write and raises ``ContentDigestMismatchError``.
    Any failure leaves nothing committed.
    """

    expected = None if content_digest is None else parse_content_digest(content_digest)
    with store.begin_put(key) as writer:
        for chunk in chunks:
            writer.write(chunk)
        return writer.commit(expected_sha256=expected)


def _check_key(key: str) -> None:
    if (
        not isinstance(key, str)
        or not key
        or len(key) > _MAX_KEY_LENGTH
        or not all(_KEY_COMPONENT.fullmatch(part) for part in key.split("/"))
    ):
        raise ValueError(
            "a blob key is /-separated components of [a-z0-9._-], none starting with '.'"
        )


def _check_range(start: int, end: int) -> None:
    if not 0 <= start <= end:
        raise ValueError("a blob range needs 0 <= start <= end")


class _Writer:
    """The writer state machine both backends share: hashing, sizes, commit and abort."""

    def __init__(self) -> None:
        self._hash = hashlib.sha256()
        self._size = 0
        self._open = True
        self._committed = False

    def write(self, chunk: bytes) -> None:
        self._require_open()
        if not chunk:
            return
        try:
            self._append(chunk)
        except BaseException:
            self._abort_after_failure()
            raise
        self._hash.update(chunk)
        self._size += len(chunk)

    def commit(self, *, expected_sha256: bytes | None = None) -> tuple[str, int]:
        """Publish the blob; ``(sha256_hex, size)``.

        With ``expected_sha256``, a different streamed digest aborts the write and
        raises ``ContentDigestMismatchError`` instead.
        """

        self._require_open()
        digest = self._hash.digest()
        if expected_sha256 is not None and not hmac.compare_digest(digest, expected_sha256):
            self._abort_after_failure()
            raise ContentDigestMismatchError("the body does not match its Content-Digest")
        try:
            self._publish()
        except BaseException:
            self._abort_after_failure()
            raise
        self._open, self._committed = False, True
        return digest.hex(), self._size

    def abort(self) -> None:
        """Discard the write; a no-op once committed or aborted."""

        if not self._open:
            return
        self._open = False
        self._discard()

    def __enter__(self) -> _Writer:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is None:
            self.abort()
        else:
            self._abort_after_failure()

    def _abort_after_failure(self) -> None:
        # Report the failure in flight, not a failed cleanup; whatever an abort
        # could not remove is a leftover the next startup sweep removes.
        with contextlib.suppress(Exception):
            self.abort()

    def _require_open(self) -> None:
        if not self._open:
            raise ValueError(
                "this blob writer is already committed"
                if self._committed
                else "this blob writer was aborted"
            )

    def _append(self, chunk: bytes) -> None:
        raise NotImplementedError

    def _publish(self) -> None:
        raise NotImplementedError

    def _discard(self) -> None:
        raise NotImplementedError


class LocalBlobStore:
    """Blobs as files under ``root``, written through the same-filesystem spool ``root/.tmp``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.spool = self.root / _SPOOL_DIRECTORY
        for directory in (self.root, self.spool):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)
        self._lock = threading.Lock()
        self._live_spool_files: set[str] = set()

    def begin_put(self, key: str) -> BlobWriter:
        _check_key(key)
        return _LocalWriter(self, self.root / key)

    def open_range(self, key: str, start: int, end: int) -> BlobReader:
        _check_key(key)
        _check_range(start, end)
        try:
            handle = (self.root / key).open("rb")
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            raise BlobNotFoundError("no blob at this key") from None
        try:
            if end > os.fstat(handle.fileno()).st_size:
                raise ValueError("the blob range ends past the blob")
            handle.seek(start)
        except BaseException:
            handle.close()
            raise
        return BlobReader(_read_file(handle, end - start), handle.close)

    def head(self, key: str) -> BlobInfo | None:
        _check_key(key)
        try:
            status = (self.root / key).stat()
        except (FileNotFoundError, NotADirectoryError):
            return None
        return BlobInfo(size=status.st_size) if stat.S_ISREG(status.st_mode) else None

    def delete(self, key: str) -> None:
        _check_key(key)
        path = self.root / key
        try:
            path.unlink()
        except (FileNotFoundError, NotADirectoryError):
            return
        _fsync_directory(path.parent)

    def sweep(self) -> int:
        """Remove spool files no live writer of this store owns; the count removed."""

        removed = 0
        for path in self.spool.iterdir():
            with self._lock:
                if path.name in self._live_spool_files:
                    continue
                path.unlink(missing_ok=True)
            removed += 1
        if removed:
            _fsync_directory(self.spool)
        return removed

    def _create_spool_file(self) -> tuple[int, Path]:
        with self._lock:
            descriptor, name = tempfile.mkstemp(prefix="put-", dir=self.spool)
            path = Path(name)
            self._live_spool_files.add(path.name)
        return descriptor, path

    def _release_spool_file(self, path: Path) -> None:
        with self._lock:
            self._live_spool_files.discard(path.name)

    def _publish(self, spool_file: Path, destination: Path) -> None:
        self._make_parents(destination.parent)
        os.replace(spool_file, destination)
        _fsync_directory(destination.parent)
        _fsync_directory(self.spool)

    def _make_parents(self, directory: Path) -> None:
        missing: list[Path] = []
        while not directory.is_dir():
            missing.append(directory)
            directory = directory.parent
        for created in reversed(missing):
            try:
                created.mkdir(mode=0o700)
            except FileExistsError:
                continue
            os.chmod(created, 0o700)
            _fsync_directory(created.parent)


class _LocalWriter(_Writer):
    def __init__(self, store: LocalBlobStore, destination: Path) -> None:
        super().__init__()
        self._store = store
        self._destination = destination
        descriptor, self._spool_file = store._create_spool_file()
        self._file = os.fdopen(descriptor, "wb")

    def _append(self, chunk: bytes) -> None:
        self._file.write(chunk)

    def _publish(self) -> None:
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        self._store._publish(self._spool_file, self._destination)
        self._store._release_spool_file(self._spool_file)

    def _discard(self) -> None:
        try:
            with contextlib.suppress(OSError):  # a failed flush of discarded bytes
                self._file.close()
            self._spool_file.unlink(missing_ok=True)
            _fsync_directory(self._store.spool)
        finally:
            self._store._release_spool_file(self._spool_file)


def _read_file(handle: Any, length: int) -> Iterator[bytes]:
    remaining = length
    while remaining:
        chunk = handle.read(min(_READ_CHUNK, remaining))
        if not chunk:
            raise BlobStoreError("the blob ended before the requested range")
        remaining -= len(chunk)
        yield chunk


class S3BlobStore:
    """Blobs as objects under ``bucket/prefix`` of an S3-compatible service."""

    def __init__(self, location: S3BlobLocation) -> None:
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise BlobStoreError(
                "an s3:// blob URL needs boto3: pip install 'earshot-observability[s3]'"
            ) from None
        self.bucket = location.bucket
        self._prefix = f"{location.prefix}/" if location.prefix else ""
        self._client = boto3.session.Session().client(
            "s3",
            endpoint_url=location.endpoint,
            region_name=location.region,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path" if location.path_style else "auto"},
                # Send and check checksums only where S3 requires them: several
                # S3-compatible services reject the default CRC32 multipart headers.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                retries={"mode": "standard"},
            ),
        )
        self._lock = threading.Lock()
        self._live_uploads: set[str] = set()

    def begin_put(self, key: str) -> BlobWriter:
        _check_key(key)
        return _S3Writer(self, self._prefix + key)

    def open_range(self, key: str, start: int, end: int) -> BlobReader:
        _check_key(key)
        _check_range(start, end)
        if start == end:
            # An empty range has no HTTP form; the key must still exist and hold it.
            info = self.head(key)
            if info is None:
                raise BlobNotFoundError("no blob at this key")
            if end > info.size:
                raise ValueError("the blob range ends past the blob")
            return BlobReader(iter(()), lambda: None)
        try:
            response = self._client.get_object(
                Bucket=self.bucket, Key=self._prefix + key, Range=f"bytes={start}-{end - 1}"
            )
        except self._client.exceptions.ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in {"NoSuchKey", "404", "NotFound"}:
                raise BlobNotFoundError("no blob at this key") from None
            if code == "InvalidRange":
                raise ValueError("the blob range ends past the blob") from None
            raise
        body = response["Body"]
        served = re.fullmatch(r"bytes (\d+)-(\d+)/\d+", response.get("ContentRange") or "")
        if served is None or int(served[1]) != start:
            body.close()
            raise BlobStoreError("the S3 service did not serve the requested range")
        if int(served[2]) != end - 1:
            # S3 shortens a range that runs past the object instead of refusing it.
            body.close()
            raise ValueError("the blob range ends past the blob")
        return BlobReader(body.iter_chunks(_READ_CHUNK), body.close)

    def head(self, key: str) -> BlobInfo | None:
        _check_key(key)
        try:
            response = self._client.head_object(Bucket=self.bucket, Key=self._prefix + key)
        except self._client.exceptions.ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        return BlobInfo(size=int(response["ContentLength"]))

    def delete(self, key: str) -> None:
        _check_key(key)
        self._client.delete_object(Bucket=self.bucket, Key=self._prefix + key)

    def sweep(self) -> int:
        """Abort multipart uploads under the prefix that no live writer owns; the count."""

        removed = 0
        # MinIO lists multipart uploads only for an empty Prefix or an exact key,
        # so list the bucket and filter by prefix here.
        paginator = self._client.get_paginator("list_multipart_uploads")
        for page in paginator.paginate(Bucket=self.bucket):
            for upload in page.get("Uploads", []):
                name, upload_id = upload["Key"], upload["UploadId"]
                if not name.startswith(self._prefix):
                    continue
                with self._lock:
                    if upload_id in self._live_uploads:
                        continue
                self._abort_upload(name, upload_id)
                removed += 1
        return removed

    def _create_upload(self, name: str) -> str:
        # Created and registered under the lock, so a concurrent sweep that lists
        # the new upload waits here and then sees it as live.
        with self._lock:
            upload_id = self._client.create_multipart_upload(Bucket=self.bucket, Key=name)[
                "UploadId"
            ]
            self._live_uploads.add(upload_id)
        return upload_id

    def _release_upload(self, upload_id: str) -> None:
        with self._lock:
            self._live_uploads.discard(upload_id)

    def _abort_upload(self, name: str, upload_id: str) -> None:
        try:
            self._client.abort_multipart_upload(Bucket=self.bucket, Key=name, UploadId=upload_id)
        except self._client.exceptions.ClientError as error:
            if error.response.get("Error", {}).get("Code") != "NoSuchUpload":
                raise


class _S3Writer(_Writer):
    # One part buffer per writer, filled in place and sent without a copy. Growing
    # a bytearray and copying it out per part made macOS RSS climb about 1 MiB per
    # MiB uploaded (freed realloc blocks stay resident), which AC8.1 forbids.
    def __init__(self, store: S3BlobStore, name: str) -> None:
        super().__init__()
        self._store = store
        self._name = name
        self._buffer: bytearray | None = None
        self._filled = 0
        self._upload_id: str | None = None
        self._parts: list[dict[str, Any]] = []

    def _append(self, chunk: bytes) -> None:
        view = memoryview(chunk).cast("B")
        while view:
            if self._buffer is None:
                self._buffer = bytearray(PART_SIZE)
            taken = min(PART_SIZE - self._filled, len(view))
            self._buffer[self._filled : self._filled + taken] = view[:taken]
            self._filled += taken
            view = view[taken:]
            if self._filled == PART_SIZE:
                self._upload_part(self._buffer)
                self._filled = 0

    def _pending(self) -> bytes:
        if self._buffer is None:
            return b""
        return bytes(memoryview(self._buffer)[: self._filled])

    def _upload_part(self, body: bytes | bytearray) -> None:
        if len(self._parts) == _MAX_PARTS:
            raise BlobStoreError("the blob exceeds the S3 limit of 10,000 parts")
        store = self._store
        if self._upload_id is None:
            self._upload_id = store._create_upload(self._name)
        number = len(self._parts) + 1
        response = store._client.upload_part(
            Bucket=store.bucket,
            Key=self._name,
            UploadId=self._upload_id,
            PartNumber=number,
            Body=body,
        )
        self._parts.append({"PartNumber": number, "ETag": response["ETag"]})

    def _publish(self) -> None:
        store = self._store
        if self._upload_id is None:
            store._client.put_object(Bucket=store.bucket, Key=self._name, Body=self._pending())
        else:
            if self._filled:
                self._upload_part(self._pending())
            store._client.complete_multipart_upload(
                Bucket=store.bucket,
                Key=self._name,
                UploadId=self._upload_id,
                MultipartUpload={"Parts": self._parts},
            )
            store._release_upload(self._upload_id)
        self._buffer = None

    def _discard(self) -> None:
        self._buffer = None
        if self._upload_id is None:
            return
        try:
            self._store._abort_upload(self._name, self._upload_id)
        finally:
            self._store._release_upload(self._upload_id)
