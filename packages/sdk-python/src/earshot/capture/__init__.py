"""Browser-capture server-side building blocks.

The capture path turns a browser telemetry drain into governed earshot facts. The
privacy-critical first step -- independently re-enforcing the server's own
allowlist over everything a client POSTs -- lives in :mod:`earshot.capture.sanitize`
as pure, framework-free helpers so both the HTTP endpoint and any in-process SDK
capture source enforce the *exact same* allowlist. Nothing here imports FastAPI.
"""

from __future__ import annotations
