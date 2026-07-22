"""Task-local ownership and instrumentation suppression for the Earshot SDK."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from types import TracebackType


@dataclass(frozen=True)
class ContextSnapshot:
    client_id: str | None
    project_id: str | None
    conversation_id: str | None
    operation_id: str | None
    # OTel identity of the active manual operation, so a nested operation can
    # adopt it as its parent span (within the same trace).
    operation_span_id: str | None = None
    operation_trace_id: str | None = None


_CURRENT: ContextVar[ContextSnapshot | None] = ContextVar("earshot_context", default=None)
_SUPPRESSION_DEPTH: ContextVar[int] = ContextVar("earshot_suppression_depth", default=0)


def current_context() -> ContextSnapshot | None:
    return _CURRENT.get()


def current_conversation() -> str | None:
    context = current_context()
    return None if context is None else context.conversation_id


def current_operation() -> str | None:
    context = current_context()
    return None if context is None else context.operation_id


def current_operation_span() -> tuple[str | None, str | None]:
    """Return the active manual operation's ``(span_id, trace_id)``, if any."""

    context = current_context()
    if context is None:
        return None, None
    return context.operation_span_id, context.operation_trace_id


def is_instrumentation_suppressed() -> bool:
    return _SUPPRESSION_DEPTH.get() > 0


class _ContextScope:
    def __init__(self, context: ContextSnapshot) -> None:
        self._context = context
        self._token: Token[ContextSnapshot | None] | None = None

    def __enter__(self) -> ContextSnapshot:
        if self._token is not None:
            raise RuntimeError("Earshot context scope cannot be entered twice")
        self._token = _CURRENT.set(self._context)
        return self._context

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        token = self._token
        self._token = None
        if token is not None:
            _CURRENT.reset(token)

    async def __aenter__(self) -> ContextSnapshot:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.__exit__(exc_type, exc_value, traceback)


class _SuppressionScope:
    def __init__(self) -> None:
        self._token: Token[int] | None = None

    def __enter__(self) -> None:
        if self._token is not None:
            raise RuntimeError("Earshot suppression scope cannot be entered twice")
        self._token = _SUPPRESSION_DEPTH.set(_SUPPRESSION_DEPTH.get() + 1)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        token = self._token
        self._token = None
        if token is not None:
            _SUPPRESSION_DEPTH.reset(token)

    async def __aenter__(self) -> None:
        self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.__exit__(exc_type, exc_value, traceback)


def suppress_instrumentation() -> _SuppressionScope:
    return _SuppressionScope()


def _conversation_scope(*, client_id: str, project_id: str, conversation_id: str) -> _ContextScope:
    return _ContextScope(
        ContextSnapshot(
            client_id=client_id,
            project_id=project_id,
            conversation_id=conversation_id,
            operation_id=None,
        )
    )


def _operation_scope(
    operation_id: str,
    *,
    span_id: str | None = None,
    trace_id: str | None = None,
) -> _ContextScope:
    current = current_context()
    if current is None:
        current = ContextSnapshot(
            client_id=None,
            project_id=None,
            conversation_id=None,
            operation_id=operation_id,
            operation_span_id=span_id,
            operation_trace_id=trace_id,
        )
    else:
        current = replace(
            current,
            operation_id=operation_id,
            operation_span_id=span_id,
            operation_trace_id=trace_id,
        )
    return _ContextScope(current)
