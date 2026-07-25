"""Deterministic projections of an incident bundle into universal export seams.

* :func:`to_otlp` -- OTLP/JSON trace document (identity- and evidence-preserving).
* :func:`to_openinference` -- the same document with OpenInference span-kind
  semantics for AI-native backends.
* :class:`OtlpHttpExporter` plus :func:`phoenix_exporter` / :func:`langfuse_exporter`
  -- a fail-open OTLP-HTTP push client that ships the document to an existing
  observability backend.

Each projection is also reachable *by name* through
:mod:`earshot.exporters.registry` (and therefore through the SDK client), which is
how a caller selects one -- or plugs in their own -- without importing any of this.
"""

from __future__ import annotations

from .openinference import to_openinference
from .otlp import span_count, to_otlp
from .push import (
    OtlpExportResult,
    OtlpHttpExporter,
    langfuse_exporter,
    phoenix_exporter,
    serialize_document,
)
from .registry import (
    ExporterRegistry,
    IncidentExporter,
    RegisteredExporter,
    default_registry,
    export_incident,
    exporter_names,
    get_exporter,
    register_exporter,
    unregister_exporter,
)

__all__ = [
    "ExporterRegistry",
    "IncidentExporter",
    "OtlpExportResult",
    "OtlpHttpExporter",
    "RegisteredExporter",
    "default_registry",
    "export_incident",
    "exporter_names",
    "get_exporter",
    "langfuse_exporter",
    "phoenix_exporter",
    "register_exporter",
    "serialize_document",
    "span_count",
    "to_openinference",
    "to_otlp",
    "unregister_exporter",
]
