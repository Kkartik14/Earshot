from __future__ import annotations

import json
from pathlib import Path

import pytest

from earshot.codec import decode_incident_json, encode_incident_json
from earshot.contract import (
    Coverage,
    IncidentBundle,
    Producer,
    RecoveryRecord,
    TimePoint,
)
from earshot.validation import IncidentValidationError, assert_valid_incident, validate_incident
from earshot.versions import (
    CONTRACT_VERSION,
    SEMANTIC_PROFILE_VERSION,
    SUPPORTED_CONTRACT_VERSIONS,
    SUPPORTED_SEMANTIC_PROFILE_VERSIONS,
)
from incident_factory import make_valid_bundle

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]


def issue_codes(bundle: IncidentBundle) -> set[str]:
    return {issue.code for issue in validate_incident(bundle).issues}


def _recovery(**overrides) -> RecoveryRecord:
    values = {
        "method": "checkpoint_journal",
        "reason": "process_terminated_before_close",
        "close_observed": False,
        "journal_id": "journal-1",
        "last_sequence": 12,
        "recoverer": Producer(name="earshot", version="0.1.0"),
    }
    values.update(overrides)
    return RecoveryRecord(**values)


def _recovered(**manifest_overrides) -> IncidentBundle:
    """A well-formed provisional bundle, for negative tests to break one way."""

    bundle = make_valid_bundle()
    profile = bundle.profile
    manifest = profile.manifest.model_copy(
        update={
            "finality": "provisional",
            "completeness": "incomplete",
            "recovery": _recovery(),
            **manifest_overrides,
        }
    )
    session = profile.session.model_copy(update={"status": "interrupted", "ended_at": None})
    coverage = (
        *profile.coverage,
        Coverage(
            signal="recorder.session_close",
            availability="unavailable",
            reason="process_terminated_before_close",
        ),
    )
    return bundle.model_copy(
        update={
            "profile": profile.model_copy(
                update={"manifest": manifest, "session": session, "coverage": coverage}
            )
        }
    )


def test_a_well_formed_recovered_bundle_validates() -> None:
    assert_valid_incident(_recovered())


def test_a_non_final_artifact_must_declare_how_it_was_reconstructed() -> None:
    bundle = _recovered()
    manifest = bundle.profile.manifest.model_copy(update={"recovery": None})
    broken = bundle.model_copy(
        update={"profile": bundle.profile.model_copy(update={"manifest": manifest})}
    )

    assert "EARSHOT_RECOVERY_DECLARATION_REQUIRED" in issue_codes(broken)


def test_a_truncated_but_cleanly_closed_bundle_still_needs_no_declaration() -> None:
    """The rule keys on finality, not completeness, so this stays valid."""

    bundle = make_valid_bundle()
    manifest = bundle.profile.manifest.model_copy(
        update={"finality": "final", "completeness": "incomplete"}
    )
    truncated = bundle.model_copy(
        update={"profile": bundle.profile.model_copy(update={"manifest": manifest})}
    )

    assert_valid_incident(truncated)


@pytest.mark.parametrize(
    "override",
    [
        {"finality": "final"},
        {"completeness": "complete"},
    ],
)
def test_an_unclosed_recovery_cannot_claim_a_clean_close(override: dict) -> None:
    bundle = _recovered(**override)

    assert "EARSHOT_RECOVERY_DECLARATION_CONTRADICTORY" in issue_codes(bundle)


def test_an_unclosed_recovery_cannot_claim_a_completed_session() -> None:
    bundle = _recovered()
    session = bundle.profile.session.model_copy(update={"status": "completed"})
    broken = bundle.model_copy(
        update={"profile": bundle.profile.model_copy(update={"session": session})}
    )

    assert "EARSHOT_RECOVERY_DECLARATION_CONTRADICTORY" in issue_codes(broken)


def test_an_unclosed_recovery_cannot_fabricate_a_session_end() -> None:
    """The last checkpoint's time is not the end of the session."""

    bundle = _recovered()
    session = bundle.profile.session.model_copy(
        update={
            "ended_at": TimePoint(
                source_time_unix_nano="1800000000000000000",
                monotonic_time_nano="0",
                clock_domain_id=bundle.profile.clock_domains[0].clock_domain_id,
            )
        }
    )
    broken = bundle.model_copy(
        update={"profile": bundle.profile.model_copy(update={"session": session})}
    )

    assert "EARSHOT_RECOVERY_SESSION_END_FABRICATED" in issue_codes(broken)


@pytest.mark.parametrize(
    "override",
    [{"torn_tail_bytes": 128}, {"journal_complete": False}],
)
def test_a_damaged_journal_cannot_have_observed_a_close(override: dict) -> None:
    bundle = make_valid_bundle()
    manifest = bundle.profile.manifest.model_copy(
        update={"recovery": _recovery(close_observed=True, **override)}
    )
    coverage = (
        *bundle.profile.coverage,
        Coverage(signal="recorder.session_close", availability="available"),
    )
    broken = bundle.model_copy(
        update={
            "profile": bundle.profile.model_copy(
                update={"manifest": manifest, "coverage": coverage}
            )
        }
    )

    assert "EARSHOT_RECOVERY_DECLARATION_CONTRADICTORY" in issue_codes(broken)


