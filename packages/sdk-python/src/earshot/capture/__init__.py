"""Browser-capture server-side building blocks.

The capture path turns a browser telemetry drain into governed earshot facts. The
privacy-critical first step -- independently re-enforcing the server's own
allowlist over everything a client POSTs -- lives in :mod:`earshot.capture.sanitize`
as pure, framework-free helpers so both the HTTP endpoint and any in-process SDK
capture source enforce the *exact same* allowlist. Nothing here imports FastAPI.

``captureVersion: 2`` accumulates a whole browser call into one journal-backed
provisional artifact instead of a per-drain incident. :func:`identity.call_key`
names the call (project-scoped, so a cross-tenant collision is impossible by
construction), and :mod:`calls` sequences, projects and journals each drain of it.

:class:`~earshot.capture.source.BrowserCaptureSource` is the in-process sibling of
that HTTP path: it authors a browser call's sanitized batches straight into a
recorder the application already owns for the call, so browser evidence and the
app's own server/model/TTS/render evidence land in ONE incident bundle (two clock
domains, no invented cross-clock relation) instead of two artifacts to reconcile.
"""

from __future__ import annotations

from .calls import (
    END_CALL_ENDED,
    RECOVERY_METHOD,
    RECOVERY_REASON_SEALED,
    CaptureCall,
    CaptureCallCapacityError,
    CaptureCallClosedError,
    CaptureCallRegistry,
    CaptureDrain,
    CaptureEnd,
    CaptureError,
    CaptureSequenceConflictError,
    CaptureSequenceGapError,
    DrainOutcome,
    ResyncClaim,
)
from .identity import call_key
from .source import BrowserCaptureReport, BrowserCaptureSource

__all__ = [
    "END_CALL_ENDED",
    "RECOVERY_METHOD",
    "RECOVERY_REASON_SEALED",
    "BrowserCaptureReport",
    "BrowserCaptureSource",
    "CaptureCall",
    "CaptureCallCapacityError",
    "CaptureCallClosedError",
    "CaptureCallRegistry",
    "CaptureDrain",
    "CaptureEnd",
    "CaptureError",
    "CaptureSequenceConflictError",
    "CaptureSequenceGapError",
    "DrainOutcome",
    "ResyncClaim",
    "call_key",
]
