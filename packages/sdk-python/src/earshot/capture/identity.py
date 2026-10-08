"""The project-scoped identity of a continuous browser call.

A browser call is one continuous timeline, drained to the server in many
batches. To accumulate those batches into a single artifact the server needs a
*stable name for the call* that is the same across every drain and differs for
distinct project/client identity tuples, barring a cryptographic hash collision.
:func:`call_key` derives that name; it is an identifier, not an authorization
credential.

The name includes ``project_id`` from the authenticated principal
(``request.state.project_id``), never from the request body. This namespaces
otherwise identical client IDs by project; project authorization is still enforced
at the API boundary and by project-scoped registry lookups. Within one project the
remaining components are the browser's stable per-recorder identity (``sessionId``
+ the recorder's random ``clockDomain.id`` + the capture version + the clock's wall
origin). Those are client-declared and forgeable by that project's credential
holder, so the artifact labels continuity as client-asserted
(``capture.call_identity`` / ``client_declared_identity``). A page reload mints a
fresh ``clockDomain.id``, so a reload is honestly a new call rather than a spliced
timeline; ``wallOriginMs`` is folded in so a client that reused a clock-domain ID
across a new ``performance.timeOrigin`` still lands in a new call.

Deterministic by construction: the same authenticated principal and the same
client-stable ids always yield the same key, with no clock or randomness -- the
call's identity is the call's identity, not the moment its first drain arrived.
"""

from __future__ import annotations

import hashlib

# ASCII unit separator: an unambiguous field delimiter that cannot appear inside
# any of the joined components (ids are constrained to ``_CAPTURE_ID_PATTERN``,
# the version and the wall origin are numeric), so no two distinct component
# tuples can serialize to the same byte string.
_UNIT_SEPARATOR = b"\x1f"
CAPTURE_CALL_ID_PREFIX = "capture-"


def _component(value: str) -> bytes:
    return value.encode("utf-8", errors="surrogatepass")


def call_key(
    *,
    project_id: str,
    capture_version: int,
    session_id: str,
    clock_domain_id: str,
    wall_origin_ms: float | None,
) -> str:
    """The stable call key a continuous capture accumulates under.

    ``project_id`` is the authenticated principal and is folded in first, so
    identical browser IDs in different projects produce different names (subject
    to the usual hash-collision bound). It is namespacing, not an authorization
    check. The remaining components are the browser's stable per-recorder identity.
    ``wall_origin_ms`` may be ``None`` (a clock with no known wall origin) and is
    then folded in as an empty component -- distinct from any numeric origin.
    """

    origin = b"" if wall_origin_ms is None else repr(float(wall_origin_ms)).encode("ascii")
    material = _UNIT_SEPARATOR.join(
        (
            _component(project_id),
            str(int(capture_version)).encode("ascii"),
            _component(session_id),
            _component(clock_domain_id),
            origin,
        )
    )
    digest = hashlib.sha256(material).hexdigest()
    return f"{CAPTURE_CALL_ID_PREFIX}{digest[:32]}"


__all__ = ["CAPTURE_CALL_ID_PREFIX", "call_key"]
