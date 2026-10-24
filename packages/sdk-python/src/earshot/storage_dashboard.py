"""The dashboard: a project's calls over a time range, bucketed and grouped.

**Which calls.** A call is on the dashboard when it has a projection whose
``started_unix_nano`` is in ``[from, to)``. That is the call's own evidence start
(its earliest span or recording), not the server's receive time, which for a
webhook or an import can be hours later. A projection is computed from one
revision of its call, so every per-turn value comes from that one revision: a call
whose revisions each restate its turns is never counted twice. A stale projection
is still the call's latest computed view, and it counts. Calls without a
projection (open, or waiting for the projection job) have no start of their own
yet; they are only counted, by receive time (``not_projected_calls``).

**Buckets** are ``started_unix_nano / b * b`` in SQL (integer division on both
dialects): UTC hours or days. Every bucket that overlaps the range is listed,
empty or not. ``groups`` split the same calls by agent or by framework (``None``:
unknown), across the whole range.

**Tiles.** Every tile says which calls it covers (``calls`` of ``of_calls``): an
unknown value is left out and its call counted as not covered, never read as 0.

- **Latency** (reported, audible gap): percentiles over **turns**, read from each
  projection's ``turns``; a percentile of each call's own median would hide a slow
  tail. ``of_turns`` counts every turn, with a value or not.
- **LLM first token by provider**: the ``turn_metrics`` rows of the incident of the
  projection's revision (the provider is the turn's LLM provider), split by the
  measurement's basis, since values measured on different bases do not compare.
- **Interruption rate**: barge-ins per turn, over the calls whose barge-ins were
  measured (one recorder timeline).
- **Dead air**: total dead air over those calls' total duration.
- **Cost per minute**: Earshot's (complete costs only: the priced items plus the
  STT estimate) and the provider's reported cost, side by side and never added
  together: a hosted provider's cost already includes the stages Earshot prices.

**Percentiles** are nearest-rank (an observed value), computed in Python, since
SQLite has no percentile function. The reads run in one read snapshot.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .storage import IncidentStore
    from .storage_database import DatabaseConnection

_NANOS_PER_SECOND = 1_000_000_000
BUCKET_NANOS = {"hour": 3_600 * _NANOS_PER_SECOND, "day": 86_400 * _NANOS_PER_SECOND}
GROUP_COLUMNS = {"framework": "framework", "agent": "agent"}
# A dashboard lists every bucket of its range; this bounds the response.
MAX_BUCKETS = 1000
_INT64_MAX = 2**63 - 1
_MS_PER_MINUTE = 60_000

_IN_RANGE = "p.project_id = ? AND p.started_unix_nano >= ? AND p.started_unix_nano < ?"
_CALLS_SQL = (
    "SELECT p.call_id, p.started_unix_nano / CAST(? AS BIGINT) * CAST(? AS BIGINT) AS bucket, "
    "p.{group} AS group_value, p.turn_count, p.duration_ms, p.dead_air_ms, p.barge_in_count, "
    "p.cost_lower_bound_nano_usd, p.cost_estimated_nano_usd, p.cost_unknown_items, "
    "p.provider_cost_nano_usd, p.provider_cost_basis "
    f"FROM call_projections p WHERE {_IN_RANGE}"
)
# Each projected turn's reported latency and, when measured, its audible gap.
_TURNS_SQLITE = (
    "SELECT p.call_id, json_extract(t.value, '$.reported_latency_ms') AS reported_ms, "
    "CASE WHEN json_extract(t.value, '$.audible_gap.status') = 'measured' "
    "THEN json_extract(t.value, '$.audible_gap.value_ms') END AS gap_ms "
    "FROM call_projections p, json_each(p.detail_json, '$.turns') AS t "
    f"WHERE {_IN_RANGE}"
)
_TURNS_POSTGRESQL = (
    "SELECT p.call_id, (t.value ->> 'reported_latency_ms')::bigint AS reported_ms, "
    "CASE WHEN t.value -> 'audible_gap' ->> 'status' = 'measured' "
    "THEN (t.value -> 'audible_gap' ->> 'value_ms')::bigint END AS gap_ms "
    "FROM call_projections p "
    "CROSS JOIN LATERAL json_array_elements(p.detail_json::json -> 'turns') AS t(value) "
    f"WHERE {_IN_RANGE}"
)
# The LLM first-token measurements of the projection's own revision. A bundle ID is
# an incident's global key, so the revision's names its rows alone; joining on the
# project too led SQLite to scan the project's every turn row once per call.
_LLM_SQL = (
    "SELECT p.call_id, m.provider, m.first_token_basis AS basis, m.first_token_ms AS value "
    "FROM call_projections p JOIN call_revisions r ON r.project_id = p.project_id "
    "AND r.call_id = p.call_id AND r.revision = p.revision "
    "JOIN turn_metrics m ON m.bundle_id = r.bundle_id "
    f"WHERE {_IN_RANGE} AND m.first_token_availability = 'available' "
    "AND m.first_token_ms IS NOT NULL"
)
_NOT_PROJECTED_SQL = (
    "SELECT count(*) FROM calls c WHERE c.project_id = ? "
    "AND c.first_evidence_unix_nano >= ? AND c.first_evidence_unix_nano < ? "
    "AND NOT EXISTS (SELECT 1 FROM call_projections p WHERE p.project_id = c.project_id "
    "AND p.call_id = c.call_id AND p.started_unix_nano IS NOT NULL)"
)


class DashboardRangeError(ValueError):
    """A dashboard request that names no range, or one with too many buckets."""

    def __init__(self, parameter: str, message: str) -> None:
        super().__init__(message)
        self.parameter = parameter


@dataclass(frozen=True, slots=True)
class DashboardQuery:
    """``[from_unix_nano, to_unix_nano)``, bucketed by ``bucket``, split by ``group_by``."""

    from_unix_nano: int
    to_unix_nano: int
    bucket: str = "day"
    group_by: str = "framework"

    def __post_init__(self) -> None:
        if self.bucket not in BUCKET_NANOS:
            raise DashboardRangeError("bucket", "bucket must be hour or day")
        if self.group_by not in GROUP_COLUMNS:
            raise DashboardRangeError("group_by", "group_by must be framework or agent")
        if not 0 <= self.from_unix_nano <= _INT64_MAX:
            raise DashboardRangeError("from", "from must be in [0, 2^63 - 1]")
        if not self.from_unix_nano < self.to_unix_nano <= _INT64_MAX:
            raise DashboardRangeError("to", "to must be after from and at most 2^63 - 1")
        if len(self.bucket_starts()) > MAX_BUCKETS:
            raise DashboardRangeError(
                "to", f"the range must span at most {MAX_BUCKETS} {self.bucket} buckets"
            )

    def bucket_starts(self) -> range:
        size = BUCKET_NANOS[self.bucket]
        first = self.from_unix_nano // size * size
        return range(first, (self.to_unix_nano - 1) // size * size + 1, size)


@dataclass(slots=True)
class _Call:
    """One projected call's row, with its turns' values."""

    bucket: int
    group: str | None
    turn_count: int
    duration_ms: int | None
    dead_air_ms: int | None
    barge_in_count: int | None
    cost_lower_bound_nano_usd: int | None
    cost_estimated_nano_usd: int | None
    cost_unknown_items: int
    provider_cost_nano_usd: int | None
    provider_cost_basis: str | None
    reported_ms: list[int] = field(default_factory=list)
    gap_ms: list[int] = field(default_factory=list)
    # ``(provider, basis, ms)`` per LLM first-token measurement.
    llm: list[tuple[str | None, str, int]] = field(default_factory=list)


def _known(value: Any) -> int | None:
    return None if value is None else int(value)


def _whole_ms(value: float) -> int:
    """Milliseconds, rounded half up."""

    return math.floor(value + 0.5)


def _rank(ordered: Sequence[int], percent: int) -> int:
    """The nearest-rank ``percent``-th percentile of ``ordered`` (not empty)."""

    return ordered[(percent * len(ordered) + 99) // 100 - 1]


def _percentiles(values: Iterable[int]) -> dict[str, int | None]:
    ordered = sorted(values)
    if not ordered:
        return {"p50_ms": None, "p95_ms": None}
    return {"p50_ms": _rank(ordered, 50), "p95_ms": _rank(ordered, 95)}


def _sum(values: Sequence[int]) -> int | None:
    """The sum of known values; ``None`` when there are none (unknown, not 0)."""

    return sum(values) if values else None


def _latency(calls: Sequence[_Call], values: Callable[[_Call], list[int]]) -> dict[str, Any]:
    measured = [value for call in calls for value in values(call)]
    return {
        **_percentiles(measured),
        "turns": len(measured),
        "of_turns": sum(call.turn_count for call in calls),
        "calls": sum(1 for call in calls if values(call)),
        "of_calls": len(calls),
    }


def _llm_first_token(calls: Sequence[_Call]) -> dict[str, Any]:
    groups: dict[tuple[str | None, str], tuple[list[int], set[int]]] = {}
    for index, call in enumerate(calls):
        for provider, basis, value in call.llm:
            values, members = groups.setdefault((provider, basis), ([], set()))
            values.append(value)
            members.add(index)
    return {
        "providers": [
            {
                "provider": provider,
                "basis": basis,
                **_percentiles(values),
                "turns": len(values),
                "calls": len(members),
            }
            for (provider, basis), (values, members) in sorted(
                groups.items(), key=lambda item: (item[0][0] is None, item[0][0] or "", item[0][1])
            )
        ],
        "calls": sum(1 for call in calls if call.llm),
        "of_calls": len(calls),
    }


def _per_minute(cost_nano_usd: int, duration_ms: int) -> int:
    """nano-USD per minute, rounded half up (exact integer arithmetic)."""

    return (2 * cost_nano_usd * _MS_PER_MINUTE + duration_ms) // (2 * duration_ms)


def _cost_rate(calls: Sequence[_Call], covered: list[tuple[int, int]]) -> dict[str, Any]:
    """``covered``: ``(cost_nano_usd, duration_ms)`` of each call the rate covers."""

    cost = _sum([item[0] for item in covered])
    duration = _sum([item[1] for item in covered])
    return {
        "nano_usd_per_minute": None
        if cost is None or duration is None
        else _per_minute(cost, duration),
        "cost_nano_usd": cost,
        "duration_ms": duration,
        "calls": len(covered),
        "of_calls": len(calls),
    }


def _tiles(calls: Sequence[_Call]) -> dict[str, Any]:
    interrupted = [call for call in calls if call.barge_in_count is not None]
    silent = [
        call for call in calls if call.dead_air_ms is not None and call.duration_ms is not None
    ]
    # (cost, duration) of the calls each cost rate covers: a known, positive duration
    # and, for Earshot's, a complete cost.
    earshot: list[tuple[int, int]] = []
    reported: list[tuple[int, int]] = []
    bases: set[str] = set()
    for call in calls:
        if not call.duration_ms:
            continue
        if call.cost_unknown_items == 0 and call.cost_lower_bound_nano_usd is not None:
            total = call.cost_lower_bound_nano_usd + (call.cost_estimated_nano_usd or 0)
            earshot.append((total, call.duration_ms))
        if call.provider_cost_nano_usd is not None:
            reported.append((call.provider_cost_nano_usd, call.duration_ms))
            if call.provider_cost_basis is not None:
                bases.add(call.provider_cost_basis)
    return {
        "calls": len(calls),
        "reported_latency": _latency(calls, lambda call: call.reported_ms),
        "audible_gap": _latency(calls, lambda call: call.gap_ms),
        "llm_first_token": _llm_first_token(calls),
        "interruption_rate": {
            "barge_ins": _sum([call.barge_in_count for call in interrupted]),
            "turns": sum(call.turn_count for call in interrupted),
            "calls": len(interrupted),
            "of_calls": len(calls),
        },
        "dead_air": {
            "dead_air_ms": _sum([call.dead_air_ms for call in silent]),
            "duration_ms": _sum([call.duration_ms for call in silent]),
            "calls": len(silent),
            "of_calls": len(calls),
        },
        "cost_per_minute": {
            "earshot": _cost_rate(calls, earshot),
            "provider": {**_cost_rate(calls, reported), "bases": sorted(bases)},
        },
    }


def _read_calls(
    connection: DatabaseConnection, project_id: str, query: DashboardQuery
) -> dict[str, _Call]:
    size = BUCKET_NANOS[query.bucket]
    in_range = (project_id, query.from_unix_nano, query.to_unix_nano)
    calls = {
        row["call_id"]: _Call(
            bucket=int(row["bucket"]),
            group=row["group_value"],
            turn_count=int(row["turn_count"]),
            duration_ms=_known(row["duration_ms"]),
            dead_air_ms=_known(row["dead_air_ms"]),
            barge_in_count=_known(row["barge_in_count"]),
            cost_lower_bound_nano_usd=_known(row["cost_lower_bound_nano_usd"]),
            cost_estimated_nano_usd=_known(row["cost_estimated_nano_usd"]),
            cost_unknown_items=int(row["cost_unknown_items"]),
            provider_cost_nano_usd=_known(row["provider_cost_nano_usd"]),
            provider_cost_basis=row["provider_cost_basis"],
        )
        for row in connection.execute(
            _CALLS_SQL.format(group=GROUP_COLUMNS[query.group_by]), (size, size, *in_range)
        ).fetchall()
    }
    turns = connection.dialect.sql(
        "dashboard-turns", sqlite=_TURNS_SQLITE, postgresql=_TURNS_POSTGRESQL
    )
    for row in connection.execute(turns, in_range).fetchall():
        call = calls[row["call_id"]]
        if row["reported_ms"] is not None:
            call.reported_ms.append(int(row["reported_ms"]))
        if row["gap_ms"] is not None:
            call.gap_ms.append(int(row["gap_ms"]))
    for row in connection.execute(_LLM_SQL, in_range).fetchall():
        calls[row["call_id"]].llm.append(
            (row["provider"], row["basis"], _whole_ms(float(row["value"])))
        )
    return calls


class DashboardReads:
    """Dashboard reads on one ``IncidentStore``."""

    def __init__(self, store: IncidentStore) -> None:
        self.store = store

    def dashboard(self, project_id: str, query: DashboardQuery) -> dict[str, Any]:
        """The project's dashboard over ``query``'s range (module docstring), as the
        API renders it: Unix nanoseconds as decimal strings."""

        with self.store._listing_connection() as connection:
            connection.begin_read()
            self.store._require_active_project(connection, project_id)
            calls = list(_read_calls(connection, project_id, query).values())
            [not_projected] = connection.execute(
                _NOT_PROJECTED_SQL, (project_id, query.from_unix_nano, query.to_unix_nano)
            ).fetchone()
        by_bucket: dict[int, list[_Call]] = {}
        by_group: dict[str | None, list[_Call]] = {}
        for call in calls:
            by_bucket.setdefault(call.bucket, []).append(call)
            by_group.setdefault(call.group, []).append(call)
        groups = sorted(
            by_group.items(),
            key=lambda item: (-len(item[1]), item[0] is None, item[0] or ""),
        )
        return {
            "from_unix_nano": str(query.from_unix_nano),
            "to_unix_nano": str(query.to_unix_nano),
            "bucket": query.bucket,
            "group_by": query.group_by,
            "not_projected_calls": int(not_projected),
            "overall": _tiles(calls),
            "buckets": [
                {"start_unix_nano": str(start), "tiles": _tiles(by_bucket.get(start, []))}
                for start in query.bucket_starts()
            ],
            "groups": [{"value": value, "tiles": _tiles(members)} for value, members in groups],
        }
