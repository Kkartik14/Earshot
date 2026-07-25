"""The project-scoped identity of a continuous browser call.

A browser call is one continuous timeline, drained to the server in many
batches. To accumulate those batches into a single artifact the server needs a
*stable name for the call* that is the same across every drain and different
across every real call. :func:`call_key` derives that name, and it derives it in
a way that makes a cross-tenant collision impossible by construction.

The name is **project-scoped**: ``project_id`` comes from the authenticated
principal (``request.state.project_id``), never from the request body. Two
tenants that POST byte-identical bodies therefore land on two different keys, and
no tenant can compute another tenant's key, because the key folds in a value only
the credential holder is. Within one tenant the remaining components are the
browser's own stable per-recorder identity (``sessionId`` + the recorder's
random ``clockDomain.id`` + the capture version + the clock's wall origin). Those
are client-declared -- forgeable by *that tenant's own* credential holder, which
is inside the trust boundary -- so the artifact labels its continuity as
client-asserted (``capture.call_identity`` / ``client_declared_identity``) rather
than pretending otherwise. A page reload mints a fresh ``clockDomain.id``, so a
reload is honestly a new call rather than a spliced timeline; ``wallOriginMs`` is
folded in so a client that reused a clock-domain id across a new
``performance.timeOrigin`` still lands in a new call.

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
    """The project-scoped call key a continuous capture accumulates under.

    ``project_id`` is the authenticated principal and is folded in first, so the
    key of one tenant's call is uncomputable and uncollidable by any other
    tenant. The remaining components are the browser's stable per-recorder
    identity. ``wall_origin_ms`` may be ``None`` (a clock with no known wall
    origin) and is then folded in as an empty component -- distinct from any
    numeric origin.
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
    return f"capture-{digest[:32]}"


__all__ = ["call_key"]
