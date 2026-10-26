"""A call's recorder timeline: the audible gap at the agent, signals, and placement (pure).

Everything here reads speech runs on **one recorder timeline**: the two channels of
one stereo recording that maps user and agent, or a mono pair (``one_timeline_pairs``).
Times are integer milliseconds from the timeline's first sample. Span times reach it
by the naive mapping ``(t - started_at) / 1e6``, corrected per turn where the
framework gives anchors.

**Mono pairs** (``one_timeline_pairs``, the one rule the projection and the analysis
job's stereo rendition both apply). A user and an agent mono recording are one
timeline when they are the only mono recordings of their speakers with that
``started_at``, and both durations are known and within ``ONE_TIMELINE_TOLERANCE_MS``.
Two mono recordings of one speaker at one start are ambiguous: neither pairs.

**Audible gap at the agent** (one per framework turn, ``audible_gap``):

1. *Agent on in the audio.* Pipecat's recorder pads the agent lane with digital
   zero while the bot is quiet and only ever inserts time, so the agent's audio
   starts at the right edge of an exact-zero run (``ZERO_RUN_ANCHOR_MS`` or longer): the
   edge nearest the naive-mapped A-on anchor within ``ANCHOR_SEARCH_MS``, else the
   onset of the nearest agent run, which is not an audio anchor. LiveKit's recorder
   places frames by wall clock, so its A-on maps naively
   (``continuous_timeline_assumed``).
2. *Pairing.* ``a`` is the agent run whose start is nearest that point, within
   ``PAIRING_WINDOW_MS``; none: ``no_agent_audio``. If a user run is active when
   ``a`` starts, the turn is ``talk_over``. Otherwise ``u*`` is the last user run
   ending by ``a.start`` with no agent run starting in between; none:
   ``no_clear_pairing``.
3. *Value.* ``a.start - u*.end`` ± ``sqrt(sigma_u^2 + sigma_a^2)`` (the boundaries' sigmas).
   If that uncertainty exceeds 100 ms, the gap is not measurable.
4. *Discontinuity* (Pipecat, which has both anchors): ``o_u = u*.end - naive(U-off)``
   and ``o_a = audio A-on - naive(A-on)``. A step ``|o_a - o_u|`` above
   ``DISCONTINUITY_STEP_MS`` means the recorder inserted time inside the gap: the
   turn is ``not_measurable / recorder_timeline_discontinuity`` and its raw gap is
   an upper bound (an insert-only recorder can only lengthen a gap). The step is
   never subtracted. The check needs both anchors and a zero-run edge at A-on: a
   Pipecat turn without them is measured on the ``continuous_timeline_assumed``
   basis, since comparing a run onset with the user-off anchor compares the heard
   gap with the reported latency, not two offsets.
5. *Declared discontinuity* (host profile): a turn whose producer declares its
   recording timeline was not continuous across it (``declared_discontinuity``) is
   ``not_measurable / recorder_timeline_discontinuity`` whatever it measured (a
   talk-over too), with no value, bound, basis or speech edges, which would give the
   gap back; the turn's offset and paired agent run stay, for placement.

A value is never fabricated: every other case is ``not_measurable`` with its reason.

**Signals** (cross-channel ones need one timeline):
talk time per channel; overlap (intervals of both speaking, each at least
``MIN_OVERLAP_MS``); barge-in (a user run starting inside an agent run, after its
first ``EDGE_GUARD_MS``, that is not a backchannel: shorter than
``BACKCHANNEL_MAX_MS`` with the agent still speaking ``YIELD_WINDOW_MS`` later);
talk-over (an agent run starting inside a user run, after its first
``EDGE_GUARD_MS``); long silences (no one speaking for ``LONG_SILENCE_MS`` or more,
leading and trailing silence excluded); dead air, the silence the agent owns: a
response gap after a user run of ``DEAD_AIR_RESPONSE_MS`` or more, an in-turn gap
between two agent runs with no user speech of ``DEAD_AIR_IN_TURN_MS`` or more, and a
pre-greeting gap before an agent-first call's first run of ``DEAD_AIR_RESPONSE_MS``
or more.
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from itertools import pairwise

# One recorder timeline.
ONE_TIMELINE_TOLERANCE_MS = 20
# Turn alignment and pairing.
ZERO_RUN_ANCHOR_MS = 50
ANCHOR_SEARCH_MS = 1500
PAIRING_WINDOW_MS = 1000
DISCONTINUITY_STEP_MS = 150
MAX_MEASURABLE_GAP_UNCERTAINTY_MS = 100
USER_SNAP_MS = 500
# Signals.
MIN_OVERLAP_MS = 120
EDGE_GUARD_MS = 100
BACKCHANNEL_MAX_MS = 600
YIELD_WINDOW_MS = 1500
LONG_SILENCE_MS = 2000
DEAD_AIR_RESPONSE_MS = 2000
DEAD_AIR_IN_TURN_MS = 3000

BASIS_ANCHORS_CHECKED = "anchors_checked"
BASIS_CONTINUOUS = "continuous_timeline_assumed"
_NANOS_PER_MS = 1_000_000


@dataclass(frozen=True, slots=True)
class Track:
    """A stored recording as the one-timeline rule reads it."""

    recording_id: str
    # Its speaker per channel: one entry for a mono recording.
    speakers: tuple[str, ...]
    started_at_unix_nano: int
    duration_ms: int | None


def one_timeline_pairs(tracks: Iterable[Track]) -> list[tuple[str, str]]:
    """``(user, agent)`` recording IDs of each mono pair on one timeline (module
    docstring), by start."""

    by_start: dict[int, dict[str, list[Track]]] = {}
    for track in tracks:
        if track.speakers in (("user",), ("agent",)):
            speakers = by_start.setdefault(track.started_at_unix_nano, {})
            speakers.setdefault(track.speakers[0], []).append(track)
    pairs = []
    for start in sorted(by_start):
        users, agents = by_start[start].get("user", []), by_start[start].get("agent", [])
        if len(users) != 1 or len(agents) != 1:
            continue
        user, agent = users[0], agents[0]
        if (
            user.duration_ms is not None
            and agent.duration_ms is not None
            and abs(user.duration_ms - agent.duration_ms) <= ONE_TIMELINE_TOLERANCE_MS
        ):
            pairs.append((user.recording_id, agent.recording_id))
    return pairs


@dataclass(frozen=True, slots=True)
class Run:
    """One refined speech run, with each boundary's sigma (its uncertainty)."""

    start_ms: int
    end_ms: int
    start_sigma_ms: int
    end_sigma_ms: int


