"""What every provider adapter shares: defaults, and the payload readers translators use.

The readers are strict where a value decides identity or time (a call ID, a call's
window) and lenient elsewhere: a field a provider omits, or sends with another
type, is left out of the evidence rather than failing the whole delivery.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .evidence import (
    TRANSCRIPT_INDEX,
    TRANSCRIPT_INTERRUPTED,
    TRANSCRIPT_LINE,
    TRANSCRIPT_SPEAKER,
    TRANSCRIPT_TEXT,
    TRANSCRIPT_TIMING,
    AttributeValue,
    EvidenceSpan,
    bounded_text,
)
from .types import DEFAULT_MAX_BODY_BYTES, DeliveryPayloadError

MAX_PROVIDER_ID_CHARACTERS = 512
MAX_CALL_NANO = 86_400 * 1_000_000_000
MAX_TRANSCRIPT_LINES = 100_000
MAX_LINE_CHARACTERS = 64 * 1024


class ProviderAdapter:
    """Defaults for an adapter (``types.ConnectorAdapter``)."""

    provider = ""
    translator_version = ""
    consumed_events: frozenset[str] = frozenset()
    streamed_events: frozenset[str] = frozenset()
    max_body_bytes = DEFAULT_MAX_BODY_BYTES
    serves_recordings_by_id = False

    @property
    def max_streamed_bytes(self) -> int:
        return self.max_body_bytes

    def delivery_key(self, event_type: str, provider_call_id: str) -> str:
        """One receipt per (event, call): a provider's retry of an event is the same
        delivery whatever else it changed (a re-signed URL, a new event time)."""

        return f"{event_type}:{provider_call_id}"


def mapping(value: object) -> Mapping[str, Any]:
    """``value`` if it is a JSON object, else an empty one."""

    return value if isinstance(value, Mapping) else {}


def required_mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DeliveryPayloadError
    return value


def event_name(value: object) -> str:
    """An event type: a non-blank string of at most 128 characters."""

    name = bounded_text(value, maximum=128)
    if name is None:
        raise DeliveryPayloadError
    return name


def provider_id(value: object) -> str:
    """A provider identifier: a non-empty string of at most 512 characters. Whether it
    is a valid session key is the kernel's call (422, not 400)."""

    if not isinstance(value, str) or not value or len(value) > MAX_PROVIDER_ID_CHARACTERS:
        raise DeliveryPayloadError
    return value


def iso_unix_nano(value: object) -> int | None:
    """An ISO 8601 time with a zone as Unix nanoseconds (``None`` if it is not one)."""

    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    seconds = parsed.timestamp()
    return round(seconds * 1_000_000) * 1_000 if seconds > 0 else None


def whole_number(value: object) -> int | None:
    """A non-negative integer (never a bool)."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def call_window(start: int | None, end: int | None) -> tuple[int, int]:
    """A call's window from the times a payload gives: one of them stands for both
    when the other is missing; none, a reversed or a day-long window is malformed."""

    if start is None and end is None:
        raise DeliveryPayloadError
    start = end if start is None else start
    end = start if end is None else end
    assert start is not None and end is not None
    if not 0 < start <= end or end - start > MAX_CALL_NANO or end > (1 << 63) - 1:
        raise DeliveryPayloadError
    return start, end


def present(values: Mapping[str, AttributeValue | None]) -> dict[str, AttributeValue]:
    """``values`` without the ``None``\\ s."""

    return {key: value for key, value in values.items() if value is not None}


def transcript_line(
    ordinal: int,
    *,
    speaker: str,
    text: object,
    start: int,
    end: int,
    timing: str,
    interrupted: bool | None = None,
) -> EvidenceSpan | None:
    """A transcript line span, or ``None`` for a line with no text."""

    if not isinstance(text, str) or not text.strip() or len(text) > MAX_LINE_CHARACTERS:
        return None
    return EvidenceSpan(
        TRANSCRIPT_LINE,
        ordinal,
        start,
        end,
        present(
            {
                TRANSCRIPT_SPEAKER: speaker,
                TRANSCRIPT_TEXT: text,
                TRANSCRIPT_TIMING: timing,
                TRANSCRIPT_INDEX: ordinal,
                TRANSCRIPT_INTERRUPTED: interrupted,
            }
        ),
    )


def items(value: object, *, maximum: int = MAX_TRANSCRIPT_LINES) -> list[object]:
    """A JSON array (empty when absent); a longer one than ``maximum`` is malformed."""

    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise DeliveryPayloadError
    return value
