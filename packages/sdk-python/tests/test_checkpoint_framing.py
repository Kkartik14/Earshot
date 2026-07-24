from __future__ import annotations

import pytest

from earshot.checkpoint.framing import (
    CHECKSUM_SIZE,
    HEADER_SIZE,
    SEPARATOR,
    FrameTooLargeError,
    encode_frame,
    scan_frames,
)
from earshot.checkpoint.records import (
    JournalLimitEntry,
    JournalOpen,
    decode_entry,
    encode_entry,
)

pytestmark = pytest.mark.unit

MAX_BODY = 1024


def _journal(count: int = 8) -> tuple[bytes, list[bytes]]:
    bodies = [f'{{"record":{index}}}'.encode() for index in range(1, count + 1)]
    return b"".join(
        encode_frame(index, body, max_body_bytes=MAX_BODY)
        for index, body in enumerate(bodies, start=1)
    ), bodies


def test_frames_round_trip_in_order() -> None:
    data, bodies = _journal()
    scan = scan_frames(data, max_body_bytes=MAX_BODY)

    assert [frame.body for frame in scan.frames] == bodies
    assert [frame.sequence for frame in scan.frames] == list(range(1, len(bodies) + 1))
    assert scan.torn_tail_bytes == 0
    assert scan.stop_reason is None


def test_every_truncation_offset_recovers_the_maximal_intact_prefix() -> None:
    """A journal cut at any byte keeps every whole frame before the cut."""

    data, bodies = _journal()
    boundaries = []
    offset = 0
    for body in bodies:
        offset += HEADER_SIZE + len(body) + CHECKSUM_SIZE
        boundaries.append(offset)

    for cut in range(len(data) + 1):
        scan = scan_frames(data[:cut], max_body_bytes=MAX_BODY)
        expected = sum(1 for boundary in boundaries if boundary <= cut)
        assert len(scan.frames) == expected, cut
        assert [frame.body for frame in scan.frames] == bodies[:expected], cut
        consumed = boundaries[expected - 1] if expected else 0
        assert scan.consumed_bytes == consumed, cut
        assert scan.torn_tail_bytes == cut - consumed, cut


def test_a_single_bit_flip_stops_the_scan_at_the_previous_good_frame() -> None:
    data, bodies = _journal(4)
    second_body_offset = HEADER_SIZE + len(bodies[0]) + CHECKSUM_SIZE + HEADER_SIZE

    for bit in range(8):
        corrupt = bytearray(data)
        corrupt[second_body_offset] ^= 1 << bit
        scan = scan_frames(bytes(corrupt), max_body_bytes=MAX_BODY)
        assert [frame.body for frame in scan.frames] == bodies[:1]
        assert scan.stop_reason == "bad_checksum"
        # The reader never resynchronizes past damage: a record's own bytes can
        # contain the separator, so hunting for the next one could admit a frame
        # forged out of the middle of a real record.
        assert scan.torn_tail_bytes == len(data) - scan.consumed_bytes


def test_a_wrong_separator_stops_the_scan_without_raising() -> None:
    data, bodies = _journal(3)
    corrupt = bytearray(data)
    corrupt[HEADER_SIZE + len(bodies[0]) + CHECKSUM_SIZE] = SEPARATOR ^ 0xFF

    scan = scan_frames(bytes(corrupt), max_body_bytes=MAX_BODY)

    assert len(scan.frames) == 1
    assert scan.stop_reason == "bad_separator"


def test_a_non_consecutive_sequence_stops_the_scan() -> None:
    data = encode_frame(1, b"one", max_body_bytes=MAX_BODY) + encode_frame(
        3, b"three", max_body_bytes=MAX_BODY
    )

    scan = scan_frames(data, max_body_bytes=MAX_BODY)

    assert [frame.body for frame in scan.frames] == [b"one"]
    assert scan.stop_reason == "sequence_gap"