def test_a_recovered_artifact_must_report_its_close_coverage() -> None:
    bundle = _recovered()
    stripped = tuple(
        item for item in bundle.profile.coverage if item.signal != "recorder.session_close"
    )
    broken = bundle.model_copy(
        update={"profile": bundle.profile.model_copy(update={"coverage": stripped})}
    )

    assert "EARSHOT_RECOVERY_DECLARATION_REQUIRED" in issue_codes(broken)


def test_close_coverage_must_agree_with_the_declaration() -> None:
    bundle = _recovered()
    coverage = tuple(
        Coverage(signal="recorder.session_close", availability="available")
        if item.signal == "recorder.session_close"
        else item
        for item in bundle.profile.coverage
    )
    broken = bundle.model_copy(
        update={"profile": bundle.profile.model_copy(update={"coverage": coverage})}
    )

    assert "EARSHOT_RECOVERY_DECLARATION_REQUIRED" in issue_codes(broken)


def test_a_bundle_claiming_a_close_it_never_observed_is_refused_everywhere() -> None:
    """Encoders validate, so the contradiction cannot reach a wire format."""

    from earshot.codec import IncidentCodecError, encode_incident_protobuf

    bundle = _recovered(finality="final")

    with pytest.raises(IncidentValidationError):
        assert_valid_incident(bundle)
    for encode in (encode_incident_json, encode_incident_protobuf):
        with pytest.raises((IncidentValidationError, IncidentCodecError)):
            encode(bundle)


# ------------------------------------------------------------ version policy


def test_producers_emit_the_current_contract_version() -> None:
    assert CONTRACT_VERSION == "0.2.0"
    assert SEMANTIC_PROFILE_VERSION == "0.2.0"
    assert CONTRACT_VERSION in SUPPORTED_CONTRACT_VERSIONS
    assert SEMANTIC_PROFILE_VERSION in SUPPORTED_SEMANTIC_PROFILE_VERSIONS


@pytest.mark.parametrize("version", SUPPORTED_CONTRACT_VERSIONS)
def test_every_supported_contract_version_still_validates(version: str) -> None:
    bundle = make_valid_bundle()
    manifest = bundle.profile.manifest.model_copy(
        update={"schema_version": version, "semantic_profile_version": version}
    )
    candidate = bundle.model_copy(
        update={"profile": bundle.profile.model_copy(update={"manifest": manifest})}
    )

    assert_valid_incident(candidate)


def test_the_existing_0_1_0_fixture_corpus_still_validates() -> None:
    paths = sorted((ROOT / "fixtures" / "faults").glob("*.incident.json"))
    assert paths

    for path in paths:
        document = json.loads(path.read_text())
        assert document["profile"]["manifest"]["schema_version"] == "0.1.0"
        assert_valid_incident(decode_incident_json(path.read_bytes()))


@pytest.mark.parametrize("version", ["0.3.0", "1.0.0", "99.0.0", "not-a-version"])
def test_an_unsupported_version_is_still_rejected(version: str) -> None:
    bundle = make_valid_bundle()
    manifest = bundle.profile.manifest.model_copy(update={"schema_version": version})
    candidate = bundle.model_copy(
        update={"profile": bundle.profile.model_copy(update={"manifest": manifest})}
    )

    assert "EARSHOT_SCHEMA_VERSION_UNSUPPORTED" in issue_codes(candidate)

    manifest = bundle.profile.manifest.model_copy(update={"semantic_profile_version": version})
    candidate = bundle.model_copy(
        update={"profile": bundle.profile.model_copy(update={"manifest": manifest})}
    )
    assert "EARSHOT_SEMANTIC_PROFILE_VERSION_UNSUPPORTED" in issue_codes(candidate)


def test_a_0_1_0_bundle_cannot_carry_a_recovery_declaration() -> None:
    """0.1.0 has no ``recovery`` member, so claiming it while using one is a lie."""

    bundle = _recovered()
    manifest = bundle.profile.manifest.model_copy(
        update={"schema_version": "0.1.0", "semantic_profile_version": "0.1.0"}
    )
    candidate = bundle.model_copy(
        update={"profile": bundle.profile.model_copy(update={"manifest": manifest})}
    )

    assert "EARSHOT_SCHEMA_VERSION_UNSUPPORTED" in issue_codes(candidate)


def test_a_recovered_bundle_survives_the_json_and_protobuf_round_trip() -> None:
    from earshot.codec import decode_incident_protobuf, encode_incident_protobuf

    bundle = _recovered()

    from_json = decode_incident_json(encode_incident_json(bundle))
    from_protobuf = decode_incident_protobuf(encode_incident_protobuf(bundle))

    for candidate in (from_json, from_protobuf):
        recovery = candidate.profile.manifest.recovery
        assert recovery is not None
        assert recovery.close_observed is False
        assert recovery.journal_id == "journal-1"
        assert candidate.profile.session.ended_at is None
