"""A call's recorder timeline: the audible gap at the agent, signals, and placement (pure).

Everything here reads speech runs on **one recorder timeline**: the two channels of
one stereo recording that maps user and agent, or a user and an agent mono
recording with the same ``started_at`` and durations within 20 ms (the analysis
job's ``ONE_TIMELINE_TOLERANCE_MS``). Times are integer milliseconds from the
timeline's first sample. Span times reach it by the naive mapping
``(t - started_at) / 1e6``, corrected per turn where the framework gives anchors.

**Audible gap at the agent** (one per framework turn, ``audible_gap``):

1. *Agent on in the audio.* Pipecat's recorder pads the agent lane with digital
   zero while the bot is quiet and only ever inserts time, so the agent's audio
   starts at the right edge of an exact-zero run (``ZERO_RUN_ANCHOR_MS`` or longer): the
   edge nearest the naive-mapped A-on anchor within ``ANCHOR_SEARCH_MS``, else the
   onset of the nearest agent run. LiveKit's recorder places frames by wall clock,
   so its A-on maps naively (``continuous_timeline_assumed``).
2. *Pairing.* ``a`` is the agent run whose start is nearest that point, within
   ``PAIRING_WINDOW_MS``; none: ``no_agent_audio``. If a user run is active when
   ``a`` starts, the turn is ``talk_over``. Otherwise ``u*`` is the last user run
   ending by ``a.start`` with no agent run starting in between; none:
   ``no_clear_pairing``.
3. *Value.* ``a.start - u*.end`` ± ``sqrt(sigma_u^2 + sigma_a^2)`` (the boundaries' sigmas).
4. *Discontinuity* (Pipecat, which has both anchors): ``o_u = u*.end - naive(U-off)``
   and ``o_a = audio A-on - naive(A-on)``. A step ``|o_a - o_u|`` above
   ``DISCONTINUITY_STEP_MS`` means the recorder inserted time inside the gap: the
   turn is ``not_measurable / recorder_timeline_discontinuity`` and its raw gap is
   an upper bound (an insert-only recorder can only lengthen a gap). The step is
   never subtracted. A Pipecat turn without both anchors is measured on the
   ``continuous_timeline_assumed`` basis.

A value is never fabricated: every other case is ``not_measurable`` with its reason.

**Signals** (the research's §4.3 definitions; cross-channel ones need one timeline):
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
from collections.abc import Sequence
from dataclasses import dataclass

# Turn alignment and pairing (M1, M6).
ZERO_RUN_ANCHOR_MS = 50
ANCHOR_SEARCH_MS = 1500
PAIRING_WINDOW_MS = 1000
DISCONTINUITY_STEP_MS = 150
USER_SNAP_MS = 500
# Signals (research §4.3; M22).
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


def _uncertainty(user: Run, agent: Run) -> int:
    return round(math.sqrt(user.end_sigma_ms**2 + agent.start_sigma_ms**2))


def _nearest(points: Sequence[int], target: float, window: float) -> int | None:
    best = min(points, key=lambda point: (abs(point - target), point), default=None)
    return None if best is None or abs(best - target) > window else best


def _audio_agent_on(timeline: Timeline, naive_agent_on: float) -> int | None:
    """Where the agent's audio starts near a mapped A-on (step 1)."""

    if timeline.insert_only:
        edges = [
            end
            for start, end in timeline.agent_zero_runs
            if end - start >= ZERO_RUN_ANCHOR_MS
            and (timeline.duration_ms is None or end < timeline.duration_ms)
        ]
        edge = _nearest(edges, naive_agent_on, ANCHOR_SEARCH_MS)
        if edge is not None:
            return edge
        return _nearest(
            [run.start_ms for run in timeline.agent_runs], naive_agent_on, ANCHOR_SEARCH_MS
        )
    return round(naive_agent_on)


def _user_on_offsets(timeline: Timeline, anchors: TurnAnchorsMs) -> list[float]:
    onsets = [run.start_ms for run in timeline.user_runs]
    offsets = []
    for user_on in anchors.user_on_unix_nano:
        naive = timeline.naive_ms(user_on)
        onset = _nearest(onsets, naive, ANCHOR_SEARCH_MS)
        if onset is not None:
            offsets.append(onset - naive)
    return offsets


