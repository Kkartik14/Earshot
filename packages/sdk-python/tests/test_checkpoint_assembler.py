from __future__ import annotations

import hashlib
import secrets
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest

from earshot.analysis import analyze_incident
from earshot.checkpoint import CheckpointConfig, CheckpointWriter, assemble_incident
from earshot.checkpoint.assembler import AssemblyError
from earshot.checkpoint.reader import JournalReader
from earshot.codec import analysis_input_sha256, encode_incident_protobuf
from earshot.contract import (
    Adapter,
    ClockDomain,
    ClockRelation,
    Evidence,
    MediaLocator,
    MediaRef,
    QualityMeasurement,
    QualitySample,
    TimePoint,
    TimeRange,
)
from earshot.privacy import CaptureClass, CapturePolicy
from earshot.recorder import IncidentRecorder, RecorderConfig
from earshot.validation import assert_valid_incident, validate_incident

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
SDK_SRC = ROOT / "packages" / "sdk-python" / "src"


def _writer(directory: Path, **kwargs) -> CheckpointWriter:
    kwargs.setdefault("keep_finalized", True)
    return CheckpointWriter(CheckpointConfig(checkpoint_dir=directory, **kwargs))


def _journal_path(directory: Path) -> Path:
    return next(directory.glob("*.eck"))


def _point(recorder: IncidentRecorder, milliseconds: int) -> TimePoint:
    return TimePoint(
        source_time_unix_nano=str(1_800_000_000_000_000_000 + milliseconds * 1_000_000),
        monotonic_time_nano=str(milliseconds * 1_000_000),
        clock_domain_id=recorder.clock_domain_id,
    )


# ------------------------------------------------------- scripted sessions
#
# Each script exercises a different admission path, because the byte-identity
# guarantee is only worth what the least-covered path is worth.


def _script_minimal(recorder: IncidentRecorder) -> None:
    recorder.add_participant("caller", role="caller")


def _script_turns(recorder: IncidentRecorder) -> None:
    recorder.add_participant("caller", role="caller")
    recorder.add_participant("agent", role="agent")
    recorder.add_stream("inbound", participant_id="caller", direction="inbound")
    for index in range(4):
        recorder.record_event("earshot.turn.start", turn_id=f"turn-{index}")
        recorder.record_operation(
            operation_id=f"stt-{index}",
            operation_name="stt",
            status="ok",
            started_at=_point(recorder, index * 100),
            ended_at=_point(recorder, index * 100 + 40),
            turn_id=f"turn-{index}",
        )


def _script_manual_operations(recorder: IncidentRecorder) -> None:
    recorder.add_participant("caller", role="caller")
    with recorder.operation("llm", turn_id="turn-0"):
        pass
    with pytest.raises(RuntimeError), recorder.operation("tts", turn_id="turn-0"):
        raise RuntimeError("provider refused")


def _script_coverage_supersession(recorder: IncidentRecorder) -> None:
    recorder.record_coverage("transport.webrtc", "unavailable", "not_observed")
    recorder.record_coverage("transport.webrtc", "available")
    recorder.record_coverage("provider.metrics", "partial", "sampled")


def _script_omissions(recorder: IncidentRecorder) -> None:
    recorder.add_participant(
        "caller",
        role="caller",
        attributes={"gen_ai.input.messages": "dropped by policy"},
    )
    recorder.record_omission("provider.raw_payload", capture_class=CaptureClass.MODEL_PAYLOAD)
    recorder.record_event(
        "earshot.stt.final",
        attributes={"earshot.transcript.text": "dropped", "gen_ai.request.model": "kept"},
    )


def _script_clock_domains(recorder: IncidentRecorder) -> None:
    recorder.register_clock_domain(
        ClockDomain(clock_domain_id="browser-1", kind="browser_monotonic", observer="client")
    )
    recorder.register_clock_relation(
        ClockRelation(
            relation_id="rel-1",
            from_clock_domain_id="browser-1",
            to_clock_domain_id=recorder.clock_domain_id,
            offset_nano="1200",
            method="handshake_sample",
        )
    )
    recorder.record_event("earshot.render.start", turn_id="turn-0")


