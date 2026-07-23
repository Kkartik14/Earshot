"""Process-scoped span routing for framework adapters.

A framework adapter used to add one recorder-bound ``SpanProcessor`` to the
application's shared OpenTelemetry provider per session. With concurrent
sessions on one provider that broadcasts every span into every session's
recorder (cross-session contamination) and leaks a processor + recorder
closure per session (unbounded retention).

This module installs exactly **one** router processor per
``(provider, framework)`` and routes each span to the single owning session
sink. Routing is resolved at ``on_start`` (which, unlike ``on_end``, receives a
context and can read the active session from OpenTelemetry baggage or the
Earshot conversation context), stashed by ``span_id``, and learned per
``trace_id`` so off-thread child spans still route to their session. When
exactly one sink is active the span is delivered unambiguously; when two or
more are active and the span cannot be attributed it is **quarantined** (dropped
with a counter), never broadcast.
"""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from weakref import WeakKeyDictionary

try:  # pragma: no cover - exercised only with the optional otel dependency
    from opentelemetry import baggage as _otel_baggage
    from opentelemetry import context as _otel_context

    _OTEL = True
except ImportError:  # pragma: no cover - optional dependency
    _OTEL = False

# The baggage key a session sets so its framework spans route to its recorder.
EARSHOT_SESSION_BAGGAGE = "earshot.session.id"

# Bound the span_id -> session stash so a long-lived provider cannot grow it
# without limit (spans are removed at on_end, but leaks/late spans are capped).
_MAX_SPAN_STASH = 8192
# Bound the learned trace_id -> session map similarly.
_MAX_TRACE_MAP = 4096

SpanPredicate = Callable[[Any], bool]
SpanConsumer = Callable[[Any], None]
LossConsumer = Callable[[str], None]
SpanIdentity = tuple[int, int]
_AMBIGUOUS_TRACE = object()


def _span_ids(span: Any) -> tuple[int | None, int | None]:
    """Return raw ``(trace_id, span_id)`` ints for a live or readable span."""

    get_ctx = getattr(span, "get_span_context", None)
    ctx = None
    if callable(get_ctx):
        try:
            ctx = get_ctx()
        except Exception:  # pragma: no cover - defensive
            ctx = None
    if ctx is None:
        ctx = getattr(span, "context", None)
    if ctx is None:
        return None, None
    trace_id = getattr(ctx, "trace_id", None)
    span_id = getattr(ctx, "span_id", None)
    trace_id = trace_id if isinstance(trace_id, int) and trace_id != 0 else None
    span_id = span_id if isinstance(span_id, int) and span_id != 0 else None
    return trace_id, span_id


@dataclass(frozen=True, slots=True)
class RoutingStatus:
    """Public, content-free health for one active routing registration."""

    active: bool
    quarantined_span_count: int


@dataclass(frozen=True, slots=True)
class _SpanDecision:
    """Start-time ownership plus the ends still expected for this identity."""

    route_key: str | None
    pending_ends: int = 1


class SpanSink:
    """One session's delivery target: the adapter's ``consume_span`` callback."""

    __slots__ = (
        "_closed",
        "_consume",
        "_lock",
        "_on_loss",
        "_quarantined_span_count",
        "route_key",
        "session_key",
    )

    def __init__(
        self,
        route_key: str,
        session_key: str,
        consume: SpanConsumer,
        on_loss: LossConsumer,
    ) -> None:
        self.route_key = route_key
        self.session_key = session_key
        self._consume: SpanConsumer | None = consume
        self._on_loss: LossConsumer | None = on_loss
        self._closed = False
        self._quarantined_span_count = 0
        self._lock = threading.RLock()

    def consume(self, span: Any) -> bool:
        """Deliver unless closed; close waits for an in-flight delivery."""

        with self._lock:
            if self._closed:
                return False
            consume = self._consume
            if consume is None:  # pragma: no cover - guarded by _closed
                return False
            consume(span)
            return True

    def record_loss(self, reason: str) -> None:
        """Count loss and publish only its stable reason, never span content."""

        with self._lock:
            self._quarantined_span_count += 1
            if self._closed:
                return
            on_loss = self._on_loss
            if on_loss is None:  # pragma: no cover - guarded by _closed
                return
            try:
                on_loss(reason)
            except Exception:  # pragma: no cover - adapter callbacks are also fail-open
                return

    def close(self) -> None:
        with self._lock:
            self._closed = True
            # A retired route needs only its opaque key and loss counter. Bound
            # callbacks retain the adapter/recorder (and incident content), so
            # sever them before the router keeps this sink for late-span safety.
            self._consume = None
            self._on_loss = None
            self.session_key = ""

    def _reset_lock_after_fork(self) -> None:
        """Make inherited status safe without touching a possibly held lock."""

        self._lock = threading.RLock()
        self.close()

    @property
    def status(self) -> RoutingStatus:
        with self._lock:
            return RoutingStatus(
                active=not self._closed,
                quarantined_span_count=self._quarantined_span_count,
            )


