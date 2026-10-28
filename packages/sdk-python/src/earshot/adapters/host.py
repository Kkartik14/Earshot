"""Record the host span profile's spans (``call_span_facts``) in a call's revision.

The profile is already Earshot's own vocabulary, so there is nothing to translate:
each span becomes one operation under its name, at its times on the call's clock,
with the turn its ``earshot.turn.id`` names. The materializer hands it spans as
``call_materializer.adapter_span`` converts them, already filtered to the attributes
the revision may keep.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..contract import Adapter, Evidence, TimePoint
from ..recorder import IncidentRecorder
from ..versions import HOST_PROFILE_VERSION
from .base import stable_id


class HostAdapter:
    def __init__(self, recorder: IncidentRecorder) -> None:
        self.recorder = recorder
        recorder.register_adapter(
            Adapter(name="earshot.host", version=HOST_PROFILE_VERSION, framework="host")
        )

    def _time(self, unix_nano: int) -> TimePoint:
        return TimePoint(
            source_time_unix_nano=str(unix_nano), clock_domain_id=self.recorder.clock_domain_id
        )

    def consume_span(self, span: Mapping[str, Any]) -> None:
        attributes = span["attributes"]
        turn_id = attributes.get("earshot.turn.id")
        scope = span["instrumentation_scope"]
        self.recorder.record_operation(
            operation_id=stable_id("host", span["trace_id"], span["span_id"]),
            operation_name=span["name"],
            status=span["status"],
            started_at=self._time(span["start_time"]),
            ended_at=self._time(span["end_time"]),
            turn_id=None if turn_id is None else str(turn_id),
            trace_id=span["trace_id"],
            span_id=span["span_id"],
            parent_span_id=span["parent_span_id"],
            instrumentation_scope_name=scope["name"],
            instrumentation_scope_version=scope["version"],
            evidence=Evidence(
                source="host",
                observer="host",
                method="otlp_host_profile",
                method_version=HOST_PROFILE_VERSION,
                confidence="measured",
                availability="available",
                source_field=span["name"],
            ),
            attributes=attributes,
        )
