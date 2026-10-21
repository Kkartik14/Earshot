"""Every route answers what its OpenAPI response models declare.

Most handlers build a ``JSONResponse`` by hand, which FastAPI never checks against
the route's ``response_model``. The OpenAPI document, the viewer's generated
``schema.d.ts`` and the Node SDK's types are generated from those models, so a
body that drifts from its model breaks every client while the spec stays green.

This test drives the call, recording, span and price routes on a real server
over a real catalog (both dialects) and checks every reply: its status is one
the route declares, and its body validates, strictly and with no undeclared
field, against the model declared for that status (or has a declared media type,
or is empty when nothing is declared). A route under those prefixes that the
scenario never drives fails the test, so a new route arrives with its check.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from pydantic import TypeAdapter

from audio_support import wav
from captured_otlp import CAPTURED_SESSION_KEY, captured_requests
from earshot.api import create_app
from earshot.blobstore import LocalBlobStore
from earshot.call_capture import spans_from_request
from earshot.clock import ManualClock
from earshot.storage_calls import CallStore, CallTimings
from earshot.storage_closer import Closer
from earshot.storage_recordings import RecordingStore
from otlp_support import Reply, request, serve
from store_support import make_store

pytestmark = pytest.mark.integration

SECOND = 1_000_000_000
START = 1_800_000_000 * SECOND
# The route families this contract test must cover completely (F-4).
COVERED_PREFIXES = ("/v1/calls", "/v1/recordings", "/v1/prices", "/v1/dashboard")
DAY = 86_400 * SECOND
JSON = "application/json"


def _declared_statuses(route: APIRoute) -> set[int]:
    return {route.status_code or 200} | {int(key) for key in route.responses if str(key).isdigit()}


@dataclass
class ContractClient:
    """Sends requests to a served app and checks each reply against its route."""

    app: FastAPI
    port: int
    covered: set[tuple[str, str]] = field(default_factory=set)

    def route(self, method: str, template: str) -> APIRoute:
        [route] = [
            route
            for route in self.app.routes
            if isinstance(route, APIRoute) and route.path == template and method in route.methods
        ]
        return route

    def __call__(
        self,
        method: str,
        template: str,
        path: str | None = None,
        body: object = None,
        *,
        headers: dict[str, str] | None = None,
        credential: str | None = None,
    ) -> Reply:
        route = self.route(method, template)
        sent = dict(headers or {})
        if credential is not None:
            sent["Authorization"] = f"Bearer {credential}"
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
            sent.setdefault("Content-Type", JSON)
        reply = request(self.port, method, path or template, body, headers=sent)
        self.check(route, reply)
        self.covered.add((method, template))
        return reply

    @staticmethod
    def check(route: APIRoute, reply: Reply) -> None:
        where = (sorted(route.methods), route.path, reply.status, reply.body[:300])
        assert reply.status in _declared_statuses(route), where
        declared = route.responses.get(reply.status) or route.responses.get(str(reply.status))
        declared = declared or {}
        if reply.status == (route.status_code or 200) and route.response_model is not None:
            model = route.response_model
        else:
            model = declared.get("model")
        if model is not None:
            assert reply.media_type == JSON, where
            TypeAdapter(model).validate_json(reply.body, strict=True)
        elif declared.get("content"):
            assert reply.media_type in declared["content"], where
        else:
            assert reply.body == b"", where

    def uncovered(self) -> set[tuple[str, str]]:
        routes = {
            (method, route.path)
            for route in self.app.routes
            if isinstance(route, APIRoute) and route.path.startswith(COVERED_PREFIXES)
            for method in route.methods
            if method != "HEAD"
        }
        return routes - self.covered


def _digest(body: bytes) -> str:
    return f"sha-256=:{base64.b64encode(hashlib.sha256(body).digest()).decode()}:"


def _record_captured_call(store, clock: ManualClock) -> str:
    calls = CallStore(store)
    call_ids: set[str] = set()
    for item in captured_requests():
        call_ids.update(calls.record_spans("default", spans_from_request(item)).call_ids)
        clock.advance(5 * SECOND)
    [call_id] = call_ids
    return call_id


def _first_llm_span() -> tuple[str, str]:
    for item in captured_requests():
        for span in spans_from_request(item):
            if span.span.name == "llm":
                return span.span.trace_id.hex(), span.span.span_id.hex()
    raise AssertionError("the capture has an llm span")


def _captured_day() -> int:
    """The UTC day the capture's evidence starts in (its call's dashboard day)."""

    started = min(
        span.span.start_time_unix_nano
        for item in captured_requests()
        for span in spans_from_request(item)
    )
    return started // DAY * DAY


def test_every_call_recording_span_and_price_route_answers_its_declared_models(
    tmp_path: Path,
) -> None:
    clock = ManualClock(wall=START, monotonic=START)
    store = make_store(tmp_path, clock=clock)
    blobs = LocalBlobStore(tmp_path / "blobs")
    call_id = _record_captured_call(store, clock)
    store.create_project("quiet", display_name="Quiet")
    store.set_capture_preset("quiet", "metadata_only")
    quiet = store.issue_api_key("quiet", label="quiet").credential
    audio = wav(seconds=1.0)
    trace_id, span_id = _first_llm_span()
    unknown_call = "call_0123456789abcdef0123456789abcdef"
    app = create_app(store=store, blob_store=blobs)

    with serve(app) as port:
        send = ContractClient(app, port)
        # Recordings: create (201, then 200 for the same manifest), upload, read.
        manifest = {
            "recording_id": "conv-1.user",
            "session_key": CAPTURED_SESSION_KEY,
            "speaker": "user",
            "content_type": "audio/wav",
            "started_at_unix_nano": str(START),
        }
        assert send("POST", "/v1/recordings", body=manifest).status == 201
        assert send("POST", "/v1/recordings", body=manifest).status == 200
        assert send("POST", "/v1/recordings", body={**manifest, "speaker": "x"}).status == 422
        early = "/v1/recordings/conv-1.user/content"
        assert send("GET", "/v1/recordings/{recording_id}/content", early).status == 409
        uploaded = send(
            "PUT",
            "/v1/recordings/{recording_id}/content",
            early,
            audio,
            headers={"Content-Type": "audio/wav", "Content-Digest": _digest(audio)},
        )
        assert uploaded.status == 200
        assert send("GET", "/v1/recordings/{recording_id}", "/v1/recordings/conv-1.user").status
        assert send("GET", "/v1/recordings/{recording_id}", "/v1/recordings/nope").status == 404
        assert send("GET", "/v1/recordings/{recording_id}/content", early).status == 200
        ranged = send(
            "GET", "/v1/recordings/{recording_id}/content", early, headers={"Range": "bytes=0-9"}
        )
        assert ranged.status == 206
        # The analysis outputs: 404 until the server's analysis job stores them.
        peaks_route = "/v1/recordings/{recording_id}/peaks"
        assert send("GET", peaks_route, "/v1/recordings/nope/peaks").status == 404
        peaks = send("GET", peaks_route, "/v1/recordings/conv-1.user/peaks")
        deadline = time.monotonic() + 60
        while peaks.status == 404 and time.monotonic() < deadline:
            time.sleep(0.1)
            peaks = send("GET", peaks_route, "/v1/recordings/conv-1.user/peaks")
        assert peaks.status == 200
        revalidated = send(
            "GET",
            peaks_route,
            "/v1/recordings/conv-1.user/peaks",
            headers={"If-None-Match": peaks.headers["etag"]},
        )
        assert revalidated.status == 304
        # A single recording is no mono pair: it holds no rendition.
        rendition = "/v1/recordings/conv-1.user/rendition"
        assert send("GET", "/v1/recordings/{recording_id}/rendition", rendition).status == 404
        rendition_peaks = "/v1/recordings/{recording_id}/rendition/peaks"
        assert send("GET", rendition_peaks, f"{rendition}/peaks").status == 404
        # Close the call; the server's projection job projects it.
        clock.advance(CallTimings().end_grace_nano)
        recordings = RecordingStore(store, calls=CallStore(store), blobs=blobs)
        assert Closer(store, calls=CallStore(store), recordings=recordings).close_next()
        detail_path = f"/v1/calls/{call_id}"
        deadline = time.monotonic() + 60
        while (
            send("GET", "/v1/calls/{call_id}", detail_path).json()["projection_state"] != "current"
            and time.monotonic() < deadline
        ):
            time.sleep(0.1)

        resolved = send("POST", "/v1/calls:resolve", body={"session_key": CAPTURED_SESSION_KEY})
        assert resolved.json() == {"call_id": call_id}
        assert send("POST", "/v1/calls:resolve", body={"session_key": "nobody"}).status == 404

        listed = send("GET", "/v1/calls").json()
        [projection] = [item["projection"] for item in listed["items"]]
        assert projection is not None and listed["items"][0]["projection_state"] == "current"
        searched = send(
            "GET",
            "/v1/calls",
            f"/v1/calls?q=table&agent={projection['agent']}&max_cost_nano_usd={10**12}",
        )
        # Two LLM requests reported no usage: the cost is incomplete.
        assert searched.json()["excluded_incomplete"] == 1
        assert send("GET", "/v1/calls", "/v1/calls?q=ab").status == 422
        forbidden = send("GET", "/v1/calls", "/v1/calls?q=table", credential=quiet)
        assert forbidden.problem_code() == "EARSHOT_PRESET_FORBIDS_CONTENT_SEARCH"
        assert send("GET", "/v1/calls", "/v1/calls?cursor=nope").status == 400

        detail = send("GET", "/v1/calls/{call_id}", detail_path)
        # One mono user recording is no recorder timeline: nothing is measured.
        gaps = [turn["audible_gap"] for turn in detail.json()["projection"]["turns"]]
        assert {(gap["status"], gap["reason"]) for gap in gaps} == {
            ("not_measurable", "not_one_timeline")
        }
        unchanged = send(
            "GET",
            "/v1/calls/{call_id}",
            detail_path,
            headers={"If-None-Match": detail.headers["etag"]},
        )
        assert unchanged.status == 304
        assert send("GET", "/v1/calls/{call_id}", f"/v1/calls/{unknown_call}").status == 404

        span_template = "/v1/calls/{call_id}/spans/{trace_id}/{span_id}"
        span = send("GET", span_template, f"{detail_path}/spans/{trace_id}/{span_id}")
        assert span.json()["span_id"] == span_id
        missing = send("GET", span_template, f"{detail_path}/spans/{trace_id}/{'0' * 16}")
        assert missing.status == 404

        day = _captured_day()
        dashboard = send(
            "GET",
            "/v1/dashboard",
            f"/v1/dashboard?from={day}&to={day + DAY}&bucket=hour&group_by=agent",
        )
        assert dashboard.json()["overall"]["calls"] == 1
        assert len(dashboard.json()["buckets"]) == 24
        backwards = send("GET", "/v1/dashboard", f"/v1/dashboard?from={day}&to={day}")
        assert backwards.status == 422

        price = {
            "provider": "Groq",
            "model": "openai/gpt-oss-20b",
            "unit": "input_token",
            "price_nano_usd": 75,
            "effective_from_unix_nano": "0",
        }
        assert send("POST", "/v1/prices", body=price).status == 201
        assert send("POST", "/v1/prices", body={**price, "unit": "token"}).status == 422
        listed_prices = send("GET", "/v1/prices").json()["items"]
        assert [item["provider"] for item in listed_prices if item["scope"] == "project"] == [
            "groq"
        ]
        assert any(item["scope"] == "builtin" for item in listed_prices)

        # A recording's transcript: 201, the same one again 200, another 409.
        transcript_route = "/v1/recordings/{recording_id}/transcript"
        transcript_path = "/v1/recordings/conv-1.user/transcript"
        vtt = b"WEBVTT\n\n00:01.000 --> 00:02.000\nHello.\n"
        text = {"Content-Type": "text/plain"}
        sent_vtt = {"Content-Type": "text/vtt"}
        assert send("POST", transcript_route, transcript_path, vtt, headers=sent_vtt).status == 201
        assert send("POST", transcript_route, transcript_path, vtt, headers=sent_vtt).status == 200
        assert send("POST", transcript_route, transcript_path, b"Hi.", headers=text).status == 409
        invalid = send("POST", transcript_route, transcript_path, b"user:", headers=text)
        assert invalid.status == 422
        unknown = "/v1/recordings/nope/transcript"
        assert send("POST", transcript_route, unknown, vtt, headers=sent_vtt).status == 404

        assert send("DELETE", "/v1/calls/{call_id}", detail_path).status == 204
        assert send("DELETE", "/v1/calls/{call_id}", detail_path).status == 404

    assert send.uncovered() == set()