class SpanRouter:
    """Routes framework spans on one provider to the owning session sink."""

    def __init__(self, predicate: SpanPredicate) -> None:
        self._predicate = predicate
        self._sinks: dict[str, SpanSink] = {}
        self._retired_sinks: dict[str, SpanSink] = {}
        self._span_to_session: OrderedDict[SpanIdentity, _SpanDecision] = OrderedDict()
        self._trace_to_session: OrderedDict[int, str | object] = OrderedDict()
        self._allow_learned_end_fallback = True
        self._allow_unscoped_fallback = True
        self._quarantined = 0
        self._stale = False
        self._lock = threading.RLock()

    # -- registration -----------------------------------------------------
    def register(self, sink: SpanSink) -> None:
        with self._lock:
            self._sinks[sink.route_key] = sink

    def deregister(self, sink: SpanSink) -> None:
        with self._lock:
            if self._sinks.get(sink.route_key) is not sink:
                return
            # Once registrations turn over, an unknown trace may be a late child
            # of the retired session. It must never be assigned to a new sole
            # sink merely because the provider currently has one registration.
            self._allow_unscoped_fallback = False
            self._sinks.pop(sink.route_key, None)
            if self._route_has_inflight_span_locked(sink.route_key):
                # Keep only content-free routing identity until every span that
                # started before detach ends. This prevents the next sole sink
                # from receiving a late span owned by the retired registration.
                self._retired_sinks[sink.route_key] = sink
            else:
                self._discard_retired_route_locked(sink.route_key)

    @property
    def sink_count(self) -> int:
        with self._lock:
            return len(self._sinks)

    @property
    def quarantined(self) -> int:
        with self._lock:
            return self._quarantined

    @property
    def routing_state_size(self) -> int:
        with self._lock:
            return (
                len(self._span_to_session) + len(self._trace_to_session) + len(self._retired_sinks)
            )

    # -- processor callbacks ---------------------------------------------
    def on_start(self, span: Any, parent_context: Any | None) -> None:
        if self._stale or not self._predicate(span):
            return
        trace_id, span_id = _span_ids(span)
        routing_hint = self._resolve_session(parent_context)
        with self._lock:
            if routing_hint is not None:
                route_key = self._route_key_for_hint_locked(routing_hint)
            else:
                route_key = self._route_for_unscoped_start_locked(trace_id)
            identity_collision = False
            if trace_id is not None and span_id is not None:
                span_identity = (trace_id, span_id)
                existing = self._span_to_session.get(span_identity)
                if existing is None:
                    decision = _SpanDecision(route_key)
                else:
                    # The SDK promises uniqueness, but custom/broken ID generators
                    # exist. Once identities collide, neither end can be assigned
                    # safely and both callbacks must consume the same tombstone.
                    decision = _SpanDecision(None, existing.pending_ends + 1)
                    identity_collision = True
                    self._trace_to_session[trace_id] = _AMBIGUOUS_TRACE
                    self._trace_to_session.move_to_end(trace_id)
                    self._trim_trace_map_locked()
                self._span_to_session[span_identity] = decision
                self._span_to_session.move_to_end(span_identity)
                while len(self._span_to_session) > _MAX_SPAN_STASH:
                    _, evicted = self._span_to_session.popitem(last=False)
                    # Once any exact start decision is lost, an end callback
                    # cannot distinguish that span from one observed end-only.
                    # Disable the weaker trace fallback process-wide rather
                    # than risk reassigning an evicted span to a later owner.
                    self._allow_learned_end_fallback = False
                    if evicted.route_key is not None and evicted.route_key not in self._sinks:
                        self._discard_retired_route_locked(evicted.route_key)
            if route_key is None or identity_collision:
                return
            if trace_id is not None:
                if trace_id not in self._trace_to_session:
                    self._trace_to_session[trace_id] = route_key
                elif self._trace_to_session[trace_id] != route_key:
                    # A trace observed in more than one session is not safe for
                    # hint-free child attribution. Exact span ownership still
                    # routes correctly; any missing exact entry fails closed.
                    self._trace_to_session[trace_id] = _AMBIGUOUS_TRACE
                self._trace_to_session.move_to_end(trace_id)
                self._trim_trace_map_locked()

    def on_end(self, span: Any) -> None:
        if self._stale or not self._predicate(span):
            return
        trace_id, span_id = _span_ids(span)
        with self._lock:
            route_key: str | None = None
            exact_unattributable = False
            if trace_id is not None and span_id is not None:
                span_identity = (trace_id, span_id)
                decision = self._span_to_session.get(span_identity)
                if decision is not None:
                    exact_unattributable = decision.route_key is None
                    route_key = decision.route_key
                    if decision.pending_ends > 1:
                        self._span_to_session[span_identity] = _SpanDecision(
                            decision.route_key,
                            decision.pending_ends - 1,
                        )
                        self._span_to_session.move_to_end(span_identity)
                    else:
                        self._span_to_session.pop(span_identity)
            if (
                self._allow_learned_end_fallback
                and not exact_unattributable
                and route_key is None
                and trace_id is not None
            ):
                learned_route = self._trace_to_session.get(trace_id)
                route_key = learned_route if isinstance(learned_route, str) else None
            sink = self._sinks.get(route_key) if route_key is not None else None
            retired_sink = self._retired_sinks.get(route_key) if route_key is not None else None
            if sink is None:
                self._quarantined += 1
                affected_sinks = (
                    (retired_sink,)
                    if retired_sink is not None
                    else tuple(self._sinks.values())
                    if route_key is None
                    else ()
                )
            else:
                affected_sinks = ()
            if route_key is not None and route_key not in self._sinks:
                self._discard_retired_route_locked(route_key)
        if sink is None:
            for affected_sink in affected_sinks:
                reason = (
                    "routing_target_closed"
                    if affected_sink is retired_sink
                    else "unattributed_span_quarantined"
                )
                affected_sink.record_loss(reason)
            return
        # Deliver outside the lock; the adapter guards its own recorder.
        if not sink.consume(span):
            with self._lock:
                self._quarantined += 1
            sink.record_loss("routing_target_closed")

    # -- helpers ----------------------------------------------------------
    def _resolve_session(self, parent_context: Any | None) -> str | None:
        if _OTEL:
            try:
                value = _otel_baggage.get_baggage(EARSHOT_SESSION_BAGGAGE, parent_context)
            except Exception:  # pragma: no cover - defensive
                value = None
            if value:
                return str(value)
        # Fall back to the Earshot conversation context for callers that opened
        # an ``earshot.conversation()`` scope on the span-starting task.
        from ..context import current_conversation

        conversation = current_conversation()
        return str(conversation) if conversation else None

    def _route_key_for_hint_locked(self, routing_hint: str) -> str | None:
        """Resolve an opaque scope key, or an unambiguous legacy session ID."""

        if routing_hint in self._sinks or routing_hint in self._retired_sinks:
            return routing_hint
        if not self._allow_unscoped_fallback:
            # A public session ID is not a generation-scoped routing capability.
            # After registration turnover, the same ID may name a new session.
            return None
        matches = [
            route_key for route_key, sink in self._sinks.items() if sink.session_key == routing_hint
        ]
        return matches[0] if len(matches) == 1 else None

    def _route_for_unscoped_start_locked(self, trace_id: int | None) -> str | None:
        """Resolve ownership at span start; never infer it from end-time state."""

        if trace_id is not None and trace_id in self._trace_to_session:
            learned_route = self._trace_to_session[trace_id]
            if isinstance(learned_route, str) and (
                learned_route in self._sinks or learned_route in self._retired_sinks
            ):
                return learned_route
            return None
        if self._allow_unscoped_fallback and len(self._sinks) == 1:
            return next(iter(self._sinks))
        return None

    def _route_has_inflight_span_locked(self, route_key: str) -> bool:
        return any(decision.route_key == route_key for decision in self._span_to_session.values())

    def _trim_trace_map_locked(self) -> None:
        while len(self._trace_to_session) > _MAX_TRACE_MAP:
            self._trace_to_session.popitem(last=False)

    def _discard_retired_route_locked(self, route_key: str) -> None:
        """Release a retired sink only after no exact late span can reference it."""

        if self._route_has_inflight_span_locked(route_key):
            return
        self._retired_sinks.pop(route_key, None)
        self._trace_to_session = OrderedDict(
            (trace, key) for trace, key in self._trace_to_session.items() if key != route_key
        )
        if not self._sinks and not self._retired_sinks:
            self._trace_to_session.clear()

    def _mark_stale(self) -> None:
        self._stale = True
        with self._lock:
            for sink in (*self._sinks.values(), *self._retired_sinks.values()):
                sink.close()
            self._sinks.clear()
            self._retired_sinks.clear()
            self._span_to_session.clear()
            self._trace_to_session.clear()

    def _mark_stale_after_fork(self) -> None:
        """Reset inherited locks/state without acquiring a pre-fork lock."""

        self._lock = threading.RLock()
        self._stale = True
        for sink in (*self._sinks.values(), *self._retired_sinks.values()):
            sink._reset_lock_after_fork()
        self._sinks.clear()
        self._retired_sinks.clear()
        self._span_to_session.clear()
        self._trace_to_session.clear()