def test_a_length_beyond_the_cap_is_damage_rather_than_a_read_instruction() -> None:
    data = bytearray(encode_frame(1, b"x" * 64, max_body_bytes=MAX_BODY))
    data[5:9] = (MAX_BODY + 1).to_bytes(4, "big")

    scan = scan_frames(bytes(data), max_body_bytes=MAX_BODY)

    assert scan.frames == ()
    assert scan.stop_reason == "frame_too_large"
    assert scan.torn_tail_bytes == len(data)


def test_a_rejected_body_stops_the_scan_and_reports_the_remaining_bytes() -> None:
    data, bodies = _journal(4)

    scan = scan_frames(
        data,
        max_body_bytes=MAX_BODY,
        open_body=lambda sequence, body: None if sequence == 3 else body,
    )

    assert [frame.body for frame in scan.frames] == bodies[:2]
    assert scan.stop_reason == "unreadable_body"
    assert scan.consumed_bytes + scan.torn_tail_bytes == len(data)


def test_encoding_refuses_a_body_beyond_the_cap() -> None:
    with pytest.raises(FrameTooLargeError):
        encode_frame(1, b"x" * (MAX_BODY + 1), max_body_bytes=MAX_BODY)


def test_scanning_arbitrary_bytes_never_raises() -> None:
    for data in (b"", b"\x00", b"\x1e", bytes(range(256)), b"\x1e" * 4096):
        scan = scan_frames(data, max_body_bytes=MAX_BODY)
        assert scan.consumed_bytes + scan.torn_tail_bytes == len(data)


def test_journal_entries_round_trip_through_their_envelope() -> None:
    entry = JournalLimitEntry(
        reason="max_records",
        kind="event",
        capture_class="metadata",
        estimated_bytes=17,
        whole_record=True,
        freeze=True,
    )

    assert decode_entry(encode_entry(entry)) == entry


def test_an_unsupported_entry_kind_is_a_format_error_not_a_crash() -> None:
    from earshot.checkpoint.records import JournalFormatError

    for body in (b"not json", b"[]", b'{"k":"nope","v":{}}', b'{"k":"limit","v":{}}'):
        with pytest.raises(JournalFormatError):
            decode_entry(body)


def test_an_entry_cannot_be_both_a_ledger_append_and_a_record() -> None:
    from earshot.checkpoint.records import JournalRecordEntry

    with pytest.raises(ValueError, match="ledger-only"):
        JournalRecordEntry(kind="omission", value={"anything": 1})
    with pytest.raises(ValueError, match="requires its contract record"):
        JournalRecordEntry(kind="event")
    with pytest.raises(ValueError, match="supersedes"):
        JournalRecordEntry(kind="event", value={}, replaces_index=0)


def test_a_journal_header_rebuilds_the_exact_capture_policy() -> None:
    from earshot.checkpoint.records import governance_to_journal
    from earshot.privacy import CaptureClass, CapturePolicy, ConsentConfig
    from earshot.privacy import CaptureGovernance as Governance

    policy = CapturePolicy(
        enabled=frozenset({CaptureClass.METADATA, CaptureClass.TRANSCRIPT}),
        policy_id="team.policy",
        policy_version="7",
        governance={
            CaptureClass.TRANSCRIPT: Governance(consent=ConsentConfig(status="granted")),
        },
    )
    header = JournalOpen(
        journal_format_version=1,
        journal_id="j",
        producer_name="earshot",
        producer_version="0.1.0",
        bundle_id="b",
        session_id="s",
        clock_domain_id="c",
        started_wall="1",
        started_mono="2",
        manual_trace_id="a" * 32,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        enabled_classes=("metadata", "transcript"),
        governance={
            "transcript": governance_to_journal(policy.governance[CaptureClass.TRANSCRIPT])
        },
        max_records=1,
        max_capture_bytes=1,
        max_raw_otlp_bytes=1,
        max_value_bytes=1,
    )

    rebuilt = decode_entry(encode_entry(header))
    assert isinstance(rebuilt, JournalOpen)
    assert rebuilt.capture_policy() == policy
