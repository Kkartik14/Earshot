"""Blob stores for tests: real local disk and a real S3-compatible server.

The repository-root ``conftest.py`` loads this module as a plugin. A test that
asks for ``blob_backend`` runs twice, as ``[local]`` and ``[s3]``:

``local``
    A ``file://`` store under the test's ``tmp_path``.
``s3``
    An ``s3://`` store on the test S3 server: one bucket per session, created on
    first use and emptied and deleted when the session ends, and one prefix per
    test. The store reads its credentials from the standard AWS variables, as in
    production; the fixture sets them for the test and isolates it from any
    ``~/.aws`` configuration.

The test S3 server is ``EARSHOT_TEST_S3_URL``, ``http(s)://ACCESS_KEY:SECRET@host:port``
(optionally ``?region=``), read from the environment or else from the repository's
git-ignored ``.env``. For the local ``earshot-dev-minio`` container that is
``http://<MINIO_ROOT_USER>:<MINIO_ROOT_PASSWORD>@127.0.0.1:59000``. When it is
not set anywhere, the ``s3`` cases skip and name the variable; when it is set, an
unreachable server fails the tests. The URL carries a secret and is never printed.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

import pytest

S3_URL_ENVIRONMENT = "EARSHOT_TEST_S3_URL"
_REPOSITORY = Path(__file__).resolve().parents[3]
_ENV_LINE = re.compile(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
# The standard AWS variables a test S3 store reads, and the ones that would let a
# developer's own AWS configuration leak into it.
_AWS_ISOLATION = {
    "AWS_PROFILE": None,
    "AWS_DEFAULT_PROFILE": None,
    "AWS_SESSION_TOKEN": None,
    "AWS_SECURITY_TOKEN": None,
    "AWS_ROLE_ARN": None,
    "AWS_WEB_IDENTITY_TOKEN_FILE": None,
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": None,
    "AWS_CONTAINER_CREDENTIALS_FULL_URI": None,
    "AWS_ENDPOINT_URL": None,
    "AWS_ENDPOINT_URL_S3": None,
    "AWS_EC2_METADATA_DISABLED": "true",
}


@dataclass
class S3TestServer:
    """The configured S3-compatible test server and this session's bucket."""

    endpoint: str
    region: str
    access_key_id: str
    secret_access_key: str
    bucket: str = field(
        default_factory=lambda: f"earshot-test-{os.getpid()}-{secrets.token_hex(4)}"
    )
    _admin: Any = None

    @property
    def admin(self) -> Any:
        """A boto3 client with the test credentials, for setup and inspection."""

        if self._admin is None:
            import boto3
            from botocore.config import Config

            self._admin = boto3.client(
                "s3",
                endpoint_url=self.endpoint,
                region_name=self.region,
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.secret_access_key,
                config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
            )
        return self._admin

    def environment(self) -> dict[str, str | None]:
        """The AWS variables a store under test reads (``None`` unsets one)."""

        return {
            **_AWS_ISOLATION,
            "AWS_ACCESS_KEY_ID": self.access_key_id,
            "AWS_SECRET_ACCESS_KEY": self.secret_access_key,
        }

    def blob_url(self, prefix: str) -> str:
        query = urlencode({"endpoint": self.endpoint, "region": self.region, "path_style": "1"})
        return f"s3://{self.bucket}/{prefix}?{query}"

    def _uploads(self) -> list[tuple[str, str]]:
        # MinIO lists multipart uploads only for an empty Prefix or an exact key,
        # so list the whole bucket and filter here.
        paginator = self.admin.get_paginator("list_multipart_uploads")
        return [
            (upload["Key"], upload["UploadId"])
            for page in paginator.paginate(Bucket=self.bucket)
            for upload in page.get("Uploads", [])
        ]

    def uploads_in_progress(self, prefix: str) -> list[str]:
        """Object names under ``prefix/`` with an in-progress multipart upload."""

        return sorted(name for name, _ in self._uploads() if name.startswith(f"{prefix}/"))

    def object_names(self, prefix: str = "") -> list[str]:
        """Committed object names, under ``prefix/`` when one is given."""

        paginator = self.admin.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=self.bucket, Prefix=f"{prefix}/" if prefix else "")
        return sorted(item["Key"] for page in pages for item in page.get("Contents", []))

    def create_bucket(self) -> None:
        self.admin.create_bucket(Bucket=self.bucket)

    def delete_bucket(self) -> None:
        for name, upload_id in self._uploads():
            self.admin.abort_multipart_upload(Bucket=self.bucket, Key=name, UploadId=upload_id)
        for name in self.object_names():
            self.admin.delete_object(Bucket=self.bucket, Key=name)
        self.admin.delete_bucket(Bucket=self.bucket)


