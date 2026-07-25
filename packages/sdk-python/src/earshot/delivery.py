"""Named, pluggable incident delivery strategies.

A *delivery strategy* is a transport-consuming sink: it takes already-sanitized
incident bytes (an :class:`~earshot.exporter.ExportItem`) and gets them to their
destination -- immediately on the caller thread (``sync``), on a bounded
background queue (``async``), or via a durable on-disk spool (``durable``). The
:class:`~earshot.exporter.BoundedAsyncExporter`,
:class:`~earshot.exporter.SynchronousExporter`, and
:class:`~earshot.exporter.DurableExporter` implementations already share one shape
-- ``submit`` / ``flush`` / ``shutdown`` / ``status`` -- and the SDK client's
export router already drives whichever one is active behind a stable target. What
was missing is a *name*: the three built-ins were reachable only through the
client's hard-wired ``delivery_mode`` switch, so every new delivery behaviour was
a change inside the client.

This module is the delivery half of the same symmetry
:mod:`earshot.exporters.registry` gave projections. A projection is
``bundle -> Mapping`` (what document a backend understands); a delivery is
``ExportItem -> sink`` (how the bytes get there). Both are selected by name;
neither is a special-cased island. A host that has its own delivery behaviour
registers a factory here and selects it by ``delivery_mode`` the same first-class
way a user registers an exporter and selects it by ``format``.

Two properties keep the seam trustworthy, matching the projection registry:

* **Inert at import.** Registering a factory fills a dict and nothing else: no
  transport is constructed, no endpoint contacted, no spool touched, and the order
  of registration cannot change what any delivery produces. A factory is called
  only when the client reconfigures.
* **No silent replacement.** A duplicate name is an error unless ``replace`` is
  set, so which strategy a ``delivery_mode`` selects cannot depend on import order.

The built-in ``async`` / ``sync`` / ``durable`` factories are registered by the
SDK client module rather than here, because they must honour the client's own
swappable transport and exporter bindings (the delivery-mode tests substitute
``sdk.HttpExportTransport`` / ``sdk.BoundedAsyncExporter``). This module owns the
registry mechanism; the client owns its built-in strategies.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .exporter import ExportDiagnostic, ExporterStatus, ExportItem, ExportTransport


class DeliverySink(Protocol):
    """A transport-consuming sink the client's export router drives.

    ``BoundedAsyncExporter`` (async), ``SynchronousExporter`` (sync), and
    ``DurableExporter`` (durable) satisfy this structurally today; a host's own
    delivery satisfies it by implementing the same four verbs. ``submit`` returns
    whether the incident was accepted for delivery; ``flush`` and ``shutdown``
    return whether they completed within the deadline; ``status`` is the exporter's
    observable delivery counters.
    """

    def submit(self, item: ExportItem) -> bool: ...

    def flush(self, timeout: float | None = None) -> bool: ...

    def shutdown(self, timeout: float = 5.0) -> bool: ...

    def status(self) -> ExporterStatus: ...


@dataclass(frozen=True)
class DeliveryContext:
    """Everything a delivery factory needs to build a sink for one route.

    The SDK client resolves these from its :class:`~earshot.sdk.SdkConfig` and hands
    them to the selected factory, so the factory stays free of config parsing and a
    host's own factory receives the same resolved inputs the built-ins do. A
    factory reads only the fields its strategy needs and ignores the rest (an async
    delivery ignores ``spool_dir``; a sync delivery ignores ``queue_capacity``).
    """

    transport: ExportTransport
    queue_capacity: int = 128
    max_queue_bytes: int = 16 * 1024 * 1024
    sync_deadline_seconds: float = 10.0
    spool_dir: Path | None = None
    destination_fingerprint: str | None = None
    max_spool_items: int = 1024
    max_spool_bytes: int = 256 * 1024 * 1024
    permanent_rejection_policy: str = "retain"
    diagnostic: Callable[[ExportDiagnostic], None] | None = None


class DeliveryFactory(Protocol):
    """Builds one delivery sink from a resolved :class:`DeliveryContext`.

    A plain function satisfies this; so does any callable object. It is called once
    per client reconfiguration, never at import.
    """

    def __call__(self, context: DeliveryContext) -> DeliverySink: ...


@dataclass(frozen=True, slots=True)
class RegisteredDelivery:
    """One delivery factory under its registered ``delivery_mode`` name."""

    name: str
    factory: DeliveryFactory


class DeliveryRegistry:
    """A mutable set of named delivery factories, safe to share across threads.

    Instantiate one to keep a private set (tests, embedding hosts); the process
    default is :func:`default_delivery_registry`, which is what the SDK client
    selects a ``delivery_mode`` from.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._factories: dict[str, RegisteredDelivery] = {}

    def register(
        self,
        name: str,
        factory: DeliveryFactory,
        *,
        replace: bool = False,
    ) -> RegisteredDelivery:
        """Register ``factory`` under ``name``; return the registration.

        A duplicate name is an error unless ``replace`` is set: silent replacement
        would make the strategy a ``delivery_mode`` selects depend on import order.
        """

        registration = RegisteredDelivery(
            name=_validated_name(name),
            factory=_validated_factory(factory),
        )
        with self._lock:
            if not replace and registration.name in self._factories:
                raise ValueError("a delivery mode is already registered under that name")
            self._factories[registration.name] = registration
        return registration

    def unregister(self, name: str) -> bool:
        """Remove ``name``; return whether anything was removed."""

        with self._lock:
            return self._factories.pop(_validated_name(name), None) is not None

    def get(self, name: str) -> RegisteredDelivery:
        """Return the registration for ``name``, or raise ``ValueError``."""

        key = _validated_name(name)
        with self._lock:
            registration = self._factories.get(key)
            known = sorted(self._factories)
        if registration is None:
            # Name the known modes, never the requested one: this message reaches
            # logs and CLI output and the request came from outside.
            raise ValueError(f"unknown delivery mode; registered: {', '.join(known) or 'none'}")
        return registration

    def names(self) -> tuple[str, ...]:
        """Every registered name, sorted, so callers and help text are stable."""

        with self._lock:
            return tuple(sorted(self._factories))

    def build(self, name: str, context: DeliveryContext) -> DeliverySink:
        """Build the named delivery sink from ``context``."""

        return self.get(name).factory(context)


