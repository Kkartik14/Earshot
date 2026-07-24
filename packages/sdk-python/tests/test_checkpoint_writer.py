from __future__ import annotations

import base64
import json
import os
import secrets
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from earshot.checkpoint import (
    CheckpointConfig,
    CheckpointWriter,
    JournalReader,
    RecordMutation,
)
from earshot.checkpoint.keys import AT_REST_NONCE_BYTES
from earshot.checkpoint.reader import JournalUnreadableError
from earshot.checkpoint.records import (
    REASON_JOURNAL_FULL,
    JournalExhausted,
    JournalOpen,
    JournalRecordEntry,
    journal_frame_aad,
)
from earshot.recorder import IncidentRecorder
from incident_factory import SECRET_SENTINEL

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
SDK_SRC = ROOT / "packages" / "sdk-python" / "src"


def _writer(directory: Path, **kwargs) -> CheckpointWriter:
    return CheckpointWriter(CheckpointConfig(checkpoint_dir=directory, **kwargs))


def _journal_path(directory: Path) -> Path:
    return next(directory.glob("*.eck"))


def _record_a_few(recorder: IncidentRecorder, count: int = 3) -> None:
    recorder.add_participant("caller", role="caller")
    for index in range(count):
        recorder.record_event("earshot.turn.start", turn_id=f"turn-{index}")


# --------------------------------------------------------------- filesystem


def test_the_journal_directory_and_file_are_owner_private(tmp_path: Path) -> None:
    writer = _writer(tmp_path / "journals")
    IncidentRecorder(session_id="s", bundle_id="b", checkpoint=writer)

    path = _journal_path(tmp_path / "journals")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "journals").stat().st_mode) == 0o700
    writer.release()


def test_a_symlinked_checkpoint_directory_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(ValueError, match="symbolic link"):
        _writer(link)