def _configured_s3_url() -> str | None:
    value = os.environ.get(S3_URL_ENVIRONMENT)
    if value:
        return value
    env_file = _REPOSITORY / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            match = _ENV_LINE.match(line)
            if match and match.group(1) == S3_URL_ENVIRONMENT:
                return match.group(2).strip().strip("'\"") or None
    return None


def _parse_s3_url(raw: str) -> S3TestServer:
    parts = urlsplit(raw)
    options = dict(parse_qsl(parts.query))
    if parts.scheme not in {"http", "https"} or not parts.hostname or not parts.password:
        # Never echo the value: it carries the secret key.
        raise pytest.UsageError(
            f"{S3_URL_ENVIRONMENT} must be http(s)://ACCESS_KEY:SECRET@host:port[?region=]"
        )
    port = f":{parts.port}" if parts.port else ""
    return S3TestServer(
        endpoint=f"{parts.scheme}://{parts.hostname}{port}",
        region=options.get("region", "us-east-1"),
        access_key_id=unquote(parts.username or ""),
        secret_access_key=unquote(parts.password),
    )


_server: S3TestServer | None = None
_bucket_ready = False


@pytest.fixture(scope="session")
def s3_test_server() -> S3TestServer:
    """The test S3 server with this session's bucket; skips when none is configured."""

    global _server, _bucket_ready
    if _server is None:
        raw = _configured_s3_url()
        if raw is None:
            pytest.skip(f"no S3 test server: set {S3_URL_ENVIRONMENT} (see blob_support.py)")
        _server = _parse_s3_url(raw)
    if not _bucket_ready:
        _server.create_bucket()
        _bucket_ready = True
    return _server


@dataclass
class BlobBackend:
    """One backend under test: its store, URL and the places leftovers would show."""

    name: str
    url: str
    store: Any
    environment: dict[str, str]
    _root: Path | None = None
    _server: S3TestServer | None = None
    _prefix: str = ""

    def reopen(self) -> Any:
        """A fresh store on the same location, as a restarted server would open it."""

        from earshot.blobstore import open_blob_store

        return open_blob_store(self.url)

    def objects(self, prefix: str = "") -> list[str]:
        """Every committed object of this store whose key starts with ``prefix``
        (``"recordings/"``: uploads only, not what a server's analysis derived)."""

        if self._server is not None:
            start = len(self._prefix) + 1
            names = [name[start:] for name in self._server.object_names(self._prefix)]
            return [name for name in names if name.startswith(prefix)]
        assert self._root is not None
        return sorted(
            key
            for path in self._root.rglob("*")
            if path.is_file() and ".tmp" not in path.relative_to(self._root).parts
            if (key := path.relative_to(self._root).as_posix()).startswith(prefix)
        )

    def leftovers(self) -> list[str]:
        """Uncommitted writes this backend still holds: spool files or S3 uploads."""

        if self._server is not None:
            start = len(self._prefix) + 1
            return [name[start:] for name in self._server.uploads_in_progress(self._prefix)]
        assert self._root is not None
        spool = self._root / ".tmp"
        return sorted(path.name for path in spool.iterdir()) if spool.is_dir() else []


@pytest.fixture(params=["local", "s3"])
def blob_backend(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[BlobBackend]:
    from earshot.blobstore import open_blob_store

    if request.param == "local":
        root = tmp_path / "blobs"
        url = root.as_uri()
        yield BlobBackend("local", url, open_blob_store(url), {}, _root=root)
        return
    server = request.getfixturevalue("s3_test_server")
    environment = server.environment()
    for name, value in environment.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    # Keep the developer's ~/.aws files out of the store's credential chain.
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-aws-credentials"))
    prefix = f"t-{secrets.token_hex(6)}"
    url = server.blob_url(prefix)
    backend = BlobBackend(
        "s3",
        url,
        open_blob_store(url),
        {name: value for name, value in os.environ.items() if name.startswith("AWS_")},
        _server=server,
        _prefix=prefix,
    )
    yield backend


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if _server is not None and _bucket_ready:
        with contextlib.suppress(Exception):
            _server.delete_bucket()


def child_environment(backend: BlobBackend) -> dict[str, str]:
    """The environment for a subprocess that opens ``backend`` through the server config."""

    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("EARSHOT_", "AWS_"))
    }
    environment.update(backend.environment)
    environment["EARSHOT_BLOB_URL"] = backend.url
    environment["PYTHONPATH"] = os.pathsep.join(
        [imported_package_root(), *filter(None, [environment.get("PYTHONPATH")])]
    )
    return environment


def imported_package_root() -> str:
    """The directory holding the ``earshot`` package this test process imported,
    so a subprocess runs the same code."""

    import earshot

    return str(Path(earshot.__file__).resolve().parents[1])


PYTHON = sys.executable
