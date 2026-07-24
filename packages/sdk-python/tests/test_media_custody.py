"""Media custody: references and sync metadata for media somebody else holds.

Earshot is metadata-only by discipline. It never stores, ingests, proxies,
caches, or re-serves media, so these tests are as much about what *cannot*
happen as about what validates: an integrity claim nobody can back is refused,
a media timeline is aligned by an ordinary ``ClockRelation`` or not at all, and
no byte of media reaches an incident, a journal, or the network.
"""

from __future__ import annotations

import hashlib
import json
import socket
import urllib.request
from pathlib import Path

import pytest
from pydantic import BaseModel

from earshot import contract as contract_module
from earshot.checkpoint import CheckpointConfig, CheckpointWriter, assemble_incident
from earshot.codec import (
    decode_incident_json,
    decode_incident_protobuf,
    encode_incident_json,
    encode_incident_protobuf,
)
from earshot.contract import (
    ByteRange,
    CaptureClassPolicy,
    ClockDomain,
    ClockRelation,
    ConsentRecord,
    IncidentBundle,
    MediaLocator,
    MediaRef,
    RetentionPolicy,
    media_custody_incoherence,
)
from earshot.privacy import CaptureClass, CapturePolicy
from earshot.recorder import IncidentRecorder, RecorderConfig
from earshot.validation import validate_incident
from incident_factory import SECRET_SENTINEL, point

pytestmark = pytest.mark.unit

# Stand-in for real media bytes. Nothing in earshot may ever be handed these,
# so the sentinel exists purely to be searched for in everything earshot writes.
MEDIA_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt EARSHOT_MEDIA_BYTES_SENTINEL"
MEDIA_DIGEST = hashlib.sha256(MEDIA_BYTES).hexdigest()
AUDIO_POLICY = CapturePolicy(enabled=frozenset({CaptureClass.METADATA, CaptureClass.AUDIO}))


def media_recorder(checkpoint_dir: Path | None = None) -> IncidentRecorder:
    """A recorder allowed to retain media custody, optionally journaling."""

    writer = (
        None
        if checkpoint_dir is None
        else CheckpointWriter(CheckpointConfig(checkpoint_dir=checkpoint_dir, keep_finalized=True))
    )
    recorder = IncidentRecorder(
        session_id="session-media",
        config=RecorderConfig(capture_policy=AUDIO_POLICY),
        checkpoint=writer,
    )
    recorder.add_participant("participant-agent", role="agent")
    recorder.add_stream("stream-output", participant_id="participant-agent", direction="outbound")
    return recorder


def replace_profile(bundle: IncidentBundle, **updates: object) -> IncidentBundle:
    return bundle.model_copy(update={"profile": bundle.profile.model_copy(update=updates)})


def codes(bundle: IncidentBundle) -> set[str]:
    return {issue.code for issue in validate_incident(bundle).errors}


def warning_codes(bundle: IncidentBundle) -> set[str]:
    return {issue.code for issue in validate_incident(bundle).warnings}


def with_audio_capture(bundle: IncidentBundle) -> IncidentBundle:
    """Grant the audio capture class; a media reference always requires it."""

    allowed = CaptureClassPolicy(capture_class="audio", decision="allow", captured=True)
    privacy = bundle.profile.privacy.model_copy(
        update={
            "capture_classes": tuple(
                allowed if policy.capture_class == "audio" else policy
                for policy in bundle.profile.privacy.capture_classes
            )
        }
    )
    return replace_profile(bundle, privacy=privacy)


def digest_ref(**overrides: object) -> MediaRef:
    """A reference whose holder measured the bytes and declared what it found."""

    fields: dict[str, object] = {
        "media_id": "media-1",
        "session_id": "session-1",
        "stream_id": "stream-output",
        "media_kind": "audio",
        "content_type": "audio/wav",
        "integrity": "content_digest",
        "sha256": MEDIA_DIGEST,
        "size_bytes": len(MEDIA_BYTES),
    }
    fields.update(overrides)
    return MediaRef(**fields)  # type: ignore[arg-type]


def opaque_ref(**overrides: object) -> MediaRef:
    """A reference to bytes nobody on this path measured."""

    fields: dict[str, object] = {
        "media_id": "media-1",
        "session_id": "session-1",
        "stream_id": "stream-output",
        "media_kind": "audio",
        "content_type": "audio/wav",
        "integrity": "opaque_handle",
        "custodian": "provider.vapi",
    }
    fields.update(overrides)
    return MediaRef(**fields)  # type: ignore[arg-type]


