from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from earshot.exporter import (
    DurableExporter,
    ExportItem,
    PermanentExportError,
    RetryableExportError,
    _coerce_spool_key,
    _resolve_spool_key,
)

pytestmark = pytest.mark.unit

_STANDALONE_FINGERPRINT = hashlib.sha256(b"earshot.standalone").hexdigest()

KEY_A = b"\x11" * 32
KEY_B = b"\x22" * 32
KEY_C = b"\x33" * 32


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not reached")
        time.sleep(0.005)


class _Unavailable:
    def send(self, _item: ExportItem) -> None:
        raise RetryableExportError("unavailable")


class _Rejecting:
    def send(self, _item: ExportItem) -> None:
        raise PermanentExportError("rejected")


class _Recording:
    def __init__(self) -> None:
        self.items: list[ExportItem] = []

    def send(self, item: ExportItem) -> None:
        self.items.append(item)


def _durable(transport, spool_dir: Path, **kwargs) -> DurableExporter:
    return DurableExporter(
        transport,
        spool_dir=spool_dir,
        max_attempts=1,
        base_backoff=0,
        max_elapsed=0.1,
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _clear_spool_key_env(monkeypatch):
    # The exporter resolves keys from the environment; keep every test hermetic.
    monkeypatch.delenv("EARSHOT_SPOOL_KEY", raising=False)
    monkeypatch.delenv("EARSHOT_SPOOL_KEY_FILE", raising=False)


def test_spool_record_is_encrypted_at_rest(tmp_path) -> None:
    payload = b"secret-\x00-transcript-payload"
    exporter = _durable(_Unavailable(), tmp_path, spool_key=KEY_A)
    try:
        assert exporter.submit(ExportItem("bundle-1", payload))
        _wait_until(lambda: len(list(tmp_path.glob("*.spool"))) == 1)

        raw = next(tmp_path.glob("*.spool")).read_bytes()
        # The plaintext, a recognizable prefix, and its base64 form are all absent.
        assert payload not in raw
        assert b"secret-" not in raw
        assert base64.b64encode(payload) not in raw
        assert b"payload_base64" not in raw

        record = json.loads(raw)
        assert record["spool_format"] == "aesgcm-v1"
        assert base64.b64decode(record["ciphertext_b64"]) != payload
        assert record["payload_sha256"] == hashlib.sha256(payload).hexdigest()
    finally:
        assert exporter.shutdown()


def test_key_decrypts_and_delivers_exact_original_bytes(tmp_path) -> None:
    transport = _Recording()
    item = ExportItem("bundle-1", b"\x00\xffexact-secret-bytes", "application/custom")
    exporter = _durable(transport, tmp_path, spool_key=KEY_A)
    try:
        assert exporter.submit(item)
        assert exporter.flush(2)
        assert transport.items == [item]
        assert exporter.status().sent == 1
        assert list(tmp_path.glob("*.spool")) == []
    finally:
        assert exporter.shutdown()


def test_encrypted_record_survives_restart_with_same_key(tmp_path) -> None:
    item = ExportItem("restart-id", b"\x00durable-secret", "application/custom")
    first = _durable(_Unavailable(), tmp_path, spool_key=KEY_A)
    assert first.submit(item)
    _wait_until(lambda: len(list(tmp_path.glob("*.spool"))) == 1)
    assert first.shutdown()

    transport = _Recording()
    second = _durable(transport, tmp_path, spool_key=KEY_A)
    try:
        assert second.flush(2)
        assert transport.items == [item]
        assert second.status().replayed == 1
        assert second.status().spool_depth == 0
    finally:
        assert second.shutdown()


def test_wrong_key_quarantines_and_never_delivers(tmp_path) -> None:
    writer = _durable(_Unavailable(), tmp_path, spool_key=KEY_A)
    assert writer.submit(ExportItem("bundle-1", b"secret"))
    _wait_until(lambda: len(list(tmp_path.glob("*.spool"))) == 1)
    assert writer.shutdown()

    transport = _Recording()
    diagnostics = []
    reader = _durable(transport, tmp_path, spool_key=KEY_B, diagnostic=diagnostics.append)
    try:
        _wait_until(lambda: reader.status().abandoned == 1)
        assert transport.items == []
        assert list(tmp_path.glob("*.spool")) == []
        assert len(list((tmp_path / "quarantine").glob("*.corrupt"))) == 1
        assert diagnostics[-1].code == "exporter.spool_corrupt"
    finally:
        assert reader.shutdown()


def test_key_erasure_makes_previously_written_records_unreadable(tmp_path) -> None:
    writer = _durable(_Unavailable(), tmp_path, spool_key=KEY_A)
    assert writer.submit(ExportItem("bundle-1", b"secret"))
    _wait_until(lambda: len(list(tmp_path.glob("*.spool"))) == 1)
    assert writer.shutdown()

    # Crypto-shredding: with the key destroyed, a reader with no key cannot
    # decrypt the record and must quarantine it rather than deliver or crash.
    transport = _Recording()
    reader = _durable(transport, tmp_path)
    try:
        _wait_until(lambda: reader.status().abandoned == 1)
        assert transport.items == []
        assert list(tmp_path.glob("*.spool")) == []
        assert len(list((tmp_path / "quarantine").glob("*.corrupt"))) == 1
    finally:
        assert reader.shutdown()


def test_aad_binding_rejects_bundle_id_swap(tmp_path) -> None:
    writer = _durable(_Unavailable(), tmp_path, spool_key=KEY_A)
    assert writer.submit(ExportItem("bundle-original", b"secret"))
    _wait_until(lambda: len(list(tmp_path.glob("*.spool"))) == 1)
    assert writer.shutdown()

    spool_file = next(tmp_path.glob("*.spool"))
    record = json.loads(spool_file.read_bytes())
    record["bundle_id"] = "bundle-swapped"
    spool_file.write_bytes(json.dumps(record).encode())
    os.chmod(spool_file, 0o600)

    transport = _Recording()
    reader = _durable(transport, tmp_path, spool_key=KEY_A)
    try:
        _wait_until(lambda: reader.status().abandoned == 1)
        assert transport.items == []
        assert len(list((tmp_path / "quarantine").glob("*.corrupt"))) == 1
    finally:
        assert reader.shutdown()


def test_aad_binding_rejects_route_move(tmp_path) -> None:
    route_a = "a" * 64
    route_b = "b" * 64
    writer = _durable(_Unavailable(), tmp_path, spool_key=KEY_A, destination_fingerprint=route_a)
    assert writer.submit(ExportItem("bundle-1", b"secret"))
    _wait_until(lambda: len(list(tmp_path.glob(f"{route_a}-*.spool"))) == 1)
    assert writer.shutdown()

    # Relabel the record so it appears to belong to route B (matching filename and
    # cleartext fingerprint field). The AAD still binds route A, so authenticated
    # decryption under route B fails even though the key is identical.
    original = next(tmp_path.glob(f"{route_a}-*.spool"))
    record = json.loads(original.read_bytes())
    record["destination_fingerprint"] = route_b
    moved = tmp_path / original.name.replace(route_a, route_b, 1)
    moved.write_bytes(json.dumps(record).encode())
    os.chmod(moved, 0o600)
    original.unlink()

    transport = _Recording()
    reader = _durable(transport, tmp_path, spool_key=KEY_A, destination_fingerprint=route_b)
    try:
        _wait_until(lambda: reader.status().abandoned == 1)
        assert transport.items == []
        assert len(list((tmp_path / "quarantine").glob("*.corrupt"))) == 1
    finally:
        assert reader.shutdown()


def test_tampered_ciphertext_quarantines(tmp_path) -> None:
    writer = _durable(_Unavailable(), tmp_path, spool_key=KEY_A)
    assert writer.submit(ExportItem("bundle-1", b"secret"))
    _wait_until(lambda: len(list(tmp_path.glob("*.spool"))) == 1)
    assert writer.shutdown()

    spool_file = next(tmp_path.glob("*.spool"))
    record = json.loads(spool_file.read_bytes())
    ciphertext = bytearray(base64.b64decode(record["ciphertext_b64"]))
    ciphertext[0] ^= 0xFF
    record["ciphertext_b64"] = base64.b64encode(bytes(ciphertext)).decode("ascii")
    spool_file.write_bytes(json.dumps(record).encode())
    os.chmod(spool_file, 0o600)

    transport = _Recording()
    reader = _durable(transport, tmp_path, spool_key=KEY_A)
    try:
        _wait_until(lambda: reader.status().abandoned == 1)
        assert transport.items == []
        assert len(list((tmp_path / "quarantine").glob("*.corrupt"))) == 1
    finally:
        assert reader.shutdown()


def test_requested_encryption_without_cryptography_fails_closed(tmp_path, monkeypatch) -> None:
    def _no_cryptography() -> type:
        raise ImportError("No module named 'cryptography'")

    monkeypatch.setattr("earshot.exporter._import_aesgcm", _no_cryptography)
    with pytest.raises(RuntimeError, match="spool-encryption"):
        _durable(_Recording(), tmp_path, spool_key=KEY_A)
    # Fail closed: no plaintext record was ever written as a fallback.
    assert list(tmp_path.glob("*.spool")) == []


def test_no_key_keeps_plaintext_spool_record(tmp_path) -> None:
    exporter = _durable(_Unavailable(), tmp_path)
    try:
        assert exporter.submit(ExportItem("bundle-1", b"plain-payload"))
        _wait_until(lambda: len(list(tmp_path.glob("*.spool"))) == 1)
        record = json.loads(next(tmp_path.glob("*.spool")).read_bytes())
        assert "payload_base64" in record
        assert "spool_format" not in record
        assert base64.b64decode(record["payload_base64"]) == b"plain-payload"
    finally:
        assert exporter.shutdown()


def test_env_base64_key_encrypts_and_round_trips(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EARSHOT_SPOOL_KEY", base64.b64encode(KEY_A).decode("ascii"))
    transport = _Recording()
    item = ExportItem("bundle-1", b"env-key-secret")
    exporter = _durable(transport, tmp_path)
    try:
        assert exporter.submit(item)
        _wait_until(lambda: transport.items == [item])
    finally:
        assert exporter.shutdown()


def test_key_file_resolution_with_secure_mode(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "spool.key"
    key_file.write_bytes(base64.b64encode(KEY_A))
    os.chmod(key_file, 0o600)
    monkeypatch.setenv("EARSHOT_SPOOL_KEY_FILE", str(key_file))

    transport = _Recording()
    item = ExportItem("bundle-1", b"file-key-secret")
    exporter = _durable(transport, tmp_path / "spool")
    try:
        assert exporter.submit(item)
        _wait_until(lambda: transport.items == [item])
    finally:
        assert exporter.shutdown()


def test_insecure_key_file_is_rejected(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "spool.key"
    key_file.write_bytes(base64.b64encode(KEY_A))
    os.chmod(key_file, 0o644)
    monkeypatch.setenv("EARSHOT_SPOOL_KEY_FILE", str(key_file))
    with pytest.raises(ValueError, match="group or other"):
        _durable(_Recording(), tmp_path / "spool")


def test_spool_key_resolution_precedence(tmp_path, monkeypatch) -> None:
    key_file = tmp_path / "k"
    key_file.write_bytes(base64.b64encode(KEY_C))
    os.chmod(key_file, 0o600)
    monkeypatch.setenv("EARSHOT_SPOOL_KEY", base64.b64encode(KEY_B).decode("ascii"))
    monkeypatch.setenv("EARSHOT_SPOOL_KEY_FILE", str(key_file))

    # Explicit argument beats every environment source.
    assert _resolve_spool_key(KEY_A) == KEY_A
    # Inline env key beats the key file.
    assert _resolve_spool_key(None) == KEY_B
    # The file is used only when the inline env key is absent.
    monkeypatch.delenv("EARSHOT_SPOOL_KEY")
    assert _resolve_spool_key(None) == KEY_C
    # Nothing configured means plaintext.
    monkeypatch.delenv("EARSHOT_SPOOL_KEY_FILE")
    assert _resolve_spool_key(None) is None


def test_coerce_spool_key_accepts_raw_and_base64_and_rejects_bad_length() -> None:
    assert _coerce_spool_key(KEY_A) == KEY_A
    assert _coerce_spool_key(base64.b64encode(KEY_A).decode("ascii")) == KEY_A
    assert _coerce_spool_key(base64.b64encode(KEY_A)) == KEY_A
    with pytest.raises(ValueError):
        _coerce_spool_key(b"too-short")
    with pytest.raises(ValueError, match="32 bytes"):
        _coerce_spool_key(base64.b64encode(b"x" * 16).decode("ascii"))


def test_rejecting_transport_retains_encrypted_record(tmp_path) -> None:
    exporter = _durable(
        _Rejecting(),
        tmp_path,
        spool_key=KEY_A,
        permanent_rejection_policy="retain",
    )
    try:
        assert exporter.submit(ExportItem("rejected", b"secret"))
        _wait_until(lambda: exporter.status().rejected == 1)
        retained = list(tmp_path.glob("*.rejected"))
        assert len(retained) == 1
        # Even the retained rejection stays ciphertext on disk.
        record = json.loads(retained[0].read_bytes())
        assert record["spool_format"] == "aesgcm-v1"
        assert b"secret" not in retained[0].read_bytes()
    finally:
        assert exporter.shutdown()