def test_the_journal_filename_never_carries_a_caller_identifier(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    IncidentRecorder(session_id=SECRET_SENTINEL, bundle_id=SECRET_SENTINEL, checkpoint=writer)

    assert SECRET_SENTINEL not in _journal_path(tmp_path).name
    writer.release()


def test_a_configured_key_without_cryptography_refuses_to_construct(tmp_path, monkeypatch) -> None:
    def unavailable() -> type:
        raise ImportError("cryptography is not installed")

    monkeypatch.setattr("earshot.checkpoint.writer.import_aesgcm", unavailable)

    with pytest.raises(RuntimeError, match="cryptography"):
        _writer(tmp_path, checkpoint_key=base64.b64encode(secrets.token_bytes(32)).decode())


def test_the_journal_inherits_the_spool_key_when_no_checkpoint_key_is_set(
    tmp_path: Path, monkeypatch
) -> None:
    key = secrets.token_bytes(32)
    monkeypatch.setenv("EARSHOT_SPOOL_KEY", base64.b64encode(key).decode())
    writer = _writer(tmp_path)
    recorder = IncidentRecorder(session_id="s", bundle_id="b", checkpoint=writer)
    _record_a_few(recorder)

    raw = _journal_path(tmp_path).read_bytes()
    assert b'"session_id"' not in raw
    replay = JournalReader(_journal_path(tmp_path), key=key).read()
    assert replay.header.session_id == "s"
    writer.release()


# ----------------------------------------------------------------- recovery


def test_a_sigkilled_child_loses_zero_admitted_records(tmp_path: Path) -> None:
    """The kernel owns an appended frame the moment ``write`` returns."""

    directory = tmp_path / "journals"
    script = textwrap.dedent(
        f"""
        import os, signal, sys
        sys.path.insert(0, {str(SDK_SRC)!r})
        from earshot.checkpoint import CheckpointConfig, CheckpointWriter
        from earshot.recorder import IncidentRecorder

        writer = CheckpointWriter(
            CheckpointConfig(checkpoint_dir={str(directory)!r}, fsync_mode="never")
        )
        recorder = IncidentRecorder(session_id="crashed", bundle_id="crashed", checkpoint=writer)
        recorder.add_participant("caller", role="caller")
        for index in range(50):
            recorder.record_event("earshot.turn.start", turn_id=f"turn-{{index}}")
        sys.stdout.flush()
        os.kill(os.getpid(), signal.SIGKILL)
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, timeout=60, check=False
    )

    assert completed.returncode == -9, completed.stderr.decode()
    replay = JournalReader(_journal_path(directory)).read()
    events = [
        entry
        for entry in replay.entries
        if isinstance(entry, JournalRecordEntry) and entry.kind == "event"
    ]
    assert len(events) == 50
    assert replay.torn_tail_bytes == 0
    assert replay.close_observed is False


def test_a_hard_exit_child_loses_zero_admitted_records(tmp_path: Path) -> None:
    directory = tmp_path / "journals"
    script = textwrap.dedent(
        f"""
        import os, sys
        sys.path.insert(0, {str(SDK_SRC)!r})
        from earshot.checkpoint import CheckpointConfig, CheckpointWriter
        from earshot.recorder import IncidentRecorder

        writer = CheckpointWriter(
            CheckpointConfig(checkpoint_dir={str(directory)!r}, fsync_mode="never")
        )
        recorder = IncidentRecorder(session_id="halted", bundle_id="halted", checkpoint=writer)
        for index in range(25):
            recorder.record_event("earshot.turn.start", turn_id=f"turn-{{index}}")
        os._exit(1)
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, timeout=60, check=False
    )

    assert completed.returncode == 1, completed.stderr.decode()
    replay = JournalReader(_journal_path(directory)).read()
    assert (
        sum(
            1
            for entry in replay.entries
            if isinstance(entry, JournalRecordEntry) and entry.kind == "event"
        )
        == 25
    )


# ------------------------------------------------------------------ failure


def test_a_partial_write_degrades_the_writer_and_stops_further_frames(
    tmp_path: Path, monkeypatch
) -> None:
    diagnostics: list[object] = []
    writer = CheckpointWriter(
        CheckpointConfig(checkpoint_dir=tmp_path), diagnostic=diagnostics.append
    )
    recorder = IncidentRecorder(session_id="s", bundle_id="b", checkpoint=writer)
    path = _journal_path(tmp_path)
    intact = path.stat().st_size

    real_write = os.write

    def short_write(descriptor: int, data: bytes) -> int:
        return real_write(descriptor, data[: max(1, len(data) // 2)])

    monkeypatch.setattr("earshot.checkpoint.writer.os.write", short_write)
    recorder.record_event("earshot.turn.start", turn_id="turn-0")
    monkeypatch.setattr("earshot.checkpoint.writer.os.write", real_write)
    recorder.record_event("earshot.turn.start", turn_id="turn-1")

    status = writer.status()
    assert status.degraded is True
    assert status.journal_complete is False
    assert [str(item.code) for item in diagnostics] == ["checkpoint.write_failed"]
    replay = JournalReader(path).read()
    assert replay.entries == ()
    assert replay.torn_tail_bytes == path.stat().st_size - intact
    writer.release()


def test_reaching_the_byte_cap_records_the_reason_instead_of_stopping_silently(
    tmp_path: Path,
) -> None:
    diagnostics: list[object] = []
    writer = CheckpointWriter(
        CheckpointConfig(checkpoint_dir=tmp_path, max_journal_bytes=1500),
        diagnostic=diagnostics.append,
    )
    recorder = IncidentRecorder(session_id="s", bundle_id="b", checkpoint=writer)
    for index in range(40):
        recorder.record_event("earshot.turn.start", turn_id=f"turn-{index}")

    status = writer.status()
    assert status.journal_complete is False
    assert status.dropped_records > 0
    assert [str(item.code) for item in diagnostics] == ["checkpoint.journal_full"]
    replay = JournalReader(_journal_path(tmp_path)).read()
    assert isinstance(replay.entries[-1], JournalExhausted)
    assert replay.entries[-1].reason == REASON_JOURNAL_FULL
    writer.release()


def test_reaching_the_record_cap_stops_the_journal(tmp_path: Path) -> None:
    writer = _writer(tmp_path, max_records=5)
    recorder = IncidentRecorder(session_id="s", bundle_id="b", checkpoint=writer)
    for index in range(20):
        recorder.record_event("earshot.turn.start", turn_id=f"turn-{index}")

    replay = JournalReader(_journal_path(tmp_path)).read()
    assert isinstance(replay.entries[-1], JournalExhausted)
    assert writer.status().journal_complete is False
    writer.release()


# -------------------------------------------------------------------- fsync


@pytest.mark.parametrize(
    ("mode", "expected_beyond_header"),
    [("always", True), ("never", False)],
)
def test_fsync_mode_controls_per_record_syncing(
    tmp_path: Path, monkeypatch, mode: str, expected_beyond_header: bool
) -> None:
    calls: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(
        "earshot.checkpoint.writer.os.fsync",
        lambda descriptor: (calls.append(descriptor), real_fsync(descriptor))[1],
    )
    writer = _writer(tmp_path, fsync_mode=mode)
    recorder = IncidentRecorder(session_id="s", bundle_id="b", checkpoint=writer)
    header_syncs = len(calls)
    assert header_syncs == 1  # the header is always fsynced regardless of mode

    for index in range(3):
        recorder.record_event("earshot.turn.start", turn_id=f"turn-{index}")

    assert (len(calls) > header_syncs) is expected_beyond_header
    recorder.close()
    # The finalize frame is always fsynced too: losing it would silently
    # downgrade a clean close to a recovered artifact.
    assert len(calls) > header_syncs


# --------------------------------------------------------------- encryption


def test_a_frame_cannot_be_moved_between_journals_or_re_sequenced(tmp_path: Path) -> None:
    key = secrets.token_bytes(32)
    writer = _writer(tmp_path, checkpoint_key=key)
    recorder = IncidentRecorder(session_id="s", bundle_id="b", checkpoint=writer)
    _record_a_few(recorder, count=2)
    writer.release()
    path = _journal_path(tmp_path)
    replay = JournalReader(path, key=key).read()
    assert len(replay.entries) >= 2

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    cipher = AESGCM(key)
    raw = path.read_bytes()
    body_start = 9
    header_len = int.from_bytes(raw[5:9], "big")
    header_body = raw[body_start : body_start + header_len]
    plaintext = cipher.decrypt(
        header_body[:AT_REST_NONCE_BYTES],
        header_body[AT_REST_NONCE_BYTES:],
        journal_frame_aad(None, 1),
    )

    with pytest.raises(Exception):  # noqa: B017 - any AEAD failure is the point
        cipher.decrypt(
            header_body[:AT_REST_NONCE_BYTES],
            header_body[AT_REST_NONCE_BYTES:],
            journal_frame_aad(None, 2),
        )
    assert json.loads(plaintext)["k"] == "open"


def test_an_encrypted_journal_read_without_its_key_is_unreadable_not_a_crash(
    tmp_path: Path,
) -> None:
    writer = _writer(tmp_path, checkpoint_key=secrets.token_bytes(32))
    IncidentRecorder(session_id="s", bundle_id="b", checkpoint=writer)
    writer.release()

    with pytest.raises(JournalUnreadableError):
        JournalReader(_journal_path(tmp_path)).read()
    summary = JournalReader(_journal_path(tmp_path)).summarize()
    assert summary.state == "unreadable"
    assert summary.journal_id is None


def test_the_wrong_key_is_unreadable_rather_than_half_decoded(tmp_path: Path) -> None:
    writer = _writer(tmp_path, checkpoint_key=secrets.token_bytes(32))
    IncidentRecorder(session_id="s", bundle_id="b", checkpoint=writer)
    writer.release()

    with pytest.raises(JournalUnreadableError):
        JournalReader(_journal_path(tmp_path), key=secrets.token_bytes(32)).read()


# ------------------------------------------------------------------ privacy


def test_no_governed_sensitive_source_reaches_the_journal_bytes(tmp_path: Path) -> None:
    """Only classes the policy already admits are journaled."""

    writer = _writer(tmp_path, keep_finalized=True)
    recorder = IncidentRecorder(session_id="s", bundle_id="b", checkpoint=writer)
    recorder.add_participant(
        "caller",
        role="caller",
        attributes={"earshot.transcript.text": SECRET_SENTINEL},
    )
    recorder.record_event(
        "earshot.stt.final",
        attributes={
            "gen_ai.input.messages": SECRET_SENTINEL,
            "earshot.tool.arguments": SECRET_SENTINEL,
        },
    )
    with recorder.operation(SECRET_SENTINEL):
        pass
    recorder.close()

    raw = _journal_path(tmp_path).read_bytes()
    assert SECRET_SENTINEL.encode() not in raw


def test_the_journal_holds_only_the_admitted_governed_record(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    recorder = IncidentRecorder(session_id="s", bundle_id="b", checkpoint=writer)
    recorder.record_event("earshot.turn.start", attributes={"gen_ai.request.model": "m"})

    replay = JournalReader(_journal_path(tmp_path)).read()
    events = [
        entry
        for entry in replay.entries
        if isinstance(entry, JournalRecordEntry) and entry.kind == "event"
    ]
    assert events[0].value is not None
    assert events[0].value["attributes"] == {"gen_ai.request.model": "m"}
    writer.release()


# ------------------------------------------------------------------ release


def test_a_finalized_journal_is_removed_only_once_it_has_a_successor(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    recorder = IncidentRecorder(session_id="s", bundle_id="b", checkpoint=writer)
    recorder.close()

    assert list(tmp_path.glob("*.eck")) == []


def test_keeping_a_finalized_journal_is_an_explicit_choice(tmp_path: Path) -> None:
    writer = _writer(tmp_path, keep_finalized=True)
    recorder = IncidentRecorder(session_id="s", bundle_id="b", checkpoint=writer)
    recorder.close()

    assert len(list(tmp_path.glob("*.eck"))) == 1
    assert JournalReader(_journal_path(tmp_path)).read().close_observed is True


# ----------------------------------------------------------------- overhead


def test_appending_a_frame_stays_off_the_voice_path_budget(tmp_path: Path) -> None:
    """A journal append is one ``os.write`` into the page cache, not an fsync."""

    import time

    writer = _writer(tmp_path, fsync_mode="never")
    recorder = IncidentRecorder(session_id="s", bundle_id="b", checkpoint=writer)
    event = recorder.record_event("earshot.turn.start", turn_id="warmup")

    samples: list[float] = []
    for _ in range(500):
        started = time.perf_counter()
        writer.append_record(RecordMutation(kind="event", record=event))
        samples.append(time.perf_counter() - started)
    samples.sort()
    p99 = samples[int(len(samples) * 0.99)]

    # Generous versus the design's 100 us reference-machine gate so a loaded CI
    # box cannot make this flaky; it still catches an fsync or a lock appearing
    # on the append path, which are milliseconds, not microseconds.
    assert p99 < 0.005, f"p99 append was {p99 * 1e6:.1f} us"
    writer.release()


# ------------------------------------------------------------- sdk wiring


def test_checkpointing_is_off_until_a_directory_is_configured(tmp_path: Path) -> None:
    import earshot

    client = earshot.Client()
    try:
        recorder = client.session()
        assert recorder.checkpoint_status().state == "disabled"
        assert client.config.checkpoint_dir is None
        recorder.close()
    finally:
        client.shutdown()


def test_an_explicit_directory_journals_every_conversation(tmp_path: Path) -> None:
    import earshot

    directory = tmp_path / "journals"
    client = earshot.Client(checkpoint_dir=directory, checkpoint_fsync_mode="never")
    try:
        recorder = client.session(session_id="wired", bundle_id="wired")
        recorder.add_participant("caller", role="caller")
        status = recorder.checkpoint_status()
        assert status.state == "open"
        assert status.last_sequence >= 2
        assert Path(str(status.path)).is_file()
        recorder.close()
        # A clean close removes the journal once the incident has a successor.
        assert list(directory.glob("*.eck")) == []
    finally:
        client.shutdown()


def test_the_checkpoint_directory_can_come_from_the_environment(
    tmp_path: Path, monkeypatch
) -> None:
    import earshot

    directory = tmp_path / "journals"
    monkeypatch.setenv("EARSHOT_CHECKPOINT_DIR", str(directory))
    monkeypatch.setenv("EARSHOT_CHECKPOINT_FSYNC_MODE", "never")
    earshot.shutdown()
    try:
        client = earshot.init()
        assert client.config.checkpoint_dir == str(directory)
        assert client.config.checkpoint_fsync_mode == "never"
    finally:
        # Drop the patched variables BEFORE reconfiguring: ``configure()`` re-reads
        # the environment, so reconfiguring while they are still set would write
        # this test's checkpoint settings back into the process-global config and
        # leave every later ``init()`` disagreeing with it.
        earshot.shutdown()
        monkeypatch.delenv("EARSHOT_CHECKPOINT_DIR", raising=False)
        monkeypatch.delenv("EARSHOT_CHECKPOINT_FSYNC_MODE", raising=False)
        earshot.configure()


def test_an_invalid_checkpoint_fsync_mode_is_refused(tmp_path: Path) -> None:
    import earshot

    with pytest.raises(ValueError, match="checkpoint_fsync_mode"):
        earshot.Client(checkpoint_dir=tmp_path, checkpoint_fsync_mode="sometimes")


def test_a_journal_with_no_readable_header_is_unreadable(tmp_path: Path) -> None:
    path = tmp_path / "broken.eck"
    path.write_bytes(b"\x00" * 64)

    with pytest.raises(JournalUnreadableError):
        JournalReader(path).read()


def test_a_second_header_is_refused_so_two_sessions_cannot_be_spliced(tmp_path: Path) -> None:
    writer_a = _writer(tmp_path / "a", keep_finalized=True)
    IncidentRecorder(session_id="a", bundle_id="a", checkpoint=writer_a)
    writer_a.release()
    writer_b = _writer(tmp_path / "b", keep_finalized=True)
    IncidentRecorder(session_id="b", bundle_id="b", checkpoint=writer_b)
    writer_b.release()

    spliced = tmp_path / "spliced.eck"
    first = _journal_path(tmp_path / "a").read_bytes()
    second = bytearray(_journal_path(tmp_path / "b").read_bytes())
    second[1:5] = (2).to_bytes(4, "big")
    # Re-sign the transplanted header so only the "one header" rule can reject it.
    import zlib

    length = int.from_bytes(second[5:9], "big")
    second[9 + length : 9 + length + 4] = (
        zlib.crc32(bytes(second[9 : 9 + length])) & 0xFFFFFFFF
    ).to_bytes(4, "big")
    spliced.write_bytes(first + bytes(second))

    replay = JournalReader(spliced).read()
    assert replay.header.session_id == "a"
    assert all(not isinstance(entry, JournalOpen) for entry in replay.entries)
    assert replay.stop_reason == "unreadable_body"
    assert replay.torn_tail_bytes == len(second)