def _validated_name(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("delivery mode name must be a non-empty trimmed string")
    return value


def _validated_factory(factory: DeliveryFactory) -> DeliveryFactory:
    if not callable(factory):
        raise TypeError("delivery factory must be callable")
    return factory


_DEFAULT_REGISTRY = DeliveryRegistry()


def default_delivery_registry() -> DeliveryRegistry:
    """The process-wide registry the SDK client selects a ``delivery_mode`` from."""

    return _DEFAULT_REGISTRY


def register_delivery(
    name: str,
    factory: DeliveryFactory,
    *,
    replace: bool = False,
) -> RegisteredDelivery:
    """Register a delivery factory in the process-wide registry."""

    return _DEFAULT_REGISTRY.register(name, factory, replace=replace)


def unregister_delivery(name: str) -> bool:
    """Remove a delivery factory from the process-wide registry."""

    return _DEFAULT_REGISTRY.unregister(name)


def get_delivery(name: str) -> RegisteredDelivery:
    """Look up one delivery factory in the process-wide registry."""

    return _DEFAULT_REGISTRY.get(name)


def delivery_modes() -> tuple[str, ...]:
    """Every ``delivery_mode`` registered in the process-wide registry, sorted."""

    return _DEFAULT_REGISTRY.names()


def build_delivery(name: str, context: DeliveryContext) -> DeliverySink:
    """Build a delivery sink from a process-wide registered factory, by name."""

    return _DEFAULT_REGISTRY.build(name, context)


__all__ = [
    "DeliveryContext",
    "DeliveryFactory",
    "DeliveryRegistry",
    "DeliverySink",
    "RegisteredDelivery",
    "build_delivery",
    "default_delivery_registry",
    "delivery_modes",
    "get_delivery",
    "register_delivery",
    "unregister_delivery",
]
