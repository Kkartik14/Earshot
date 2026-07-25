"""Cross-clock calibration: declared ClockRelations align wall timestamps.

These tests exercise the alignment layer that `analysis.comparable_delta` used to
refuse outright. A latency across two clock domains is computed only inside a
declared, in-window ``ClockRelation`` -- with the calibration's own uncertainty
propagated -- and stays unavailable otherwise.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from earshot.analysis import (
    _AMBIGUOUS_ALIGNMENT,
    _DEGENERATE_ALIGNMENT,
    _UNREPRESENTABLE_ALIGNMENT,
    _ClockAligner,
    analyze_incident,
    comparable_delta,
)
from earshot.contract import MAX_CLOCK_DRIFT_PPM, ClockDomain, ClockRelation, TimePoint
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


# --- F5(a): exact affine inverse with drift ----------------------------------


def _drift_relation(**overrides: object) -> ClockRelation:
    params: dict[str, object] = {
        "relation_id": "rel-drift",
        "from_clock_domain_id": "A",
        "to_clock_domain_id": "B",
        "offset_nano": "250",
        "drift_ppm": 500.0,
        "reference_unix_nano": "1000000000",
        "uncertainty_nano": "0",
        "method": "handshake_offset",
    }
    params.update(overrides)
    return ClockRelation(**params)


def test_drift_inverse_is_the_exact_affine_inverse() -> None:
    # INVARIANT: for a relation with non-zero drift, inverse(forward(t)) == t.
    # The old code applied ``wall - correction`` on the inverse path, which is not
    # the inverse of ``wall + offset + drift*(wall-ref)`` and drifts off by tens of
    # nanoseconds as t moves away from the reference.
    aligner = _ClockAligner((_drift_relation(),))
    reference = 1_000_000_000
    for gap in (0, 1_000_000, 200_000_000, -50_000_000):
        t = reference + gap
        a_point = TimePoint(source_time_unix_nano=str(t), clock_domain_id="A")
        forward = aligner.align(a_point, "B")
        assert isinstance(forward, tuple)
        forward_value = forward[0]
        # Hand-computed forward: t + offset(250) + round(0.0005 * gap).
        assert forward_value == t + 250 + round(0.0005 * gap)
        b_point = TimePoint(source_time_unix_nano=str(forward_value), clock_domain_id="B")
        inverse = aligner.align(b_point, "A")
        assert isinstance(inverse, tuple)
        assert abs(inverse[0] - t) <= 1, (t, inverse[0])


def test_degenerate_drift_slope_refuses_inverse_without_crashing() -> None:
    # drift_ppm == -1e6 makes slope ``1 + (-1) == 0``: f collapses to a constant and
    # is not invertible. The contract now refuses such a relation outright, so the
    # only way to reach the arithmetic with one is to bypass validation -- and the
    # arithmetic must still refuse it rather than divide by zero.
    with pytest.raises(ValidationError):
        _drift_relation(relation_id="rel-degenerate", drift_ppm=-1_000_000.0, offset_nano="0")
    unvalidated = ClockRelation.model_construct(
        relation_id="rel-degenerate",
        from_clock_domain_id="A",
        to_clock_domain_id="B",
        offset_nano="0",
        drift_ppm=-1_000_000.0,
        reference_unix_nano="1000000000",
        uncertainty_nano="0",
        method="handshake_offset",
    )
    aligner = _ClockAligner((unvalidated,))
    b_point = TimePoint(source_time_unix_nano="1000000500", clock_domain_id="B")
    assert aligner.align(b_point, "A") is _DEGENERATE_ALIGNMENT


# --- F5(b): validity bounds live in the from-domain coordinate ----------------


def test_inverse_validity_window_uses_from_domain_coordinate() -> None:
    # Window is declared in the ``from`` (A) domain; B = A + offset(-9e8).
    relation = ClockRelation(
        relation_id="rel-window",
        from_clock_domain_id="A",
        to_clock_domain_id="B",
        offset_nano="-900000000",
        method="handshake_offset",
        valid_from_unix_nano="1000000000",
        valid_to_unix_nano="3000000000",
    )
    aligner = _ClockAligner((relation,))
    # Inverse B->A of 2e8 gives A = 1.1e9, inside [1e9, 3e9]; the old code compared
    # the *input* 2e8 against the A-window and wrongly refused it.
    in_window = TimePoint(source_time_unix_nano="200000000", clock_domain_id="B")
    aligned = aligner.align(in_window, "A")
    assert isinstance(aligned, tuple)
    assert aligned[0] == 1_100_000_000
    # Inverse B->A of 2.5e9 gives A = 3.4e9, outside the A-window; the old code saw
    # the input 2.5e9 sitting inside [1e9, 3e9] and wrongly aligned it.
    out_of_window = TimePoint(source_time_unix_nano="2500000000", clock_domain_id="B")
    assert aligner.align(out_of_window, "A") is None


# --- F5(c): overlapping relations are reconciled, not lexically picked --------


def _pair_relation(relation_id: str, offset: int, uncertainty: int) -> ClockRelation:
    return ClockRelation(
        relation_id=relation_id,
        from_clock_domain_id="A",
        to_clock_domain_id="B",
        offset_nano=str(offset),
        uncertainty_nano=str(uncertainty),
        method="handshake_offset",
    )


def test_overlapping_relations_that_agree_are_used_deterministically() -> None:
    aligner = _ClockAligner(
        (_pair_relation("rel-b", 1200, 600), _pair_relation("rel-a", 1000, 600))
    )
    a_point = TimePoint(source_time_unix_nano="5000000000", clock_domain_id="A")
    aligned = aligner.align(a_point, "B")
    assert isinstance(aligned, tuple)
    # |1200-1000| = 200 <= 600+600: they agree; the tighter-then-smaller value wins.
    assert aligned[0] == 5_000_001_000


def test_overlapping_relations_that_disagree_are_ambiguous_not_a_silent_pick() -> None:
    aligner = _ClockAligner(
        (_pair_relation("rel-a", 1000, 100), _pair_relation("rel-b", 9000, 100))
    )
    a_point = TimePoint(source_time_unix_nano="5000000000", clock_domain_id="A")
    # |9000-1000| = 8000 > 100+100: the two calibrations materially disagree.
    assert aligner.align(a_point, "B") is _AMBIGUOUS_ALIGNMENT


def test_ambiguous_calibration_makes_cross_clock_delta_unavailable() -> None:
    start = TimePoint(source_time_unix_nano="5000000000", clock_domain_id="A", uncertainty_nano="0")
    end = TimePoint(source_time_unix_nano="5000000500", clock_domain_id="B", uncertainty_nano="0")
    aligner = _ClockAligner(
        (
            ClockRelation(
                relation_id="rel-a",
                from_clock_domain_id="B",
                to_clock_domain_id="A",
                offset_nano="-100",
                uncertainty_nano="10",
                method="handshake_offset",
            ),
            ClockRelation(
                relation_id="rel-b",
                from_clock_domain_id="B",
                to_clock_domain_id="A",
                offset_nano="-9000",
                uncertainty_nano="10",
                method="handshake_offset",
            ),
        )
    )
    delta = comparable_delta(start, end, aligner)
    assert delta.availability == "unavailable"
    assert delta.limitation == "cross_clock_ambiguous"


# --- F5(d)/(e): the contract rejects non-finite and unanchored drift ----------


def test_non_finite_drift_rejected_by_contract() -> None:
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            ClockRelation(
                relation_id="rel-nonfinite",
                from_clock_domain_id="A",
                to_clock_domain_id="B",
                offset_nano="0",
                drift_ppm=bad,
                reference_unix_nano="0",
                method="handshake_offset",
            )


def test_drift_without_reference_rejected_by_contract() -> None:
    with pytest.raises(ValidationError):
        ClockRelation(
            relation_id="rel-drift-noref",
            from_clock_domain_id="A",
            to_clock_domain_id="B",
            offset_nano="0",
            drift_ppm=10.0,
            method="handshake_offset",
        )
    # Zero (and absent) drift needs no reference.
    ClockRelation(
        relation_id="rel-zero-drift",
        from_clock_domain_id="A",
        to_clock_domain_id="B",
        offset_nano="0",
        drift_ppm=0.0,
        method="handshake_offset",
    )


# --- P1#6(a): a contract-valid drift can never make the analyzer raise --------


def _unvalidated_relation(**overrides: object) -> ClockRelation:
    """Build a relation the contract would reject, bypassing validation.

    The point of these tests is that the arithmetic is *total*: it refuses rather
    than raising even for a relation no validator would let through.
    """

    params: dict[str, object] = {
        "relation_id": "rel-unvalidated",
        "from_clock_domain_id": "B",
        "to_clock_domain_id": "A",
        "offset_nano": "0",
        "reference_unix_nano": "1000000000",
        "uncertainty_nano": "0",
        "method": "handshake_offset",
    }
    params.update(overrides)
    return ClockRelation.model_construct(**params)


_A_POINT = TimePoint(source_time_unix_nano="2000000000", clock_domain_id="A")
_B_POINT = TimePoint(source_time_unix_nano="3000000000", clock_domain_id="B")


@pytest.mark.parametrize(
    "drift_ppm",
    [
        1e308,  # the reviewer's crash: drift * gap overflows to inf, round() raised
        -1e308,
        1e300,
        MAX_CLOCK_DRIFT_PPM,  # slope 2.0 exactly: the first value the contract refuses
        -MAX_CLOCK_DRIFT_PPM,  # slope 0.0 exactly: non-invertible
        -2e6,  # slope -1.0: a calibration that runs time backwards
    ],
)
def test_drift_outside_the_documented_domain_is_refused_by_the_contract(
    drift_ppm: float,
) -> None:
    with pytest.raises(ValidationError):
        ClockRelation(
            relation_id="rel-absurd-drift",
            from_clock_domain_id="A",
            to_clock_domain_id="B",
            offset_nano="0",
            drift_ppm=drift_ppm,
            reference_unix_nano="1000000000",
            method="handshake_offset",
        )


def test_drift_at_the_edge_of_the_documented_domain_is_accepted_and_applies() -> None:
    # The bound is exclusive, so a drift just inside it is a legal calibration and
    # must still produce a value rather than a refusal.
    relation = ClockRelation(
        relation_id="rel-edge-drift",
        from_clock_domain_id="A",
        to_clock_domain_id="B",
        offset_nano="0",
        drift_ppm=MAX_CLOCK_DRIFT_PPM - 1.0,
        reference_unix_nano="1000000000",
        uncertainty_nano="0",
        method="handshake_offset",
    )
    aligned = _ClockAligner((relation,)).align(
        TimePoint(source_time_unix_nano="2000000000", clock_domain_id="A"), "B"
    )
    assert isinstance(aligned, tuple)


def test_extreme_drift_refuses_with_a_limitation_instead_of_raising() -> None:
    # Before the fix this exact input raised
    #   OverflowError: cannot convert float infinity to integer
    # from ``round(drift * (wall - reference))`` in ``_ClockAligner._map_wall``.
    # An analyzer must never crash on evidence -- it must decline to produce a value
    # and say why.
    aligner = _ClockAligner((_unvalidated_relation(drift_ppm=1e308),))
    delta = comparable_delta(_A_POINT, _B_POINT, aligner)
    assert delta.availability == "unavailable"
    assert delta.basis == "cross_clock_calibrated"
    assert delta.nanoseconds is None
    assert delta.limitation == "cross_clock_calibration_unrepresentable"


def test_alignment_leaving_the_uint64_domain_refuses_rather_than_reporting_it() -> None:
    # A legal drift can still carry an instant past the largest nanosecond value the
    # contract can express. That is not a coordinate, so it is not reported as one.
    relation = _unvalidated_relation(
        relation_id="rel-out-of-domain",
        offset_nano=str((1 << 63) - 1),
        drift_ppm=999_999.0,
        reference_unix_nano="0",
    )
    point_b = TimePoint(source_time_unix_nano=str((1 << 64) - 1), clock_domain_id="B")
    assert _ClockAligner((relation,)).align(point_b, "A") is _UNREPRESENTABLE_ALIGNMENT


# --- P1#6(b): a relation that reverses time is not a calibration --------------


def test_negative_slope_is_refused_forward_not_silently_reversed() -> None:
    # slope 1 + (-3e6/1e6) == -2: the forward map sends a later instant to an
    # earlier one. Applying it would produce a reversed -- and therefore fictional
    # -- delta, so the relation is refused in both directions.
    aligner = _ClockAligner((_unvalidated_relation(drift_ppm=-3e6),))
    assert aligner.align(_B_POINT, "A") is _DEGENERATE_ALIGNMENT
    delta = comparable_delta(_A_POINT, _B_POINT, aligner)
    assert delta.availability == "unavailable"
    assert delta.limitation == "cross_clock_calibration_degenerate"


def test_a_usable_relation_still_wins_over_a_degenerate_sibling() -> None:
    # Refusing a degenerate relation must not poison a declared, applicable one.
    aligner = _ClockAligner(
        (
            _unvalidated_relation(relation_id="rel-degenerate", drift_ppm=-3e6),
            ClockRelation(
                relation_id="rel-good",
                from_clock_domain_id="B",
                to_clock_domain_id="A",
                offset_nano="-500",
                uncertainty_nano="10",
                method="handshake_offset",
            ),
        )
    )
    aligned = aligner.align(_B_POINT, "A")
    assert aligned == (2_999_999_500, 10)


# --- P1#6(c): a relation's absent uncertainty is unknown, never zero ----------


def test_relation_without_uncertainty_yields_an_unknown_bound_not_zero() -> None:
    relation = ClockRelation(
        relation_id="rel-no-bound",
        from_clock_domain_id="B",
        to_clock_domain_id="A",
        offset_nano="0",
        method="handshake_offset",
    )
    aligned = _ClockAligner((relation,)).align(_B_POINT, "A")
    assert aligned == (3_000_000_000, None)  # unknown, not 0

    delta = comparable_delta(_A_POINT, _B_POINT, _ClockAligner((relation,)))
    assert delta.availability == "available"
    assert delta.nanoseconds == 1_000_000_000
    assert delta.uncertainty is None
    assert delta.limitation == "calibration_uncertainty_unknown"


def test_unknown_relation_bound_survives_metric_serialization() -> None:
    # The bound must not vanish on the way out: an unknown one is named, and a known
    # one is carried as a number in the metric's own unit.
    relation = ClockRelation(
        relation_id="rel-no-bound",
        from_clock_domain_id="B",
        to_clock_domain_id="A",
        offset_nano="0",
        method="handshake_offset",
    )
    unknown = comparable_delta(_A_POINT, _B_POINT, _ClockAligner((relation,))).as_dict()
    assert unknown["limitation"] == "calibration_uncertainty_unknown"
    assert "uncertainty" not in unknown

    bounded = comparable_delta(
        _A_POINT,
        _B_POINT,
        _ClockAligner((_pair_relation_to_a("rel-bound", 0, 2_000_000),)),
    ).as_dict()
    assert bounded["uncertainty"] == pytest.approx(2.0)  # 2_000_000 ns == 2 ms
    assert bounded["unit"] == "ms"
    assert "limitation" not in bounded


def _pair_relation_to_a(relation_id: str, offset: int, uncertainty: int) -> ClockRelation:
    return ClockRelation(
        relation_id=relation_id,
        from_clock_domain_id="B",
        to_clock_domain_id="A",
        offset_nano=str(offset),
        uncertainty_nano=str(uncertainty),
        method="handshake_offset",
    )


def test_relation_declaring_a_bound_is_preferred_over_one_declaring_none() -> None:
    # Both place the instant identically, so there is no disagreement; the tie is
    # broken toward the relation that actually declares how wrong it might be.
    aligner = _ClockAligner(
        (
            ClockRelation(
                relation_id="rel-unbounded",
                from_clock_domain_id="B",
                to_clock_domain_id="A",
                offset_nano="0",
                method="handshake_offset",
            ),
            _pair_relation_to_a("rel-bounded", 0, 40),
        )
    )
    assert aligner.align(_B_POINT, "A") == (3_000_000_000, 40)


def test_disagreeing_relations_with_an_unknown_bound_are_ambiguous() -> None:
    # An unknown bound cannot show that two differing placements agree, so the
    # alignment is not decidable -- exactly as when a known bound is too small.
    aligner = _ClockAligner(
        (
            ClockRelation(
                relation_id="rel-unbounded",
                from_clock_domain_id="B",
                to_clock_domain_id="A",
                offset_nano="0",
                method="handshake_offset",
            ),
            _pair_relation_to_a("rel-bounded", -9000, 10),
        )
    )
    assert aligner.align(_B_POINT, "A") is _AMBIGUOUS_ALIGNMENT
    delta = comparable_delta(_A_POINT, _B_POINT, aligner)
    assert delta.availability == "unavailable"
    assert delta.limitation == "cross_clock_ambiguous"
