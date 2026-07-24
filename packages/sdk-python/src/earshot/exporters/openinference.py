"""Projection of an ``IncidentBundle`` into OTLP/JSON with OpenInference semantics.

OpenInference is the span-kind vocabulary that Arize Phoenix and other AI-native
backends read to classify a trace's spans (``LLM``, ``TOOL``, ``AGENT``, ``AUDIO``,
``CHAIN`` ...). This exporter reuses the exact same identity-preserving,
evidence-faithful OTLP projection as :func:`earshot.exporters.otlp.to_otlp` and only
*adds* ``openinference.span.kind`` to each span, mapping the earshot voice-operation
name onto the OpenInference kind.

It deliberately does **not** rename model/tool/audio facts: any ``gen_ai.*`` or
OpenInference-named token/audio attribute already present on the source operation is
preserved verbatim. The ``earshot.*`` attributes continue to carry the
voice-pipeline boundaries (capture, VAD, endpointing, transport, render,
interruption) that OpenInference does not define.
"""

from __future__ import annotations

from typing import Any

from ..contract import IncidentBundle
from .otlp import _build_document

# earshot operation name -> OpenInference span kind.
#
# * ``llm`` / ``agent`` are the reasoning/model kinds.
# * ``tool`` is a tool invocation.
# * ``stt`` / ``tts`` are audio-content operations. A native speech-to-speech agent
#   can also be an audio operation, but earshot cannot reliably distinguish a
#   text-reasoning ``agent`` from a native-audio one without inventing a signal, so
#   ``agent`` maps to ``AGENT`` and the audio kinds are reserved for the operations
#   that are unambiguously audio work.
_OPENINFERENCE_KIND: dict[str, str] = {
    "llm": "LLM",
    "agent": "AGENT",
    "tool": "TOOL",
    "stt": "AUDIO",
    "tts": "AUDIO",
}

# Recognized voice-pipeline operations that are steps in the response chain but are
# not themselves AI/model/tool/audio work. They map to ``CHAIN`` so the span still
# renders as a legitimate pipeline step rather than an unknown.
_CHAIN_OPERATIONS: frozenset[str] = frozenset(
    {
        "capture",
        "vad",
        "turn_detection",
        "encode",
        "decode",
        "transport_send",
        "transport_receive",
        "render",
    }
)


def openinference_span_kind(operation_name: str | None) -> str:
    """Map an earshot operation name to an OpenInference span kind.

    ``None`` (a synthetic session span or a point-event span) maps to ``CHAIN``; an
    unrecognized operation name maps to ``UNKNOWN`` rather than being guessed.
    """

    if operation_name is None:
        return "CHAIN"
    if operation_name in _OPENINFERENCE_KIND:
        return _OPENINFERENCE_KIND[operation_name]
    if operation_name in _CHAIN_OPERATIONS:
        return "CHAIN"
    return "UNKNOWN"


def to_openinference(bundle: IncidentBundle) -> dict[str, Any]:
    """Project ``bundle`` into a deterministic OTLP/JSON document with OpenInference
    ``openinference.span.kind`` semantics on every span."""

    return _build_document(bundle, openinference_span_kind=openinference_span_kind)
