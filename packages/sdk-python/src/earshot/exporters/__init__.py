"""Deterministic projections of an incident bundle into universal export seams.

* :func:`to_otlp` -- OTLP/JSON trace document (identity- and evidence-preserving).
* :func:`to_openinference` -- the same document with OpenInference span-kind
  semantics for AI-native backends.
* :class:`OtlpHttpExporter` plus :func:`phoenix_exporter` / :func:`langfuse_exporter`
  -- a fail-open OTLP-HTTP push client that ships the document to an existing
  observability backend.
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

__all__ = [
    "OtlpExportResult",
    "OtlpHttpExporter",
    "langfuse_exporter",
    "phoenix_exporter",
    "serialize_document",
    "span_count",
    "to_openinference",
    "to_otlp",
]