def _script_quality_and_media(recorder: IncidentRecorder) -> None:
    recorder.add_participant("caller", role="caller")
    recorder.add_stream("inbound", participant_id="caller", direction="inbound")
    recorder.record_quality_sample(
        QualitySample(
            sample_id="sample-1",
            session_id=recorder.session_id,
            quality_kind="transport",
            sample_window=TimeRange(start=_point(recorder, 0), end=_point(recorder, 1000)),
            measurements=(
                QualityMeasurement(name="jitter", value=12.5, unit="ms"),
                QualityMeasurement(name="packets_lost", value=3, unit="1"),
            ),
            evidence=Evidence(
                source="webrtc_stats",
                observer="client",
                method="RTCPeerConnection.getStats",
                confidence="measured",
                availability="available",
            ),
            stream_id="inbound",
        )
    )
    recorder.add_media_ref(
        MediaRef(
            media_id="media-1",
            session_id=recorder.session_id,
            stream_id="inbound",
            media_kind="audio",
            content_type="audio/wav",
            sha256="a" * 64,
            size_bytes=1024,
            locator=MediaLocator(uri="https://media.example.com/a.wav"),
        )
    )


def _script_raw_otlp(recorder: IncidentRecorder) -> None:
    recorder.add_participant("caller", role="caller")
    recorder.add_raw_otlp_chunk(chunk_id="chunk-1", signal="traces", payload=b"\x00\x01\x02exact")
    recorder.add_raw_otlp_chunk(chunk_id="chunk-2", signal="logs", payload=secrets.token_bytes(512))


def _script_truncated(recorder: IncidentRecorder) -> None:
    recorder.add_participant("caller", role="caller")
    for index in range(20):
        recorder.record_event("earshot.turn.start", turn_id=f"turn-{index}")


SCRIPTS: dict[str, tuple[Callable[[IncidentRecorder], None], dict]] = {
    "minimal": (_script_minimal, {}),
    "turns": (_script_turns, {}),
    "manual_operations": (_script_manual_operations, {}),
    "coverage_supersession": (_script_coverage_supersession, {}),
    "omissions": (_script_omissions, {}),
    "clock_domains": (_script_clock_domains, {}),
    "quality_and_media": (
        _script_quality_and_media,
        {
            "capture_policy": CapturePolicy(
                enabled=frozenset({CaptureClass.METADATA, CaptureClass.AUDIO})
            )
        },
    ),
    "raw_otlp": (
        _script_raw_otlp,
        {
            "capture_policy": CapturePolicy(
                enabled=frozenset({CaptureClass.METADATA, CaptureClass.RAW_OTLP})
            )
        },
    ),
    "truncated": (_script_truncated, {"max_records": 6}),
    "adapters": (
        _script_minimal,
        {"adapters": (Adapter(name="pipecat", version="1", framework="pipecat"),)},
    ),
    "encrypted": (_script_turns, {}),
}


def _run(name: str, directory: Path, *, key: bytes | None = None) -> tuple[bytes, Path]:
    script, config_kwargs = SCRIPTS[name]
    writer = _writer(directory, checkpoint_key=key)
    recorder = IncidentRecorder(
        session_id=f"session-{name}",
        bundle_id=f"bundle-{name}",
        config=RecorderConfig(**config_kwargs),
        checkpoint=writer,
    )
    script(recorder)
    bundle = recorder.close()
    return encode_incident_protobuf(bundle), _journal_path(directory)


