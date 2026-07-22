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
from collections import OrderedDict
from collections.abc import Callable
from contextlib import contextmanager
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


class SpanSink:
    """One session's delivery target: the adapter's ``consume_span`` callback."""

    __slots__ = ("_consume", "closed", "session_key")

    def __init__(self, session_key: str, consume: SpanConsumer) -> None:
        self.session_key = session_key
        self._consume = consume
        self.closed = False

    def consume(self, span: Any) -> None:
        if not self.closed:
            self._consume(span)


class SpanRouter:
    """Routes framework spans on one provider to the owning session sink."""

    def __init__(self, predicate: SpanPredicate) -> None:
        self._predicate = predicate
        self._sinks: dict[str, SpanSink] = {}
        self._span_to_session: OrderedDict[int, str] = OrderedDict()
        self._trace_to_session: OrderedDict[int, str] = OrderedDict()
        self._quarantined = 0
        self._stale = False
        self._lock = threading.RLock()

    # -- registration -----------------------------------------------------
    def register(self, sink: SpanSink) -> None:
        with self._lock:
            self._sinks[sink.session_key] = sink

    def deregister(self, session_key: str) -> None:
        with self._lock:
            self._sinks.pop(session_key, None)
            self._trace_to_session = OrderedDict(
                (trace, key) for trace, key in self._trace_to_session.items() if key != session_key
            )
            self._span_to_session = OrderedDict(
                (span, key) for span, key in self._span_to_session.items() if key != session_key
            )

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
            return len(self._span_to_session) + len(self._trace_to_session)

    # -- processor callbacks ---------------------------------------------
    def on_start(self, span: Any, parent_context: Any | None) -> None:
        if self._stale or not self._predicate(span):
            return
        session_key = self._resolve_session(parent_context)
        if session_key is None:
            return
        trace_id, span_id = _span_ids(span)
        with self._lock:
            if session_key not in self._sinks:
                return
            if span_id is not None:
                self._span_to_session[span_id] = session_key
                self._span_to_session.move_to_end(span_id)
                while len(self._span_to_session) > _MAX_SPAN_STASH:
                    self._span_to_session.popitem(last=False)
            if trace_id is not None and trace_id not in self._trace_to_session:
                self._trace_to_session[trace_id] = session_key
                while len(self._trace_to_session) > _MAX_TRACE_MAP:
                    self._trace_to_session.popitem(last=False)

    def on_end(self, span: Any) -> None:
        if self._stale or not self._predicate(span):
            return
        trace_id, span_id = _span_ids(span)
        with self._lock:
            session_key: str | None = None
            if span_id is not None:
                session_key = self._span_to_session.pop(span_id, None)
            if session_key is None and trace_id is not None:
                session_key = self._trace_to_session.get(trace_id)
            if session_key is None and len(self._sinks) == 1:
                # Unambiguous single session: no attribution needed.
                session_key = next(iter(self._sinks))
            sink = self._sinks.get(session_key) if session_key is not None else None
            if sink is None or sink.closed:
                self._quarantined += 1
                return
        # Deliver outside the lock; the adapter guards its own recorder.
        sink.consume(span)

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

    def _mark_stale(self) -> None:
        self._stale = True
        with self._lock:
            self._sinks.clear()
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
    with _registry_lock:
        for per_framework in list(_routers.values()):
            for router in list(per_framework.values()):
                router._mark_stale()
        _routers.clear()


import os  # noqa: E402

os.register_at_fork(after_in_child=_reset_after_fork)


class RoutingHandle:
    """Deactivation + session-activation handle returned by ``attach``.

    Quacks like a ``SpanProcessor`` (``shutdown``/``force_flush``) so legacy
    callers that treated the return value as a processor keep working.
    """

    __slots__ = ("_router", "_sink")

    def __init__(self, router: SpanRouter, sink: SpanSink) -> None:
        self._router = router
        self._sink = sink

    @property
    def session_key(self) -> str:
        return self._sink.session_key

    def close(self) -> None:
        """Remove this session's routing state from the shared provider."""

        self._sink.closed = True
        self._router.deregister(self._sink.session_key)

    @contextmanager
    def session_scope(self):
        """Tag spans started in this scope so they route to this session.

        Required only when multiple sessions share one provider concurrently;
        a lone session routes without it.
        """

        if not _OTEL:
            yield
            return
        ctx = _otel_baggage.set_baggage(EARSHOT_SESSION_BAGGAGE, self._sink.session_key)
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
) -> RoutingHandle:
    """Register an adapter's recorder as a session sink on the shared router."""

    # Validate before the weak-keyed registry touches the provider so an invalid
    # provider raises a clear error (and never a "cannot weakref" surprise).
    if not callable(getattr(provider, "add_span_processor", None)):
        raise TypeError("tracer provider does not support span processors")
    router = get_router(provider, framework, predicate)
    sink = SpanSink(session_key, consume)
    router.register(sink)
    return RoutingHandle(router, sink)