def media_clock(bundle: IncidentBundle, *, relation: ClockRelation | None) -> IncidentBundle:
    """Declare the media's own timeline, optionally calibrated to the session's."""

    domain = ClockDomain(
        clock_domain_id="media-1",
        kind="media_timeline",
        observer="provider.vapi",
        monotonic_origin_nano="0",
    )
    return replace_profile(
        bundle,
        clock_domains=(*bundle.profile.clock_domains, domain),
        clock_relations=(
            bundle.profile.clock_relations
            if relation is None
            else (*bundle.profile.clock_relations, relation)
        ),
    )


def session_relation(**overrides: object) -> ClockRelation:
    fields: dict[str, object] = {
        "relation_id": "relation-media",
        "from_clock_domain_id": "media-1",
        "to_clock_domain_id": "server-clock",
        "offset_nano": "1800000000000000000",
        "uncertainty_nano": "12000000",
        "method": "provider_declared",
    }
    fields.update(overrides)
    return ClockRelation(**fields)  # type: ignore[arg-type]


# ---------------------------------------------------------------- integrity


def test_both_integrity_modes_are_valid_custody(valid_bundle) -> None:
    granted = with_audio_capture(valid_bundle)

    assert validate_incident(replace_profile(granted, media_refs=(digest_ref(),))).ok
    assert validate_incident(replace_profile(granted, media_refs=(opaque_ref(),))).ok


@pytest.mark.parametrize(
    ("ref", "why"),
    [
        (digest_ref(sha256=None), "a digest claim with no digest"),
        (digest_ref(size_bytes=None), "a digest claim with no measured size"),
        (opaque_ref(sha256=MEDIA_DIGEST), "an unmeasured handle asserting a digest"),
        (opaque_ref(size_bytes=7), "an unmeasured handle asserting a size"),
        (opaque_ref(custodian=None), "an unattestable handle naming no holder"),
        (
            opaque_ref(byte_range=ByteRange(offset=0, length=8)),
            "a range into bytes whose length was never observed",
        ),
    ],
)
def test_an_integrity_claim_nobody_can_back_is_refused(valid_bundle, ref, why) -> None:
    granted = with_audio_capture(valid_bundle)

    assert "EARSHOT_MEDIA_CUSTODY_INCOHERENT" in codes(
        replace_profile(granted, media_refs=(ref,))
    ), why


def test_an_opaque_handle_cannot_produce_a_digest_claim(valid_bundle) -> None:
    """The whole point of the discriminator: no digest, no implied verification.

    ``opaque_handle`` means earshot never read the bytes, so there is no path —
    default, coercion, encoder normalization, or validation repair — by which the
    artifact acquires a digest for them.
    """

    granted = with_audio_capture(valid_bundle)
    bundle = replace_profile(granted, media_refs=(opaque_ref(),))
    assert validate_incident(bundle).ok

    encoded = encode_incident_json(bundle)
    round_tripped = decode_incident_json(encoded)
    media = round_tripped.profile.media_refs[0]

    assert media.integrity == "opaque_handle"
    assert media.sha256 is None
    assert media.size_bytes is None
    # Not merely absent from the model: absent from the bytes, so no consumer can
    # read a digest — or a size implying one — out of the artifact at all.
    assert MEDIA_DIGEST.encode() not in encoded
    serialized = json.loads(encoded)["profile"]["media_refs"][0]
    assert "sha256" not in serialized
    assert "size_bytes" not in serialized


def test_a_digest_reference_carries_a_declaration_earshot_did_not_compute(
    valid_bundle,
) -> None:
    """A digest is the holder's commitment, and earshot must not recompute it.

    Recomputation would require reading the bytes, which is the one thing earshot
    refuses to do, so the digest travels as declared even when it is wrong.
    """

    granted = with_audio_capture(valid_bundle)
    declared = "b" * 64
    assert declared != MEDIA_DIGEST
    bundle = replace_profile(granted, media_refs=(digest_ref(sha256=declared),))

    assert validate_incident(bundle).ok
    assert (
        decode_incident_json(encode_incident_json(bundle)).profile.media_refs[0].sha256 == declared
    )


