from __future__ import annotations

import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from earshot.codec import encode_incident_protobuf
from earshot.contract import ErrorRecord
from earshot.exporter import BoundedAsyncExporter, ExportItem, HttpExportTransport
from earshot.recorder import IncidentRecorder
from earshot.validation import IncidentValidationError, validate_incident
from incident_factory import SECRET_SENTINEL, point
from test_contract_validation import issue_codes, replace_profile

pytestmark = pytest.mark.unit


def test_error_message_cannot_self_classify_as_metadata(valid_bundle) -> None:
    operations = list(valid_bundle.profile.operations)
    operations[0] = operations[0].model_copy(
        update={
            "error": ErrorRecord(
                code="application.failure",
                category="application",
                message=SECRET_SENTINEL,
                capture_class="metadata",
            )
        }
    )
    poisoned = replace_profile(valid_bundle, operations=tuple(operations))
    assert "EARSHOT_PRIVACY_PAYLOAD_SMUGGLED" in issue_codes(poisoned)
    with pytest.raises(IncidentValidationError):
        encode_incident_protobuf(poisoned)


def test_cross_origin_redirect_never_receives_bearer_token() -> None:
    observed: dict[str, str | None] = {"authorization": None}

    class SinkHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            observed["authorization"] = self.headers.get("Authorization")
            self.send_response(200)
            self.end_headers()

        def do_POST(self) -> None:
            observed["authorization"] = self.headers.get("Authorization")
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args) -> None:
            return None

    sink = ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            port = sink.server_address[1]
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{port}/captured")
            self.end_headers()

        def log_message(self, *_args) -> None:
            return None

    redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
    redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
    sink_thread.start()
    redirect_thread.start()
    try:
        endpoint = f"http://127.0.0.1:{redirect.server_address[1]}"
        transport = HttpExportTransport(endpoint, token=SECRET_SENTINEL, timeout=2)
        with pytest.raises((RuntimeError, urllib.error.URLError)):
            transport.send(ExportItem("redirect-test", b"payload"))
        assert observed["authorization"] is None
    finally:
        redirect.shutdown()
        sink.shutdown()
        redirect.server_close()
        sink.server_close()
        redirect_thread.join(timeout=2)
        sink_thread.join(timeout=2)


def test_reentrant_diagnostic_callback_cannot_deadlock_submit() -> None:
    callback_entered = False
    nested_results: list[bool] = []
    exporter: BoundedAsyncExporter

    def diagnostic(_event) -> None:
        nonlocal callback_entered
        if callback_entered:
            return
        callback_entered = True
        nested_results.append(exporter.submit(ExportItem("nested", b"nested")))

    class NoopTransport:
        def send(self, _item) -> None:
            return None

    exporter = BoundedAsyncExporter(NoopTransport(), diagnostic=diagnostic)
    assert exporter.shutdown()
    outer_results: list[bool] = []
    thread = threading.Thread(
        target=lambda: outer_results.append(exporter.submit(ExportItem("outer", b"outer"))),
        daemon=True,
    )
    thread.start()
    thread.join(timeout=1)
    assert not thread.is_alive(), (
        "diagnostic callback deadlocked submit while exporter lock was held"
    )
    assert outer_results == [False]
    assert nested_results == [False]


def test_recording_failure_never_masks_application_exception() -> None:
    class AppFailure(RuntimeError):
        pass

    recorder = IncidentRecorder()
    with pytest.raises(AppFailure, match="application failed"), recorder.operation("tool"):
        recorder.close()
        raise AppFailure(f"application failed {SECRET_SENTINEL}")
    bundle = recorder.close()
    assert isinstance(recorder.last_export_error, RuntimeError)
    assert SECRET_SENTINEL not in str(recorder.last_export_error)
    assert SECRET_SENTINEL not in str(bundle.model_dump(mode="python"))


@pytest.mark.parametrize("record_kind", ["operation", "event"])
def test_stream_and_participant_must_describe_the_same_owner(
    valid_bundle, record_kind: str
) -> None:
    if record_kind == "operation":
        records = list(valid_bundle.profile.operations)
        records[0] = records[0].model_copy(
            update={"participant_id": "participant-user", "stream_id": "stream-output"}
        )
        broken = replace_profile(valid_bundle, operations=tuple(records))
    else:
        records = list(valid_bundle.profile.events)
        records[0] = records[0].model_copy(
            update={"participant_id": "participant-user", "stream_id": "stream-output"}
        )
        broken = replace_profile(valid_bundle, events=tuple(records))
    assert "EARSHOT_STREAM_PARTICIPANT_MISMATCH" in issue_codes(broken)


def test_same_domain_mixed_clock_representations_still_detect_reversal(valid_bundle) -> None:
    operations = list(valid_bundle.profile.operations)
    operations[0] = operations[0].model_copy(
        update={
            "started_at": point(100).model_copy(
                update={"source_time_unix_nano": "100", "monotonic_time_nano": "100"}
            ),
            "ended_at": point(50).model_copy(
                update={"source_time_unix_nano": "50", "monotonic_time_nano": None}
            ),
        }
    )
    broken = replace_profile(valid_bundle, operations=tuple(operations))
    report = validate_incident(broken)
    assert "EARSHOT_TIME_RANGE_REVERSED" in {issue.code for issue in report.errors}
