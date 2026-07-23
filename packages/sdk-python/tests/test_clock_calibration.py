"""Cross-clock calibration: declared ClockRelations align wall timestamps.

These tests exercise the alignment layer that `analysis.comparable_delta` used to
refuse outright. A latency across two clock domains is computed only inside a
declared, in-window ``ClockRelation`` -- with the calibration's own uncertainty
propagated -- and stays unavailable otherwise.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from earshot.analysis import _ClockAligner, analyze_incident, comparable_delta
from earshot.contract import ClockDomain, ClockRelation, TimePoint
from earshot.validation import validate_incident
from incident_factory import make_valid_bundle, point

pytestmark = pytest.mark.unit

SERVER_ORIGIN = 1_800_000_000_000_000_000
# The browser wall clock reads this many nanoseconds ahead of the server clock.
CLIENT_SKEW = 3000

CLIENT_DOMAIN = ClockDomain(
    clock_domain_id="client-render",
    kind="wall_clock",
    observer="browser",
    wall_origin_unix_nano=str(SERVER_ORIGIN + CLIENT_SKEW),
    uncertainty_nano="0",
)


def _client_point(nano: int) -> TimePoint:
    return point(nano, domain="client-render", wall_origin=SERVER_ORIGIN + CLIENT_SKEW)


def _server_point(nano: int) -> TimePoint:
    return point(nano, domain="server-clock", wall_origin=SERVER_ORIGIN)


def _calibration(**overrides: object) -> ClockRelation:
    """A client-render -> server-clock offset calibration."""

    params: dict[str, object] = {
        "relation_id": "rel-client-server",
        "from_clock_domain_id": "client-render",
        "to_clock_domain_id": "server-clock",
        "offset_nano": str(-CLIENT_SKEW),
        "uncertainty_nano": "500",
        "method": "handshake_offset",
    }
    params.update(overrides)
    return ClockRelation(**params)


def _cross_domain_render_bundle(*, relations: tuple[ClockRelation, ...] = ()):
    """Move the render operation/event into a client-render domain.

    The turn anchor stays on the server clock, so the render latency is only
    computable through a declared calibration between the two domains.
    """

    bundle = make_valid_bundle()
    profile = bundle.profile
    operations = tuple(
        op.model_copy(
            update={
                "started_at": _client_point(1_700_000_000),
                "ended_at": _client_point(1_900_000_000),
            }
        )
        if op.operation_id == "op-render"
        else op
        for op in profile.operations
    )
    events = tuple(
        ev.model_copy(update={"time": _client_point(1_720_000_000)})
        if ev.event_id == "evt-render"
        else ev
        for ev in profile.events
    )
    new_profile = profile.model_copy(
        update={
            "clock_domains": (*profile.clock_domains, CLIENT_DOMAIN),
            "clock_relations": relations,
            "operations": operations,
            "events": events,
        }
    )
    return bundle.model_copy(update={"profile": new_profile})


def _analyze(bundle):
    return analyze_incident(
        bundle,
        input_sha256="a" * 64,
        generated_at_unix_nano="1800000005000000000",
    )


def _render_metric(analysis) -> dict:
    return analysis.projections["turns"][0]["metrics"]["render_start_response_latency"]


# A committed turn anchor on the server clock, and a render point on the client
# clock, that alignment should relate to a 720 ms render latency.
_START = TimePoint(
    source_time_unix_nano=str(SERVER_ORIGIN + 1_000_000_000),
    clock_domain_id="server-clock",
    uncertainty_nano="10",
)
_END = TimePoint(
    source_time_unix_nano=str(SERVER_ORIGIN + CLIENT_SKEW + 1_720_000_000),
    clock_domain_id="client-render",
    uncertainty_nano="20",
)


# (a) A valid calibration yields an available, estimated, uncertainty-carrying delta.
def test_calibrated_cross_clock_delta_is_available_and_estimated() -> None:
    aligner = _ClockAligner((_calibration(),))
    delta = comparable_delta(_START, _END, aligner)
    assert delta.availability == "available"
    assert delta.basis == "cross_clock_calibrated"
    assert delta.confidence == "estimated"
    assert delta.nanoseconds == 720_000_000
    # start (10) + end (20) + calibration bound (500).
    assert delta.uncertainty == 530
    assert delta.uncertainty >= 500  # the relation's own error bound is included


def test_calibrated_render_latency_becomes_available_end_to_end() -> None:
    bundle = _cross_domain_render_bundle(relations=(_calibration(),))
    assert validate_incident(bundle).ok, validate_incident(bundle)
    metric = _render_metric(_analyze(bundle))
    assert metric["availability"] == "available"
    assert metric["confidence"] == "estimated"
    assert metric["value"] == pytest.approx(720.0)
    assert metric["unit"] == "ms"


# (b) The same scenario without a relation stays refused.
def test_without_relation_cross_clock_delta_is_unavailable() -> None:
    aligner = _ClockAligner(())
    delta = comparable_delta(_START, _END, aligner)
    assert delta.availability == "unavailable"
    assert delta.limitation == "cross_clock_domain"
    # No aligner at all behaves identically.
    assert comparable_delta(_START, _END).limitation == "cross_clock_domain"


def test_without_relation_render_latency_stays_cross_clock_domain_end_to_end() -> None:
    bundle = _cross_domain_render_bundle(relations=())
    assert validate_incident(bundle).ok, validate_incident(bundle)
    metric = _render_metric(_analyze(bundle))
    assert metric["availability"] == "unavailable"
    assert metric["limitation"] == "cross_clock_domain"


# (c) A calibration whose validity window has expired does not align.
def test_expired_validity_window_refuses_alignment() -> None:
    expired = _calibration(
        valid_from_unix_nano="0",
        valid_to_unix_nano=str(SERVER_ORIGIN + 1_000_000_000),
    )
    aligner = _ClockAligner((expired,))
    delta = comparable_delta(_START, _END, aligner)
    assert delta.availability == "unavailable"
    assert delta.limitation == "cross_clock_domain"


def test_expired_validity_window_end_to_end() -> None:
    expired = _calibration(
        valid_from_unix_nano="0",
        valid_to_unix_nano=str(SERVER_ORIGIN + 1_000_000_000),
    )
    bundle = _cross_domain_render_bundle(relations=(expired,))
    assert validate_incident(bundle).ok, validate_incident(bundle)
    metric = _render_metric(_analyze(bundle))
    assert metric["availability"] == "unavailable"
    assert metric["limitation"] == "cross_clock_domain"


# (d) A calibration that reverses the ordering is inconsistent, not clamped.
def test_calibration_producing_negative_latency_is_inconsistent() -> None:
    reversing = _calibration(offset_nano=str(-2_000_000_000))
    aligner = _ClockAligner((reversing,))
    delta = comparable_delta(_START, _END, aligner)
    assert delta.availability == "inconsistent"
    assert delta.basis == "cross_clock_calibrated"
    assert delta.limitation == "calibrated_time_reversed"
    assert delta.nanoseconds is None


# (e) An inverse-direction relation is applied in reverse.
def test_inverse_direction_relation_aligns_in_reverse() -> None:
    inverse = ClockRelation(
        relation_id="rel-server-client",
        from_clock_domain_id="server-clock",
        to_clock_domain_id="client-render",
        offset_nano=str(CLIENT_SKEW),
        uncertainty_nano="500",
        method="handshake_offset",
    )
    aligner = _ClockAligner((inverse,))
    delta = comparable_delta(_START, _END, aligner)
    assert delta.availability == "available"
    assert delta.basis == "cross_clock_calibrated"
    assert delta.confidence == "estimated"
    assert delta.nanoseconds == 720_000_000


def test_drift_correction_is_anchored_at_reference() -> None:
    # 1000 ppm drift over a 1 second gap after the reference is a 1_000_000 ns shift.
    reference = SERVER_ORIGIN + CLIENT_SKEW + 720_000_000
    drifting = _calibration(
        offset_nano=str(-CLIENT_SKEW),
        drift_ppm=1000.0,
        reference_unix_nano=str(reference),
    )
    aligner = _ClockAligner((drifting,))
    aligned = aligner.align(_END, "server-clock")
    assert aligned is not None
    aligned_wall, added_uncertainty = aligned
    end_wall = SERVER_ORIGIN + CLIENT_SKEW + 1_720_000_000
    gap = end_wall - reference
    expected = end_wall + (-CLIENT_SKEW + int(1000.0 * gap / 1e6))
    assert aligned_wall == expected
    assert added_uncertainty == 500


# (f) Same-domain behaviour is unchanged, even when an aligner is supplied.
def test_same_domain_behaviour_is_unchanged_with_aligner() -> None:
    aligner = _ClockAligner((_calibration(),))
    delta = comparable_delta(point(1_000_000), point(3_500_000), aligner)
    assert delta.availability == "available"
    assert delta.basis == "monotonic"
    assert delta.confidence == "measured"
    assert delta.nanoseconds == 2_500_000
    # A reversed same-domain pair is still inconsistent, not aligned across clocks.
    reversed_delta = comparable_delta(point(10), point(9), aligner)
    assert reversed_delta.availability == "inconsistent"
    assert reversed_delta.basis == "monotonic"


def test_monotonic_values_are_never_aligned_across_domains() -> None:
    # Two points sharing only monotonic values across domains never subtract, even
    # with a relation present: monotonic clocks are domain-local.
    aligner = _ClockAligner((_calibration(),))
    start = TimePoint(monotonic_time_nano="1000", clock_domain_id="server-clock")
    end = TimePoint(monotonic_time_nano="2000", clock_domain_id="client-render")
    delta = comparable_delta(start, end, aligner)
    assert delta.availability == "unavailable"
    assert delta.limitation == "cross_clock_domain"


# (g) Validation rejects self-relations, unknown domains, and reversed windows.
def test_self_relation_rejected_by_contract() -> None:
    with pytest.raises(ValidationError):
        ClockRelation(
            relation_id="rel-self",
            from_clock_domain_id="server-clock",
            to_clock_domain_id="server-clock",
            offset_nano="0",
            method="handshake_offset",
        )


def test_reversed_validity_window_rejected_by_contract() -> None:
    with pytest.raises(ValidationError):
        ClockRelation(
            relation_id="rel-window",
            from_clock_domain_id="client-render",
            to_clock_domain_id="server-clock",
            offset_nano="0",
            method="handshake_offset",
            valid_from_unix_nano="100",
            valid_to_unix_nano="50",
        )


def test_signed_offset_beyond_int64_rejected_by_contract() -> None:
    with pytest.raises(ValidationError):
        ClockRelation(
            relation_id="rel-overflow",
            from_clock_domain_id="client-render",
            to_clock_domain_id="server-clock",
            offset_nano=str(1 << 63),
            method="handshake_offset",
        )


def test_unknown_clock_domain_flagged_by_validation() -> None:
    bundle = make_valid_bundle()
    relation = ClockRelation(
        relation_id="rel-ghost",
        from_clock_domain_id="server-clock",
        to_clock_domain_id="ghost-domain",
        offset_nano="0",
        method="handshake_offset",
    )
    broken = bundle.model_copy(
        update={"profile": bundle.profile.model_copy(update={"clock_relations": (relation,)})}
    )
    codes = {issue.code for issue in validate_incident(broken).errors}
    assert "EARSHOT_UNKNOWN_CLOCK_DOMAIN" in codes


def test_duplicate_relation_id_flagged_by_validation() -> None:
    first = _calibration()
    second = _calibration(
        relation_id="rel-client-server",
        from_clock_domain_id="server-clock",
        to_clock_domain_id="client-render",
        offset_nano="0",
    )
    bundle = _cross_domain_render_bundle(relations=(first, second))
    codes = {issue.code for issue in validate_incident(bundle).errors}
    assert "EARSHOT_DUPLICATE_ID" in codes


def test_valid_calibration_bundle_passes_validation() -> None:
    bundle = _cross_domain_render_bundle(relations=(_calibration(),))
    report = validate_incident(bundle)
    assert report.ok, report