def test_a_byte_range_is_still_bounded_by_a_measured_size(valid_bundle) -> None:
    granted = with_audio_capture(valid_bundle)
    ref = digest_ref(size_bytes=10, byte_range=ByteRange(offset=9, length=2))

    assert "EARSHOT_MEDIA_RANGE_OUT_OF_BOUNDS" in codes(replace_profile(granted, media_refs=(ref,)))


def test_the_shared_incoherence_rule_is_the_one_the_recorder_enforces() -> None:
    """One rule, two enforcement points; a second copy could disagree."""

    assert media_custody_incoherence(digest_ref()) is None
    assert media_custody_incoherence(opaque_ref()) is None
    assert media_custody_incoherence(digest_ref(sha256=None)) is not None
    assert media_custody_incoherence(opaque_ref(custodian=None)) is not None


# ------------------------------------------------------------ clock alignment


def test_media_aligned_by_a_declared_clock_relation_is_not_warned_about(
    valid_bundle,
) -> None:
    """Alignment is the ordinary cross-clock question, answered by ClockRelation."""

    granted = media_clock(with_audio_capture(valid_bundle), relation=session_relation())
    bundle = replace_profile(granted, media_refs=(opaque_ref(clock_domain_id="media-1"),))

    report = validate_incident(bundle)
    assert report.ok
    assert "EARSHOT_MEDIA_UNALIGNED" not in {issue.code for issue in report.issues}


def test_a_reversed_relation_still_aligns_because_a_calibration_is_invertible(
    valid_bundle,
) -> None:
    granted = media_clock(
        with_audio_capture(valid_bundle),
        relation=session_relation(
            from_clock_domain_id="server-clock",
            to_clock_domain_id="media-1",
            offset_nano="-1800000000000000000",
        ),
    )
    bundle = replace_profile(granted, media_refs=(opaque_ref(clock_domain_id="media-1"),))

    assert "EARSHOT_MEDIA_UNALIGNED" not in warning_codes(bundle)


def test_media_with_no_calibration_stays_honestly_unaligned(valid_bundle) -> None:
    """Unalignable custody is still legitimate custody — a warning, not an error."""

    granted = media_clock(with_audio_capture(valid_bundle), relation=None)
    bundle = replace_profile(granted, media_refs=(opaque_ref(clock_domain_id="media-1"),))

    report = validate_incident(bundle)
    assert report.ok, report
    assert "EARSHOT_MEDIA_UNALIGNED" in {issue.code for issue in report.warnings}


def test_a_relation_to_a_domain_the_session_never_uses_does_not_align(
    valid_bundle,
) -> None:
    """Alignment means reaching *this session's* timeline, not any timeline."""

    granted = media_clock(with_audio_capture(valid_bundle), relation=None)
    stranded = ClockDomain(clock_domain_id="stranded", kind="other", observer="nobody")
    granted = replace_profile(
        granted,
        clock_domains=(*granted.profile.clock_domains, stranded),
        clock_relations=(session_relation(to_clock_domain_id="stranded"),),
    )
    bundle = replace_profile(granted, media_refs=(opaque_ref(clock_domain_id="media-1"),))

    assert "EARSHOT_MEDIA_UNALIGNED" in warning_codes(bundle)


def test_media_in_a_domain_the_session_records_in_needs_no_relation(valid_bundle) -> None:
    granted = with_audio_capture(valid_bundle)
    bundle = replace_profile(granted, media_refs=(opaque_ref(clock_domain_id="server-clock"),))

    assert "EARSHOT_MEDIA_UNALIGNED" not in warning_codes(bundle)


def test_an_undeclared_media_clock_domain_is_refused(valid_bundle) -> None:
    granted = with_audio_capture(valid_bundle)
    bundle = replace_profile(granted, media_refs=(opaque_ref(clock_domain_id="ghost"),))

    assert "EARSHOT_MEDIA_CLOCK_UNKNOWN" in codes(bundle)


def test_custody_declares_no_second_synchronization_model() -> None:
    """The media timeline is a clock domain; there is no parallel sync record.

    A second mechanism would come with its own offset, drift, and uncertainty
    semantics that the analyzer's cross-clock rules do not know about, and media
    would quietly become comparable by a path nothing else is held to.
    """

    field_names = set(MediaRef.model_fields)
    assert "clock_domain_id" in field_names
    assert not {
        name
        for name in field_names
        if any(token in name for token in ("offset", "drift", "sync", "skew"))
    }