@dataclass(frozen=True, slots=True)
class Timeline:
    """One recorder timeline's user and agent speech runs (ms from its first sample)."""

    started_at_unix_nano: int
    duration_ms: int | None
    user_runs: tuple[Run, ...]
    agent_runs: tuple[Run, ...]
    # Exact-zero runs on the agent channel, ``(start_ms, end_ms)``.
    agent_zero_runs: tuple[tuple[int, int], ...]
    # Whether the recorder only inserts time and zero-pads the agent (Pipecat).
    insert_only: bool
    _user_runs_by_start: tuple[Run, ...] = field(init=False, repr=False, compare=False)
    _user_starts: tuple[int, ...] = field(init=False, repr=False, compare=False)
    _agent_runs_by_start: tuple[Run, ...] = field(init=False, repr=False, compare=False)
    _agent_starts: tuple[int, ...] = field(init=False, repr=False, compare=False)
    _agent_zero_edges: tuple[int, ...] = field(init=False, repr=False, compare=False)
    _user_runs_by_end: tuple[Run, ...] = field(init=False, repr=False, compare=False)
    _user_end_keys: tuple[tuple[int, int], ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        user_by_start = tuple(sorted(self.user_runs, key=lambda run: run.start_ms))
        agent_by_start = tuple(sorted(self.agent_runs, key=lambda run: run.start_ms))
        user_by_end = tuple(sorted(self.user_runs, key=lambda run: (run.end_ms, run.start_ms)))
        object.__setattr__(self, "_user_runs_by_start", user_by_start)
        object.__setattr__(self, "_user_starts", tuple(run.start_ms for run in user_by_start))
        object.__setattr__(self, "_agent_runs_by_start", agent_by_start)
        object.__setattr__(self, "_agent_starts", tuple(run.start_ms for run in agent_by_start))
        object.__setattr__(
            self,
            "_agent_zero_edges",
            tuple(
                sorted(
                    end
                    for start, end in self.agent_zero_runs
                    if end - start >= ZERO_RUN_ANCHOR_MS
                    and (self.duration_ms is None or end < self.duration_ms)
                )
            ),
        )
        object.__setattr__(self, "_user_runs_by_end", user_by_end)
        object.__setattr__(
            self,
            "_user_end_keys",
            tuple((run.end_ms, run.start_ms) for run in user_by_end),
        )

    def naive_ms(self, unix_nano: int) -> float:
        return (unix_nano - self.started_at_unix_nano) / _NANOS_PER_MS


@dataclass(frozen=True, slots=True)
class TurnAnchorsMs:
    """A turn's anchors on the span clock (Unix ns), as ``call_span_facts`` reads them."""

    user_on_unix_nano: tuple[int, ...]
    user_off_unix_nano: int | None
    agent_on_unix_nano: int | None


@dataclass(frozen=True, slots=True)
class Gap:
    """The audible gap of one turn on the timeline (the API's ``audible_gap``)."""

    status: str  # measured | talk_over | not_measurable
    value_ms: int | None = None
    uncertainty_ms: int | None = None
    bound_ms: int | None = None
    user_end_ms: int | None = None  # timeline ms
    agent_start_ms: int | None = None  # timeline ms
    reason: str | None = None
    basis: str | None = None
    # The turn's offset (timeline ms minus naive-mapped span ms), for placement.
    offset_ms: float = 0.0
    # The paired agent run, for placing the turn's agent line.
    agent_run: Run | None = None


def not_measurable(reason: str) -> Gap:
    return Gap(status="not_measurable", reason=reason)


def declared_discontinuity(gap: Gap) -> Gap:
    """The gap of a turn whose producer declared its recording timeline not continuous
    across it (module docstring)."""

    return replace(
        gap,
        status="not_measurable",
        value_ms=None,
        uncertainty_ms=None,
        bound_ms=None,
        reason="recorder_timeline_discontinuity",
        basis=None,
        user_end_ms=None,
        agent_start_ms=None,
    )


def _uncertainty(user: Run, agent: Run) -> float:
    return math.sqrt(user.end_sigma_ms**2 + agent.start_sigma_ms**2)


def _nearest(points: Sequence[int], target: float, window: float) -> int | None:
    if not points:
        return None
    position = bisect_left(points, target)
    candidates = points[max(0, position - 1) : min(len(points), position + 1)]
    best = min(candidates, key=lambda point: (abs(point - target), point))
    return None if best is None or abs(best - target) > window else best


def _nearest_run(
    runs: Sequence[Run], starts: Sequence[int], target: float, window: float
) -> Run | None:
    start = _nearest(starts, target, window)
    if start is None:
        return None
    return runs[bisect_left(starts, start)]


def _audio_agent_on(timeline: Timeline, naive_agent_on: float) -> tuple[int | None, bool]:
    """Where the agent's audio starts near a mapped A-on (step 1), and whether a
    zero-run edge anchors it there."""

    if timeline.insert_only:
        edge = _nearest(timeline._agent_zero_edges, naive_agent_on, ANCHOR_SEARCH_MS)
        if edge is not None:
            return edge, True
        return _nearest(timeline._agent_starts, naive_agent_on, ANCHOR_SEARCH_MS), False
    return round(naive_agent_on), False


def _user_on_offsets(timeline: Timeline, anchors: TurnAnchorsMs) -> list[float]:
    offsets = []
    for user_on in anchors.user_on_unix_nano:
        naive = timeline.naive_ms(user_on)
        onset = _nearest(timeline._user_starts, naive, ANCHOR_SEARCH_MS)
        if onset is not None:
            offsets.append(onset - naive)
    return offsets


def audible_gap(timeline: Timeline, anchors: TurnAnchorsMs) -> Gap:
    """One turn's audible gap at the agent (module docstring)."""

    if anchors.agent_on_unix_nano is None:
        return not_measurable("no_agent_audio")
    naive_on = timeline.naive_ms(anchors.agent_on_unix_nano)
    audio_on, anchored = _audio_agent_on(timeline, naive_on)
    if audio_on is None:
        return not_measurable("no_agent_audio")
    agent = _nearest_run(
        timeline._agent_runs_by_start,
        timeline._agent_starts,
        audio_on,
        PAIRING_WINDOW_MS,
    )
    if agent is None:
        return not_measurable("no_agent_audio")
    offsets = _user_on_offsets(timeline, anchors) if timeline.insert_only else []
    o_a = audio_on - naive_on
    if timeline.insert_only:
        offsets.append(o_a)
    latest_user_index = bisect_right(timeline._user_starts, agent.start_ms) - 1
    if (
        latest_user_index >= 0
        and timeline._user_runs_by_start[latest_user_index].start_ms < agent.start_ms
        and timeline._user_runs_by_start[latest_user_index].end_ms > agent.start_ms
    ):
        return Gap(
            status="talk_over",
            agent_start_ms=agent.start_ms,
            offset_ms=statistics.median(offsets) if offsets else 0.0,
            agent_run=agent,
        )
    user_index = bisect_right(timeline._user_end_keys, (agent.start_ms, math.inf)) - 1
    user = timeline._user_runs_by_end[user_index] if user_index >= 0 else None
    agent_after_user = user is not None and bisect_right(
        timeline._agent_starts, user.end_ms
    ) < bisect_left(timeline._agent_starts, agent.start_ms)
    if user is None or agent_after_user:
        return Gap(
            status="not_measurable",
            reason="no_clear_pairing",
            agent_start_ms=agent.start_ms,
            offset_ms=statistics.median(offsets) if offsets else 0.0,
            agent_run=agent,
        )
    value = agent.start_ms - user.end_ms
    uncertainty = _uncertainty(user, agent)
    uncertainty_ms = round(uncertainty)
    basis = BASIS_CONTINUOUS
    if anchored and anchors.user_off_unix_nano is not None:
        basis = BASIS_ANCHORS_CHECKED
        o_u = user.end_ms - timeline.naive_ms(anchors.user_off_unix_nano)
        offsets.append(o_u)
        offset = statistics.median(offsets)
        if abs(o_a - o_u) > DISCONTINUITY_STEP_MS:
            return Gap(
                status="not_measurable",
                uncertainty_ms=uncertainty_ms,
                bound_ms=value,
                user_end_ms=user.end_ms,
                agent_start_ms=agent.start_ms,
                reason="recorder_timeline_discontinuity",
                basis=basis,
                offset_ms=offset,
                agent_run=agent,
            )
    offset = statistics.median(offsets) if offsets else 0.0
    if uncertainty > MAX_MEASURABLE_GAP_UNCERTAINTY_MS:
        return Gap(
            status="not_measurable",
            uncertainty_ms=uncertainty_ms,
            user_end_ms=user.end_ms,
            agent_start_ms=agent.start_ms,
            reason="boundary_uncertainty_too_high",
            basis=basis,
            offset_ms=offset,
            agent_run=agent,
        )
    return Gap(
        status="measured",
        value_ms=value,
        uncertainty_ms=uncertainty_ms,
        user_end_ms=user.end_ms,
        agent_start_ms=agent.start_ms,
        basis=basis,
        offset_ms=offset,
        agent_run=agent,
    )


# --- signals -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Signals:
    talk_time_user_ms: int | None = None
    talk_time_agent_ms: int | None = None
    overlap_ms: int | None = None
    barge_in_count: int | None = None
    talk_over_count: int | None = None
    long_silence_count: int | None = None
    dead_air_ms: int | None = None


def talk_time(runs: Sequence[Run]) -> int:
    return sum(run.end_ms - run.start_ms for run in runs)


def _coalesce(runs: Sequence[Run]) -> tuple[Run, ...]:
    """Sorted maximal speech runs for one speaker, including overlapping inputs."""

    result: list[Run] = []
    for run in sorted(runs, key=lambda item: (item.start_ms, item.end_ms)):
        if not result or run.start_ms > result[-1].end_ms:
            result.append(run)
            continue
        previous = result[-1]
        if run.end_ms > previous.end_ms:
            result[-1] = Run(
                previous.start_ms,
                run.end_ms,
                previous.start_sigma_ms,
                run.end_sigma_ms,
            )
    return tuple(result)


def _speech_groups(user: Sequence[Run], agent: Sequence[Run]) -> list[tuple[int, int, str, bool]]:
    """Merged activity intervals with the last and first speaker at their edges."""

    events = sorted(
        [(run.start_ms, run.end_ms, "user") for run in user]
        + [(run.start_ms, run.end_ms, "agent") for run in agent],
        key=lambda item: (item[0], item[1], 0 if item[2] == "user" else 1),
    )
    groups: list[list[int | str | bool]] = []
    for start, end, speaker in events:
        if not groups or start > groups[-1][1]:
            groups.append([start, end, speaker, speaker == "agent"])
            continue
        group = groups[-1]
        if start == group[0] and speaker == "agent":
            group[3] = True
        if end > group[1]:
            group[1] = end
            group[2] = speaker
        elif end == group[1] and speaker == "user" and group[2] == "agent":
            # ``max(before, key=end)`` used to prefer user runs on an end-time tie.
            group[2] = "user"
    return [
        (int(start), int(end), str(last_speaker), bool(agent_at_start))
        for start, end, last_speaker, agent_at_start in groups
    ]


def _silences(user: Sequence[Run], agent: Sequence[Run]) -> list[tuple[int, int]]:
    """Maximal intervals with no one speaking, between the first and last speech."""

    groups = _speech_groups(user, agent)
    return [(previous[1], following[0]) for previous, following in pairwise(groups)]


def signals(timeline: Timeline) -> Signals:
    """The call's signals on one timeline (module docstring)."""

    user, agent = _coalesce(timeline.user_runs), _coalesce(timeline.agent_runs)
    overlap = 0
    user_index = agent_index = 0
    while user_index < len(user) and agent_index < len(agent):
        u, a = user[user_index], agent[agent_index]
        both = min(u.end_ms, a.end_ms) - max(u.start_ms, a.start_ms)
        if both >= MIN_OVERLAP_MS:
            overlap += both
        if u.end_ms <= a.end_ms:
            user_index += 1
        if a.end_ms <= u.end_ms:
            agent_index += 1

    def backchannel(u: Run, a: Run) -> bool:
        short = u.end_ms - u.start_ms < BACKCHANNEL_MAX_MS
        return short and a.end_ms > u.start_ms + YIELD_WINDOW_MS

    agent_starts = [run.start_ms for run in agent]
    barge_ins = 0
    for u in user:
        index = bisect_right(agent_starts, u.start_ms) - 1
        if index < 0:
            continue
        a = agent[index]
        if a.start_ms + EDGE_GUARD_MS < u.start_ms < a.end_ms and not backchannel(u, a):
            barge_ins += 1

    user_starts = [run.start_ms for run in user]
    talk_overs = 0
    for a in agent:
        index = bisect_right(user_starts, a.start_ms) - 1
        if index >= 0:
            u = user[index]
            if u.start_ms + EDGE_GUARD_MS < a.start_ms < u.end_ms:
                talk_overs += 1

    groups = _speech_groups(user, agent)
    long_silences = sum(
        1
        for previous, following in pairwise(groups)
        if following[0] - previous[1] >= LONG_SILENCE_MS
    )
    dead_air = 0
    for previous, following in pairwise(groups):
        silence = following[0] - previous[1]
        followed_by_agent = following[3]
        response_gap = (
            previous[2] == "user" and followed_by_agent and silence >= DEAD_AIR_RESPONSE_MS
        )
        in_turn_gap = (
            previous[2] == "agent" and followed_by_agent and silence >= DEAD_AIR_IN_TURN_MS
        )
        if response_gap or in_turn_gap:
            dead_air += silence
    first_start = min((run.start_ms for run in (*user, *agent)), default=None)
    first_is_user = first_start is not None and any(run.start_ms == first_start for run in user)
    first_is_agent = first_start is not None and any(run.start_ms == first_start for run in agent)
    if (
        first_start is not None
        and first_is_agent
        and not first_is_user
        and first_start >= DEAD_AIR_RESPONSE_MS
    ):
        dead_air += first_start
    return Signals(
        talk_time_user_ms=talk_time(user),
        talk_time_agent_ms=talk_time(agent),
        overlap_ms=overlap,
        barge_in_count=barge_ins,
        talk_over_count=talk_overs,
        long_silence_count=long_silences,
        dead_air_ms=dead_air,
    )