def audible_gap(timeline: Timeline, anchors: TurnAnchorsMs) -> Gap:
    """One turn's audible gap at the agent (module docstring)."""

    if anchors.agent_on_unix_nano is None:
        return not_measurable("no_agent_audio")
    naive_on = timeline.naive_ms(anchors.agent_on_unix_nano)
    audio_on = _audio_agent_on(timeline, naive_on)
    if audio_on is None:
        return not_measurable("no_agent_audio")
    candidates = [
        run for run in timeline.agent_runs if abs(run.start_ms - audio_on) <= PAIRING_WINDOW_MS
    ]
    if not candidates:
        return not_measurable("no_agent_audio")
    agent = min(candidates, key=lambda run: (abs(run.start_ms - audio_on), run.start_ms))
    offsets = _user_on_offsets(timeline, anchors) if timeline.insert_only else []
    o_a = audio_on - naive_on
    if timeline.insert_only:
        offsets.append(o_a)
    if any(run.start_ms < agent.start_ms < run.end_ms for run in timeline.user_runs):
        return Gap(
            status="talk_over",
            agent_start_ms=agent.start_ms,
            offset_ms=statistics.median(offsets) if offsets else 0.0,
            agent_run=agent,
        )
    ended = [run for run in timeline.user_runs if run.end_ms <= agent.start_ms]
    user = max(ended, key=lambda run: (run.end_ms, run.start_ms), default=None)
    if user is None or any(
        user.end_ms < run.start_ms < agent.start_ms for run in timeline.agent_runs
    ):
        return Gap(
            status="not_measurable",
            reason="no_clear_pairing",
            agent_start_ms=agent.start_ms,
            offset_ms=statistics.median(offsets) if offsets else 0.0,
            agent_run=agent,
        )
    value = agent.start_ms - user.end_ms
    uncertainty = _uncertainty(user, agent)
    basis = BASIS_CONTINUOUS
    if timeline.insert_only and anchors.user_off_unix_nano is not None:
        basis = BASIS_ANCHORS_CHECKED
        o_u = user.end_ms - timeline.naive_ms(anchors.user_off_unix_nano)
        offsets.append(o_u)
        offset = statistics.median(offsets)
        if abs(o_a - o_u) > DISCONTINUITY_STEP_MS:
            return Gap(
                status="not_measurable",
                uncertainty_ms=uncertainty,
                bound_ms=value,
                user_end_ms=user.end_ms,
                agent_start_ms=agent.start_ms,
                reason="recorder_timeline_discontinuity",
                basis=basis,
                offset_ms=offset,
                agent_run=agent,
            )
    offset = statistics.median(offsets) if offsets else 0.0
    return Gap(
        status="measured",
        value_ms=value,
        uncertainty_ms=uncertainty,
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


def _silences(user: Sequence[Run], agent: Sequence[Run]) -> list[tuple[int, int]]:
    """Maximal intervals with no one speaking, between the first and last speech."""

    spans = sorted((run.start_ms, run.end_ms) for run in (*user, *agent))
    silences = []
    reach = None
    for start, end in spans:
        if reach is not None and start > reach:
            silences.append((reach, start))
        reach = end if reach is None else max(reach, end)
    return silences


def signals(timeline: Timeline) -> Signals:
    """The call's signals on one timeline (module docstring)."""

    user, agent = timeline.user_runs, timeline.agent_runs
    overlap = 0
    for u in user:
        for a in agent:
            both = min(u.end_ms, a.end_ms) - max(u.start_ms, a.start_ms)
            if both >= MIN_OVERLAP_MS:
                overlap += both

    def backchannel(u: Run, a: Run) -> bool:
        short = u.end_ms - u.start_ms < BACKCHANNEL_MAX_MS
        return short and a.end_ms > u.start_ms + YIELD_WINDOW_MS

    barge_ins = sum(
        1
        for u in user
        if any(
            a.start_ms + EDGE_GUARD_MS < u.start_ms < a.end_ms and not backchannel(u, a)
            for a in agent
        )
    )
    talk_overs = sum(
        1 for a in agent if any(u.start_ms + EDGE_GUARD_MS < a.start_ms < u.end_ms for u in user)
    )
    silences = _silences(user, agent)
    long_silences = sum(1 for start, end in silences if end - start >= LONG_SILENCE_MS)
    dead_air = 0
    for start, end in silences:
        before = [run for run in (*user, *agent) if run.end_ms <= start]
        after = [run for run in (*user, *agent) if run.start_ms >= end]
        if not before or not after:
            continue
        last = max(before, key=lambda run: run.end_ms)
        followed_by_agent = any(run.start_ms == end for run in agent)
        response_gap = last in user and end - start >= DEAD_AIR_RESPONSE_MS
        in_turn_gap = last in agent and followed_by_agent and end - start >= DEAD_AIR_IN_TURN_MS
        if response_gap or in_turn_gap:
            dead_air += end - start
    first = min((*user, *agent), key=lambda run: run.start_ms, default=None)
    if first is not None and first in agent and first.start_ms >= DEAD_AIR_RESPONSE_MS:
        dead_air += first.start_ms
    return Signals(
        talk_time_user_ms=talk_time(user),
        talk_time_agent_ms=talk_time(agent),
        overlap_ms=overlap,
        barge_in_count=barge_ins,
        talk_over_count=talk_overs,
        long_silence_count=long_silences,
        dead_air_ms=dead_air,
    )