# ------------------------------------------------------- no media byte path


def test_no_contract_record_can_carry_media_bytes() -> None:
    """Structural proof that media has no ingestion path into the artifact.

    ``RawOtlpChunk.payload`` is the only ``bytes`` member in the whole contract,
    and it is governed by the ``raw_otlp`` capture class, not by media custody.
    """

    byte_fields = {
        f"{name}.{field}"
        for name, value in vars(contract_module).items()
        if isinstance(value, type) and issubclass(value, BaseModel)
        for field, info in value.model_fields.items()
        if info.annotation is bytes
    }

    assert byte_fields == {"RawOtlpChunk.payload"}
    assert not any(info.annotation is bytes for info in MediaRef.model_fields.values())


def test_no_media_content_reaches_the_incident_or_the_journal(tmp_path: Path) -> None:
    """The sentinel test: custody records carry references, never content."""

    checkpoint_dir = tmp_path / "journal"
    recorder = media_recorder(checkpoint_dir)
    recorder.register_clock_domain(
        ClockDomain(
            clock_domain_id="media-1",
            kind="media_timeline",
            observer="provider.vapi",
            monotonic_origin_nano="0",
        )
    )
    assert recorder.add_media_ref(
        MediaRef(
            media_id="media-1",
            session_id="session-media",
            stream_id="stream-output",
            media_kind="audio",
            content_type="audio/wav",
            integrity="opaque_handle",
            custodian="provider.vapi",
            clock_domain_id="media-1",
            locator=MediaLocator(uri="https://example.invalid/recordings/1.wav"),
        )
    )
    bundle = recorder.close()

    encoded = encode_incident_json(bundle)
    journal_bytes = b"".join(
        path.read_bytes() for path in sorted(checkpoint_dir.rglob("*")) if path.is_file()
    )

    for blob in (encoded, encode_incident_protobuf(bundle), journal_bytes):
        assert MEDIA_BYTES not in blob
        assert b"EARSHOT_MEDIA_BYTES_SENTINEL" not in blob
        # An opaque handle asserts no digest, so not even a fingerprint of the
        # media survives anywhere earshot writes.
        assert MEDIA_DIGEST.encode() not in blob
    # The custody facts themselves are present; only the content is not.
    assert b"provider.vapi" in encoded
    assert b"media-1" in journal_bytes


def test_recovering_a_journal_preserves_custody_without_acquiring_content(
    tmp_path: Path,
) -> None:
    checkpoint_dir = tmp_path / "journal"
    recorder = media_recorder(checkpoint_dir)
    recorder.add_media_ref(
        MediaRef(
            media_id="media-1",
            session_id="session-media",
            stream_id="stream-output",
            media_kind="audio",
            content_type="audio/wav",
            integrity="opaque_handle",
            custodian="customer.s3",
            consent=ConsentRecord(status="granted"),
            retention=RetentionPolicy(ttl_nano="86400000000000"),
        )
    )

    recovered = assemble_incident(next(iter(checkpoint_dir.glob("*.eck"))))
    media = recovered.bundle.profile.media_refs[0]

    assert media.integrity == "opaque_handle"
    assert media.custodian == "customer.s3"
    assert media.sha256 is None
    assert media.consent is not None and media.consent.status == "granted"
    assert media.retention is not None and media.retention.ttl_nano == "86400000000000"


def test_no_custody_path_dereferences_a_locator(valid_bundle, monkeypatch) -> None:
    """Nothing — validation, encoding, or decoding — fetches the media."""

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("earshot must never fetch media")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)

    granted = with_audio_capture(valid_bundle)
    bundle = replace_profile(
        granted,
        media_refs=(opaque_ref(locator=MediaLocator(uri="https://example.invalid/audio.wav")),),
    )

    assert validate_incident(bundle).ok
    assert decode_incident_protobuf(encode_incident_protobuf(bundle)).profile.media_refs


# --------------------------------------------------------- locator hygiene