@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_replaying_a_finalized_journal_reproduces_the_closed_bytes(
    name: str, tmp_path: Path
) -> None:
    """One construction path means a recovered artifact cannot drift from a closed one."""

    key = secrets.token_bytes(32) if name == "encrypted" else None
    closed, journal = _run(name, tmp_path, key=key)

    result = assemble_incident(journal, key=key)

    recovered = encode_incident_protobuf(result.bundle)
    assert hashlib.sha256(recovered).hexdigest() == hashlib.sha256(closed).hexdigest()
    assert recovered == closed
    assert result.report.close_observed is True
    assert result.report.counter_mismatch is False
    # A finalized replay declares nothing, because nothing about the evidence
    # changed: re-ingesting it must deduplicate rather than conflict.
    assert result.bundle.profile.manifest.recovery is None
    assert result.bundle.profile.manifest.finality == "final"


def test_replaying_the_same_journal_twice_is_bit_for_bit_stable(tmp_path: Path) -> None:
    _, journal = _run("turns", tmp_path)

    first = encode_incident_protobuf(assemble_incident(journal).bundle)
    second = encode_incident_protobuf(assemble_incident(journal).bundle)

    assert first == second


# ------------------------------------------------------------ crash recovery


def _crash_after(directory: Path, script: str) -> Path:
    program = textwrap.dedent(
        f"""
        import os, signal, sys
        sys.path.insert(0, {str(SDK_SRC)!r})
        from earshot.checkpoint import CheckpointConfig, CheckpointWriter
        from earshot.recorder import IncidentRecorder

        writer = CheckpointWriter(CheckpointConfig(checkpoint_dir={str(directory)!r}))
        recorder = IncidentRecorder(
            session_id="crashed", bundle_id="crashed", checkpoint=writer
        )
        {script}
        os.kill(os.getpid(), signal.SIGKILL)
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, timeout=60, check=False
    )
    assert completed.returncode == -9, completed.stderr.decode()
    return _journal_path(directory)


def test_a_sigkilled_session_recovers_as_provisional_and_cannot_claim_a_clean_close(
    tmp_path: Path,
) -> None:
    journal = _crash_after(
        tmp_path,
        textwrap.dedent(
            """
            recorder.add_participant("caller", role="caller")
            for index in range(12):
                recorder.record_event("earshot.turn.start", turn_id=f"turn-{index}")
            """
        ).replace("\n", "\n        "),
    )

    result = assemble_incident(journal)
    bundle = result.bundle
    manifest = bundle.profile.manifest

    assert manifest.finality == "provisional"
    assert manifest.completeness == "incomplete"
    assert bundle.profile.session.status == "interrupted"
    # The last checkpoint is not the end of the session, so there is no end.
    assert bundle.profile.session.ended_at is None
    assert manifest.recovery is not None
    assert manifest.recovery.close_observed is False
    assert manifest.recovery.reason == "process_terminated_before_close"
    assert manifest.recovery.method == "checkpoint_journal"
    assert manifest.recovery.last_observation is not None
    coverage = {item.signal: (item.availability, item.reason) for item in bundle.profile.coverage}
    assert coverage["recorder.session_close"] == (
        "unavailable",
        "process_terminated_before_close",
    )
    assert coverage["recorder.checkpoint_journal"] == ("available", None)
    assert len(bundle.profile.events) == 12
    assert_valid_incident(bundle)


def test_an_operation_that_never_finished_recovers_as_open_and_unknown(tmp_path: Path) -> None:
    journal = _crash_after(
        tmp_path,
        textwrap.dedent(
            """
            recorder.add_participant("caller", role="caller")
            entered = recorder.operation("llm", turn_id="turn-0")
            entered.__enter__()
            """
        ).replace("\n", "\n        "),
    )

    bundle = assemble_incident(journal).bundle

    (operation,) = bundle.profile.operations
    assert operation.operation_name == "llm"
    assert operation.status == "unknown"
    assert operation.ended_at is None
    assert operation.evidence is not None
    assert operation.evidence.method == "checkpoint_journal"
    assert operation.evidence.availability == "partial"
    coverage = {item.signal: (item.availability, item.reason) for item in bundle.profile.coverage}
    assert coverage["recorder.operation_completion"] == (
        "partial",
        "process_terminated_mid_operation",
    )
    assert_valid_incident(bundle)


def test_the_analyzer_reports_an_unfinished_operation_as_unavailable_not_zero(
    tmp_path: Path,
) -> None:
    journal = _crash_after(
        tmp_path,
        textwrap.dedent(
            """
            recorder.record_event("earshot.turn.start", turn_id="turn-0")
            entered = recorder.operation("tool", turn_id="turn-0")
            entered.__enter__()
            """
        ).replace("\n", "\n        "),
    )
    bundle = assemble_incident(journal).bundle

    analysis = analyze_incident(
        bundle, input_sha256=analysis_input_sha256(bundle), generated_at_unix_nano=0
    )

    (turn,) = analysis.projections.turns
    tools = turn.metrics.tools
    assert tools.operation_count == 1
    assert tools.timed_operation_count == 0
    assert tools.untimed_operation_count == 1
    assert tools.total_work_completeness == "unavailable"
    assert tools.total_work_ms == 0.0
    assert tools.limitation is not None


# ---------------------------------------------------------------- torn tail


def test_a_torn_tail_becomes_a_declared_limitation_rather_than_silence(tmp_path: Path) -> None:
    journal = _crash_after(
        tmp_path,
        textwrap.dedent(
            """
            recorder.add_participant("caller", role="caller")
            for index in range(8):
                recorder.record_event("earshot.turn.start", turn_id=f"turn-{index}")
            """
        ).replace("\n", "\n        "),
    )
    intact = JournalReader(journal).read()
    data = journal.read_bytes()
    torn = journal.with_suffix(".torn.eck")
    torn.write_bytes(data[:-9])

    result = assemble_incident(torn)
    bundle = result.bundle

    assert result.report.torn_tail_bytes > 0
    assert bundle.profile.manifest.recovery is not None
    assert bundle.profile.manifest.recovery.torn_tail_bytes == result.report.torn_tail_bytes
    assert bundle.profile.manifest.recovery.journal_complete is False
    reasons = {omission.reason for omission in bundle.profile.privacy.omissions}
    assert "checkpoint_torn_tail" in reasons
    coverage = {item.signal: (item.availability, item.reason) for item in bundle.profile.coverage}
    assert coverage["recorder.checkpoint_journal"] == ("partial", "torn_tail")
    # The partial record was never admitted, so it is simply absent.
    assert (
        len(bundle.profile.events)
        == len([entry for entry in intact.entries if getattr(entry, "kind", None) == "event"]) - 1
    )
    assert_valid_incident(bundle)


def test_every_truncation_of_a_real_journal_assembles_or_reports_no_header(
    tmp_path: Path,
) -> None:
    _, journal = _run("turns", tmp_path)
    data = journal.read_bytes()
    from earshot.checkpoint.reader import JournalUnreadableError

    for cut in range(0, len(data), 37):
        candidate = tmp_path / "cut.eck"
        candidate.write_bytes(data[:cut])
        try:
            result = assemble_incident(candidate)
        except JournalUnreadableError:
            continue
        assert_valid_incident(result.bundle)
        assert result.report.torn_tail_bytes == cut - _consumed(data, cut)


def _consumed(data: bytes, cut: int) -> int:
    from earshot.checkpoint.framing import scan_frames

    return scan_frames(data[:cut], max_body_bytes=32 * 1024 * 1024).consumed_bytes


# ---------------------------------------------------------- journal capacity


def test_a_journal_that_hit_its_cap_declares_the_loss(tmp_path: Path) -> None:
    writer = _writer(tmp_path, max_journal_bytes=1400)
    recorder = IncidentRecorder(session_id="s", bundle_id="b", checkpoint=writer)
    recorder.add_participant("caller", role="caller")
    for index in range(40):
        recorder.record_event("earshot.turn.start", turn_id=f"turn-{index}")

    bundle = assemble_incident(_journal_path(tmp_path)).bundle

    assert bundle.profile.manifest.recovery is not None
    assert bundle.profile.manifest.recovery.journal_complete is False
    reasons = {omission.reason for omission in bundle.profile.privacy.omissions}
    assert "checkpoint_journal_full" in reasons
    coverage = {item.signal: (item.availability, item.reason) for item in bundle.profile.coverage}
    assert coverage["recorder.checkpoint_journal"] == ("partial", "journal_full")
    assert_valid_incident(bundle)
    writer.release()


# ------------------------------------------------------------ cross-checking


def test_a_journal_whose_totals_disagree_with_its_records_fails_loudly(tmp_path: Path) -> None:
    _, journal = _run("truncated", tmp_path)
    data = bytearray(journal.read_bytes())
    marker = b'"truncated_records":'
    index = data.rindex(marker) + len(marker)
    digits = len(data) - index - len(data[index:].lstrip(b"0123456789"))
    assert digits > 0
    data[index : index + digits] = b"9" * digits
    tampered = tmp_path / "tampered.eck"
    _rewrite_last_frame(data, tampered)

    with pytest.raises(AssemblyError):
        assemble_incident(tampered)

    result = assemble_incident(tampered, best_effort=True)
    assert result.report.counter_mismatch is True
    # The replayed values stay authoritative; the flag says the two disagreed.
    assert result.bundle.profile.manifest.finality == "final"


def _rewrite_last_frame(data: bytearray, destination: Path) -> None:
    """Re-checksum the final frame after editing its body in place."""

    import zlib

    from earshot.checkpoint.framing import CHECKSUM_SIZE, HEADER_SIZE

    offset = 0
    frames: list[int] = []
    while offset < len(data):
        frames.append(offset)
        length = int.from_bytes(data[offset + 5 : offset + 9], "big")
        offset += HEADER_SIZE + length + CHECKSUM_SIZE
    start = frames[-1]
    length = int.from_bytes(data[start + 5 : start + 9], "big")
    body = bytes(data[start + HEADER_SIZE : start + HEADER_SIZE + length])
    checksum = (zlib.crc32(body) & 0xFFFFFFFF).to_bytes(4, "big")
    data[start + HEADER_SIZE + length : start + HEADER_SIZE + length + CHECKSUM_SIZE] = checksum
    destination.write_bytes(bytes(data))


# ---------------------------------------------------------------- ingestion


def test_a_recovered_bundle_ingests_and_lists_as_provisional(tmp_path: Path) -> None:
    from earshot.storage import IncidentStore

    journal = _crash_after(
        tmp_path / "journals",
        '\n        recorder.add_participant("caller", role="caller")',
    )
    result = assemble_incident(journal)
    payload = encode_incident_protobuf(result.bundle)
    store = IncidentStore(tmp_path / "data")
    try:
        first = store.ingest(result.bundle, payload)
        # Determinism makes a re-recovery an idempotent duplicate, not a conflict.
        again = assemble_incident(journal)
        second = store.ingest(again.bundle, encode_incident_protobuf(again.bundle))
        assert first.created is True
        assert second.created is False
        page = store.list_incidents(destination="local_cli")
        assert [item.finality for item in page.items] == ["provisional"]
    finally:
        store.close()


def test_a_suffixed_bundle_id_produces_a_distinct_artifact(tmp_path: Path) -> None:
    journal = _crash_after(tmp_path, '\n        recorder.add_participant("caller", role="caller")')

    plain = assemble_incident(journal)
    suffixed = assemble_incident(journal, bundle_id_suffix=".r2")

    assert suffixed.bundle.profile.manifest.bundle_id == "crashed.r2"
    assert validate_incident(suffixed.bundle).ok
    assert encode_incident_protobuf(plain.bundle) != encode_incident_protobuf(suffixed.bundle)