# --- process-scoped registry ---------------------------------------------
_routers: WeakKeyDictionary[Any, dict[str, SpanRouter]] = WeakKeyDictionary()
_registry_lock = threading.RLock()


def _install_processor(provider: Any, router: SpanRouter) -> None:
    from opentelemetry.sdk.trace import ReadableSpan, Span  # noqa: F401
    from opentelemetry.sdk.trace.export import SpanProcessor

    class _EarshotRouterProcessor(SpanProcessor):
        def on_start(self, span: Any, parent_context: Any | None = None) -> None:
            router.on_start(span, parent_context)

        def on_end(self, span: Any) -> None:
            router.on_end(span)

        def shutdown(self) -> None:
            return None

        def force_flush(self, timeout_millis: int = 30_000) -> bool:
            del timeout_millis
            return True

    add = getattr(provider, "add_span_processor", None)
    if not callable(add):
        raise TypeError("tracer provider does not support span processors")
    add(_EarshotRouterProcessor())


def get_router(provider: Any, framework: str, predicate: SpanPredicate) -> SpanRouter:
    """Return the single router for ``(provider, framework)``, installing once."""

    with _registry_lock:
        per_framework = _routers.get(provider)
        if per_framework is None:
            per_framework = {}
            _routers[provider] = per_framework
        router = per_framework.get(framework)
        if router is None or router._stale:
            router = SpanRouter(predicate)
            _install_processor(provider, router)
            per_framework[framework] = router
        return router


