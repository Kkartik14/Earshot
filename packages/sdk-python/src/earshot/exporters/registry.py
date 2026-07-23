"""Named, pluggable incident projections.

An *exporter* here is a projection, not a transport: it takes a finished
:class:`~earshot.contract.IncidentBundle` and returns the document some backend
understands. :func:`~earshot.exporters.otlp.to_otlp` and
:func:`~earshot.exporters.openinference.to_openinference` were only ever reachable
by importing their module and calling them, which made every integration a code
change inside earshot. The registry turns that into a name: a caller says
``"otlp"``, and a user who has their own backend registers ``"acme"`` beside it
without touching this package.

Two properties keep the seam trustworthy:

* **Governed.** Every named export goes through
  :func:`~earshot.privacy.assert_export_allowed` against the exporter's declared
  destination before the projection runs. A bundle whose capture policy forbids
  that destination is refused, not projected and then discarded. Calling
  ``to_otlp`` directly stays unchecked exactly as before -- the caller is then the
  one holding the policy, which is why the registry, not the projection function,
  is the surface the client and CLI use.
* **Inert at import.** Registering the built-ins fills a dict and nothing else: no
  endpoint is contacted, no file is read, no environment is consulted, and the
  order of registration cannot change what any exporter produces. Importing this
  module must stay as free of consequence as importing a constant.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ..contract import IncidentBundle
from ..privacy import assert_export_allowed
from .openinference import to_openinference
from .otlp import to_otlp

# The export destination the OTLP-shaped projections declare. Both built-ins ship
# the same OTLP/JSON document to an OTLP collector, so a policy that names an
# allowed destination governs them as one -- adding ``openinference`` as a second
# destination name would silently widen every policy already written for ``otlp``.
_OTLP_DESTINATION = "otlp"


class IncidentExporter(Protocol):
    """Projects one finished incident into one backend's document shape.

    A plain function satisfies this; so does any callable object that also ships
    what it projected. The return value is the document, so a pushing exporter
    still hands back what it sent rather than swallowing it.
    """

    def __call__(self, bundle: IncidentBundle) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class RegisteredExporter:
    """One exporter under its registered name and export destination.

    ``destination`` is the name a capture policy's ``ExportPolicy.destinations``
    must permit for this exporter to run. It is separate from ``name`` because
    several projections can legitimately target one destination (the OTLP and
    OpenInference documents both go to an OTLP collector), and because a policy
    written against a destination must not be invalidated by someone registering a
    new projection name for the same backend.
    """

    name: str
    export: IncidentExporter
    destination: str


class ExporterRegistry:
    """A mutable set of named exporters, safe to share across threads.

    Instantiate one to keep a private set (tests, embedding hosts); the process
    default is :func:`default_registry`, which is what the SDK client and CLI use.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._exporters: dict[str, RegisteredExporter] = {}

    def register(
        self,
        name: str,
        exporter: IncidentExporter,
        *,
        destination: str | None = None,
        replace: bool = False,
    ) -> RegisteredExporter:
        """Register ``exporter`` under ``name``; return the registration.

        ``destination`` defaults to ``name`` so a new exporter is governed by a
        policy destination of its own name rather than inheriting somebody else's
        permission. A duplicate name is an error unless ``replace`` is set: silent
        replacement would make the projection a caller receives depend on import
        order.
        """

        registration = RegisteredExporter(
            name=_validated_name(name, "exporter name"),
            export=_validated_exporter(exporter),
            destination=_validated_name(
                name if destination is None else destination, "export destination"
            ),
        )
        with self._lock:
            if not replace and registration.name in self._exporters:
                raise ValueError("an exporter is already registered under that name")
            self._exporters[registration.name] = registration
        return registration

    def unregister(self, name: str) -> bool:
        """Remove ``name``; return whether anything was removed."""

        with self._lock:
            return self._exporters.pop(_validated_name(name, "exporter name"), None) is not None

    def get(self, name: str) -> RegisteredExporter:
        """Return the registration for ``name``, or raise ``ValueError``."""

        key = _validated_name(name, "exporter name")
        with self._lock:
            registration = self._exporters.get(key)
            known = sorted(self._exporters)
        if registration is None:
            # Name the known exporters, never the requested one: this message
            # reaches CLI output and logs, and the request came from outside.
            raise ValueError(f"unknown incident exporter; registered: {', '.join(known) or 'none'}")
        return registration

    def names(self) -> tuple[str, ...]:
        """Every registered name, sorted, so callers and help text are stable."""

        with self._lock:
            return tuple(sorted(self._exporters))

    def export(self, bundle: IncidentBundle, *, format: str = "otlp") -> Mapping[str, Any]:
        """Project ``bundle`` with the named exporter, honouring its export policy."""

        registration = self.get(format)
        assert_export_allowed(bundle, registration.destination)
        return registration.export(bundle)


def _validated_name(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty trimmed string")
    return value


def _validated_exporter(exporter: IncidentExporter) -> IncidentExporter:
    if not callable(exporter):
        raise TypeError("exporter must be callable")
    return exporter


_DEFAULT_REGISTRY = ExporterRegistry()
_DEFAULT_REGISTRY.register("otlp", to_otlp, destination=_OTLP_DESTINATION)
_DEFAULT_REGISTRY.register("openinference", to_openinference, destination=_OTLP_DESTINATION)


def default_registry() -> ExporterRegistry:
    """The process-wide registry the SDK client and CLI select exporters from."""

    return _DEFAULT_REGISTRY


def register_exporter(
    name: str,
    exporter: IncidentExporter,
    *,
    destination: str | None = None,
    replace: bool = False,
) -> RegisteredExporter:
    """Register an exporter in the process-wide registry."""

    return _DEFAULT_REGISTRY.register(name, exporter, destination=destination, replace=replace)


def unregister_exporter(name: str) -> bool:
    """Remove an exporter from the process-wide registry."""

    return _DEFAULT_REGISTRY.unregister(name)


def get_exporter(name: str) -> RegisteredExporter:
    """Look up one exporter in the process-wide registry."""

    return _DEFAULT_REGISTRY.get(name)


def exporter_names() -> tuple[str, ...]:
    """Every exporter name registered in the process-wide registry, sorted."""

    return _DEFAULT_REGISTRY.names()


def export_incident(bundle: IncidentBundle, *, format: str = "otlp") -> Mapping[str, Any]:
    """Project ``bundle`` with a process-wide registered exporter, by name."""

    return _DEFAULT_REGISTRY.export(bundle, format=format)


__all__ = [
    "ExporterRegistry",
    "IncidentExporter",
    "RegisteredExporter",
    "default_registry",
    "export_incident",
    "exporter_names",
    "get_exporter",
    "register_exporter",
    "unregister_exporter",
]