@pytest.mark.parametrize("integrity", ["content_digest", "opaque_handle"])
def test_a_credential_bearing_locator_never_survives_admission(integrity: str) -> None:
    """Custody must not turn earshot into a holder of somebody else's keys."""

    recorder = media_recorder()
    extra: dict[str, object] = (
        {"sha256": MEDIA_DIGEST, "size_bytes": len(MEDIA_BYTES)}
        if integrity == "content_digest"
        else {"custodian": "provider.vapi"}
    )
    assert recorder.add_media_ref(
        MediaRef(
            media_id="media-1",
            session_id="session-media",
            stream_id="stream-output",
            media_kind="audio",
            content_type="audio/wav",
            integrity=integrity,  # type: ignore[arg-type]
            locator=MediaLocator(uri=f"https://example.invalid/a.wav?token={SECRET_SENTINEL}"),
            **extra,  # type: ignore[arg-type]
        )
    )
    bundle = recorder.close()

    assert bundle.profile.media_refs[0].locator is None
    assert SECRET_SENTINEL.encode() not in encode_incident_json(bundle)
    assert any(
        omission.reason == "credential_bearing_locator"
        for omission in bundle.profile.privacy.omissions
    )


def test_the_recorder_refuses_an_incoherent_custody_claim_at_admission() -> None:
    """Refused where it is authored, not silently carried until close()."""

    recorder = media_recorder()

    with pytest.raises(ValueError, match="opaque_handle"):
        recorder.add_media_ref(
            MediaRef(
                media_id="media-1",
                session_id="session-media",
                stream_id="stream-output",
                media_kind="audio",
                content_type="audio/wav",
                integrity="opaque_handle",
                sha256=MEDIA_DIGEST,
                size_bytes=len(MEDIA_BYTES),
            )
        )


# --------------------------------------------------------------- versioning


def test_the_default_integrity_mode_still_demands_a_digest(valid_bundle) -> None:
    """Relaxing the field must not relax the claim.

    ``sha256``/``size_bytes`` became optional, but ``integrity`` defaults to
    ``content_digest``, so a caller that simply omits them is refused rather than
    silently reclassified as an unverifiable handle.
    """

    granted = with_audio_capture(valid_bundle)
    ref = MediaRef(
        media_id="media-1",
        session_id="session-1",
        stream_id="stream-output",
        media_kind="audio",
        content_type="audio/wav",
    )

    assert ref.integrity == "content_digest"
    assert "EARSHOT_MEDIA_CUSTODY_INCOHERENT" in codes(replace_profile(granted, media_refs=(ref,)))


def test_a_0_1_0_bundle_cannot_declare_media_custody(valid_bundle) -> None:
    """0.1.0's MediaRef was digest-and-size only; claiming it while using custody
    asserts a contract that version cannot express."""

    granted = with_audio_capture(valid_bundle)
    manifest = granted.profile.manifest.model_copy(
        update={"schema_version": "0.1.0", "semantic_profile_version": "0.1.0"}
    )
    older = replace_profile(granted, manifest=manifest)

    assert "EARSHOT_SCHEMA_VERSION_UNSUPPORTED" in codes(
        replace_profile(older, media_refs=(opaque_ref(),))
    )
    # The shape 0.1.0 *could* express keeps validating untouched, so the existing
    # fixture corpus is unaffected by the field becoming optional.
    assert validate_incident(replace_profile(older, media_refs=(digest_ref(),))).ok


def test_custody_survives_both_wire_formats(valid_bundle) -> None:
    granted = media_clock(with_audio_capture(valid_bundle), relation=session_relation())
    bundle = replace_profile(
        granted,
        media_refs=(
            opaque_ref(
                clock_domain_id="media-1",
                custodian="customer.s3",
                consent=ConsentRecord(status="granted", legal_basis="contract"),
                retention=RetentionPolicy(expires_at_unix_nano="1900000000000000000"),
                time_range=contract_module.TimeRange(start=point(0), end=point(2_000_000)),
            ),
        ),
    )

    for encode, decode in (
        (encode_incident_json, decode_incident_json),
        (encode_incident_protobuf, decode_incident_protobuf),
    ):
        media = decode(encode(bundle)).profile.media_refs[0]
        assert media.integrity == "opaque_handle"
        assert media.custodian == "customer.s3"
        assert media.clock_domain_id == "media-1"
        assert media.consent is not None and media.consent.legal_basis == "contract"
        assert media.retention is not None
        assert media.sha256 is None and media.size_bytes is None