def _reset_after_fork() -> None:
    # A lock held by a vanished thread can never be acquired in the child.
    # Replace every inherited lock before touching the associated state.
    global _registry_lock
    _registry_lock = threading.RLock()
    for per_framework in list(_routers.values()):
        for router in list(per_framework.values()):
            router._mark_stale_after_fork()
    _routers.clear()


import os  # noqa: E402

if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


class RoutingHandle:
    """Deactivation + session-activation handle returned by ``attach``.

    Quacks like a ``SpanProcessor`` (``shutdown``/``force_flush``) so legacy
    callers that treated the return value as a processor keep working.
    """

    __slots__ = ("_router", "_session_key", "_sink")

    def __init__(self, router: SpanRouter, sink: SpanSink) -> None:
        self._router = router
        self._sink = sink
        self._session_key = sink.session_key

    @property
    def session_key(self) -> str:
        return self._session_key

    def close(self) -> None:
        """Remove this session's routing state from the shared provider."""

        self._sink.close()
        self._router.deregister(self._sink)

    @property
    def status(self) -> RoutingStatus:
        """Return content-free loss and activation status for this session."""

        return self._sink.status

    @contextmanager
    def session_scope(self):
        """Tag spans started in this scope so they route to this session.

        Always use this scope when a provider can host concurrent or sequential
        sessions. Only the provider's first, uninterrupted lone registration
        can safely route unscoped spans; after registration turnover, unknown
        traces are quarantined so late children cannot reach a new session.
        """

        if not _OTEL:
            yield
            return
        ctx = _otel_baggage.set_baggage(EARSHOT_SESSION_BAGGAGE, self._sink.route_key)
        token = _otel_context.attach(ctx)
        try:
            yield
        finally:
            _otel_context.detach(token)

    # -- legacy SpanProcessor compatibility -------------------------------
    def shutdown(self) -> None:
        self.close()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        del timeout_millis
        return True


def attach_adapter(
    provider: Any,
    framework: str,
    predicate: SpanPredicate,
    session_key: str,
    consume: SpanConsumer,
    on_loss: LossConsumer,
) -> RoutingHandle:
    """Register an adapter's recorder as a session sink on the shared router."""

    # Validate before the weak-keyed registry touches the provider so an invalid
    # provider raises a clear error (and never a "cannot weakref" surprise).
    if not callable(getattr(provider, "add_span_processor", None)):
        raise TypeError("tracer provider does not support span processors")
    router = get_router(provider, framework, predicate)
    sink = SpanSink(uuid.uuid4().hex, session_key, consume, on_loss)
    router.register(sink)
    return RoutingHandle(router, sink)
