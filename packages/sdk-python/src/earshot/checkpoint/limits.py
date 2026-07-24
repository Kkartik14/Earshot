"""Every size bound on the checkpoint path, decided once and in one place.

Four different bounds used to be chosen independently, and they did not fit
together: the journal writer framed records up to 32 MiB, the uploader batched
to 512 KiB but could still emit one whole frame far above that, and the ingest
API accepted a 1 MiB body. A single legitimately large frame — a raw OTLP
passthrough is the one record kind that reaches this size — could therefore
never be delivered at all, and the uploader learned that only as an opaque HTTP
failure that degraded it permanently.

The reconciled ladder, from the widest bound to the narrowest::

    DEFAULT_MAX_FRAME_BYTES          32 MiB   what the local journal will frame
    MAX_CHECKPOINT_FRAME_BYTES        1 MiB   what one upload may carry as a frame
      == MAX_CHECKPOINT_BATCH_BYTES           and therefore what ingest accepts
    DEFAULT_MAX_BATCH_BYTES         512 KiB   how much an uploader batches at once

Why this way round, rather than the two alternatives:

* **The writer keeps the larger frame.** The journal on disk is the source of
  truth and survives the process; capping it at what a transport happens to
  accept would let a remote bound damage local evidence, which is exactly
  backwards.
* **Ingest keeps the smaller body.** Checkpoint batches arrive every few hundred
  milliseconds from every live producer, and each one is buffered whole in
  server memory; a 32 MiB body times the live-session quota is a denial of
  service with a polite name. The single record kind that can exceed 1 MiB is
  the raw OTLP passthrough, whose payload the live tail withholds from
  subscribers anyway — so raising the wire bound would buy nothing a subscriber
  could see.
* **The batching bound is a cadence, not a licence.** It decides how much to
  send per request, never how much a request may be: one whole frame is always
  sent alone rather than stranded, and a frame above the wire bound is refused
  by the uploader itself, explicitly and terminally, instead of being posted
  into a guaranteed 413.

The cost of that choice is a declared coverage limit, stated by
``CHECKPOINT_COVERAGE_NOTE`` on ``GET /v1/live/sessions`` and by the uploader's
``undeliverable_frame`` state: remote live upload of a session ends at the first
frame above :data:`MAX_CHECKPOINT_FRAME_BYTES`. Nothing is lost — the local
journal still holds every record, and the artifact still travels through
``POST /v1/incidents`` or an operator seal — but the live view of that session
stops, loudly, at a named sequence.

Nothing here reads configuration or touches a file, so both ends of the wire can
import it without importing each other.
"""

from __future__ import annotations

from .framing import FRAME_OVERHEAD

# One admitted record is already bounded by the recorder's capture caps; this
# only has to stay above the largest one they can produce once encoded.
DEFAULT_MAX_FRAME_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_JOURNAL_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_JOURNAL_RECORDS = 100_000

# The largest body ``POST /v1/live/sessions/{id}/checkpoints`` will accept, and
# therefore the largest batch an uploader may send.
MAX_CHECKPOINT_BATCH_BYTES = 1024 * 1024
# A batch is one or more whole frames and may be exactly one, so the largest
# deliverable frame is the largest deliverable batch.
MAX_CHECKPOINT_FRAME_BYTES = MAX_CHECKPOINT_BATCH_BYTES
# What the server's frame scan admits as a body, once framing is paid for.
MAX_CHECKPOINT_FRAME_BODY_BYTES = MAX_CHECKPOINT_FRAME_BYTES - FRAME_OVERHEAD

# How much one upload request carries by default. Well under the wire bound so
# that ordinary traffic is many small frames per request rather than one large
# one, and clamped to it so the two can never cross.
DEFAULT_MAX_BATCH_BYTES = 512 * 1024

CHECKPOINT_COVERAGE_NOTE = (
    "remote checkpoint upload carries journal frames up to "
    f"{MAX_CHECKPOINT_FRAME_BYTES} bytes; a session whose journal frames a larger "
    "record stops being followed live at that sequence and says so, because the "
    "local journal — not this stream — is the complete record"
)

# The ladder is only a contract if it is checked. These are relationships the
# rest of the package assumes silently, so they are asserted where they are
# declared rather than left to a comment.
if not FRAME_OVERHEAD < MAX_CHECKPOINT_FRAME_BYTES <= DEFAULT_MAX_FRAME_BYTES:
    raise ValueError("the checkpoint wire bound must fit inside the journal frame bound")
if not DEFAULT_MAX_BATCH_BYTES <= MAX_CHECKPOINT_BATCH_BYTES:
    raise ValueError("the uploader batch bound must fit inside the checkpoint wire bound")


__all__ = [
    "CHECKPOINT_COVERAGE_NOTE",
    "DEFAULT_MAX_BATCH_BYTES",
    "DEFAULT_MAX_FRAME_BYTES",
    "DEFAULT_MAX_JOURNAL_BYTES",
    "DEFAULT_MAX_JOURNAL_RECORDS",
    "MAX_CHECKPOINT_BATCH_BYTES",
    "MAX_CHECKPOINT_FRAME_BODY_BYTES",
    "MAX_CHECKPOINT_FRAME_BYTES",
]
