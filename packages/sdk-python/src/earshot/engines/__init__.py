"""Deterministic, server-side diagnostic engines over raw browser telemetry.

These engines are the server-side consumer of what a browser collector emits.
Each turns raw, standard telemetry into governed earshot facts through the
recorder/pipeline seams, so the existing boundary-attribution analyzer diagnoses
the result without any browser in the loop:

* :func:`~earshot.engines.webrtc.analyze_webrtc_stats` -- a W3C ``getStats``
  delta engine (packet loss, jitter, RTT, jitter-buffer growth, concealment,
  reconnect, route change).
* :func:`~earshot.engines.device.analyze_audio_graph` -- Web Audio / device
  lifecycle diagnostics (permission, context suspension, sink change, sample-rate
  mismatch, under-run, ``baseLatency`` / ``outputLatency``).

Each ``analyze_*`` is a pure function returning an immutable facts value; the
paired ``apply_*`` derives and records onto a
:class:`~earshot.pipeline.TurnRecorder`.
"""

from __future__ import annotations

from .base import EngineCoverage, EngineEvent, EngineMeasurement
from .device import DeviceFacts, analyze_audio_graph, apply_audio_graph
from .webrtc import WebRtcFacts, analyze_webrtc_stats, apply_webrtc_stats

__all__ = [
    "DeviceFacts",
    "EngineCoverage",
    "EngineEvent",
    "EngineMeasurement",
    "WebRtcFacts",
    "analyze_audio_graph",
    "analyze_webrtc_stats",
    "apply_audio_graph",
    "apply_webrtc_stats",
]
