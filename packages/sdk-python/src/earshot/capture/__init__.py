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
"""

from __future__ import annotations

from .calls import (
    RECOVERY_METHOD,
    RECOVERY_REASON_SEALED,
    CaptureCall,
    CaptureCallCapacityError,
    CaptureCallRegistry,
    CaptureDrain,
    CaptureError,
    CaptureSequenceConflictError,
    CaptureSequenceGapError,
    DrainOutcome,
    ResyncClaim,
)
from .identity import call_key

__all__ = [
    "RECOVERY_METHOD",
    "RECOVERY_REASON_SEALED",
    "CaptureCall",
    "CaptureCallCapacityError",
    "CaptureCallRegistry",
    "CaptureDrain",
    "CaptureError",
    "CaptureSequenceConflictError",
    "CaptureSequenceGapError",
    "DrainOutcome",
    "ResyncClaim",
    "call_key",
]
