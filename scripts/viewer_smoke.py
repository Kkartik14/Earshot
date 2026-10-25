"""The viewer smoke: the bundled viewer and the API its call views use, on replayed calls.

It starts ``earshot serve`` on a fresh data directory (SQLite and local disk,
anonymous on loopback), replays the two real captures into it as their examples
sent them (``packages/sdk-python/tests/capture_replay.py``: Pipecat 1.5 with a mono
user/agent pair, LiveKit 1.8 with a stereo Ogg Opus recording), and waits until the
server's own jobs have closed both calls, analysed their audio and projected them.
Then it requests what a browser on ``/calls`` and ``/calls/{call_id}`` requests:

- the single-page app: ``/calls``, each call's page and its revision's session page
  serve ``index.html``; every script and stylesheet it names loads; the main script
  holds the calls views and names the lazily loaded player chunk, which loads too;
- the session rail beside every page: ``/v1/incidents?limit=50``, ``/v1/live/sessions``;
- the calls list: the first page, keyset paging, and each filter the list offers
  (agent, outcome, tag, reported latency, audible gap, cost with
  ``excluded_incomplete``, transcript text), each expected to match exactly the calls
  whose projection satisfies it;
- each call view: the detail (``no-store``, an ``ETag``, 304 on ``If-None-Match``),
  the revision's incident the view links to, each media entry's peaks (audiowaveform
  ``.dat`` v2, one channel per lane, 304 on its ``ETag``) and its audio by Range
  (206, the media's type), and every span a turn lists (an LLM span carries its
  input for the drawer).

The standard library only, so it runs against an installed artifact:
``viewer_smoke.py --earshot /path/to/venv/bin/earshot``. Without ``--earshot`` it
runs this Python's ``-m earshot.cli``. ``--keep-serving`` leaves the server up after
the checks (until standard input closes) for a manual check in a browser. On
success it prints one JSON summary and exits 0; a failed check exits 1 with the
server's log tail.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "sdk-python" / "tests"))

from capture_replay import replay_livekit, replay_pipecat  # noqa: E402

SETTLE_TIMEOUT_S = 180.0
DAT_HEADER = struct.Struct("<iIiiIi")  # version, flags, rate, samples/pixel, length, channels
AUDIO_MAGIC = {"audio/wav": b"RIFF", "audio/flac": b"fLaC", "audio/ogg": b"OggS"}
LLM_INPUT_KEYS = {"gen_ai.input.messages", "gen_ai.input"}


class SmokeFailure(AssertionError):
    """A check failed."""


def check(condition: object, message: object) -> None:
    if not condition:
        raise SmokeFailure(message)


class Server:
    """One ``earshot serve`` process on loopback."""

    def __init__(self, command: list[str], data_dir: Path, log: Path) -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = int(listener.getsockname()[1])
        environment = {k: v for k, v in os.environ.items() if not k.startswith("EARSHOT_")}
        self.log = log
        self._handle = log.open("ab")
        self.process = subprocess.Popen(
            [
                *command,
                *("serve", "--data-dir", str(data_dir), "--host", "127.0.0.1"),
                *("--port", str(self.port), "--log-level", "warning"),
            ],
            env=environment,
            stdout=self._handle,
            stderr=subprocess.STDOUT,
        )

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            connection.request(method, path, body=body, headers=dict(headers or {}))
            response = connection.getresponse()
            return (
                response.status,
                {name.lower(): value for name, value in response.getheaders()},
                response.read(),
            )
        finally:
            connection.close()

    def send(
        self, method: str, path: str, body: bytes | None, headers: Mapping[str, str]
    ) -> tuple[int, bytes]:
        status, _, reply = self.request(method, path, body, headers)
        return status, reply

    def json(self, path: str) -> Any:
        status, headers, body = self.request("GET", path)
        check(status == 200, f"GET {path}: {status} {body[:300]!r}")
        media_type = headers.get("content-type", "").split(";", 1)[0]
        check(media_type.startswith("application/") and media_type.endswith("json"), headers)
        return json.loads(body)

    def wait_ready(self, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            check(self.process.poll() is None, "earshot serve exited before it was ready")
            with contextlib.suppress(OSError):
                if self.request("GET", "/readyz")[0] == 200:
                    return
            time.sleep(0.1)
        raise SmokeFailure("earshot serve did not become ready")

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self._handle.close()


@contextlib.contextmanager
def running(command: list[str], work: Path) -> Iterator[Server]:
    server = Server(command, work / "data", work / "serve.log")
    try:
        server.wait_ready()
        yield server
    finally:
        server.stop()


def wait_projected(server: Server, call_id: str) -> dict[str, Any]:
    """The call's detail once it is closed and projected for its latest revision, with
    no analysis still pending."""

    deadline = time.monotonic() + SETTLE_TIMEOUT_S
    while True:
        detail = server.json(f"/v1/calls/{call_id}")
        projection, latest = detail["projection"], detail["latest_revision"]
        if (
            detail["state"] == "closed"
            and detail["projection_state"] == "current"
            and projection is not None
            and latest is not None
            and projection["revision"] == latest["revision"]
            and projection["analysis_state"] != "pending"
        ):
            check(projection["analysis_state"] == "stored", (call_id, projection["media"]))
            return detail
        check(
            time.monotonic() < deadline,
            f"{call_id} was not projected in {SETTLE_TIMEOUT_S} s: "
            f"{detail['state']}, {detail['projection_state']}",
        )
        time.sleep(0.5)


# --- the single-page app ---------------------------------------------------------------------


def check_spa(server: Server, pages: list[str]) -> dict[str, Any]:
    index = b""
    for page in pages:
        status, headers, index = server.request("GET", page)
        check(status == 200 and headers.get("content-type", "").startswith("text/html"), page)
        check(b'<div id="root"></div>' in index, f"{page} is not the viewer's index.html")
    assets = re.findall(rb'(?:src|href)="(/assets/[^"]+\.(?:js|css))"', index)
    check(assets, "index.html names no compiled asset")
    sizes = {}
    main = b""
    for asset in assets:
        status, _, body = server.request("GET", asset.decode())
        check(status == 200 and body, f"{asset!r}: {status}")
        sizes[asset.decode()] = len(body)
        if asset.endswith(b".js"):
            main += body
    check(b"/v1/calls" in main, "the bundled viewer has no calls views (a stale bundle?)")
    chunks = sorted(set(re.findall(rb"wavesurfer[\w.-]*\.js", main)))
    check(chunks, "the main script names no lazily loaded player chunk")
    for chunk in chunks:
        status, _, body = server.request("GET", f"/assets/{chunk.decode()}")
        check(status == 200 and body, f"player chunk {chunk!r}: {status}")
        sizes[f"/assets/{chunk.decode()}"] = len(body)
    return sizes


# --- the calls list ----------------------------------------------------------------------------


def list_ids(server: Server, query: Mapping[str, Any]) -> tuple[list[str], Any]:
    page = server.json("/v1/calls?" + urllib.parse.urlencode(query, doseq=True))
    return [item["call_id"] for item in page["items"]], page["excluded_incomplete"]


def check_list(server: Server, calls: Mapping[str, dict[str, Any]]) -> int:
    """The list's first page, paging, and every filter; the number of queries."""

    status, headers, _ = server.request("GET", "/v1/calls?limit=50")
    check(status == 200 and headers.get("cache-control") == "no-store", ("list", headers))
    newest_first = sorted(
        calls, key=lambda c: (calls[c]["first_evidence_unix_nano"], c), reverse=True
    )
    check(list_ids(server, {"limit": 50})[0] == newest_first, "the list's order")
    page = server.json("/v1/calls?limit=1")
    check(page["next_cursor"] is not None, "a one-call page has no next cursor")
    following = server.json(
        "/v1/calls?" + urllib.parse.urlencode({"limit": 1, "cursor": page["next_cursor"]})
    )
    check(
        [page["items"][0]["call_id"], following["items"][0]["call_id"]] == newest_first,
        "keyset paging",
    )

    projections = {call_id: detail["projection"] for call_id, detail in calls.items()}
    expected: list[tuple[dict[str, Any], set[str]]] = []
    for field in ("agent", "outcome"):
        for value in {p[field] for p in projections.values()}:
            expected.append(
                ({field: value}, {c for c, p in projections.items() if p[field] == value})
            )
    tags = sorted({tag for p in projections.values() for tag in p["tags"]}) or ["vip"]
    for tag in tags:
        expected.append(({"tag": tag}, {c for c, p in projections.items() if tag in p["tags"]}))
    for field, column in (
        ("reported_latency_ms", "reported_latency_p50_ms"),
        ("audible_gap_ms", "audible_gap_p50_ms"),
    ):
        values = [p[column] for p in projections.values() if p[column] is not None]
        check(len(values) == len(projections), f"a call has no {column}")
        expected.append(
            (
                {f"min_{field}": max(values)},
                {c for c, p in projections.items() if p[column] == max(values)},
            )
        )
        expected.append(
            (
                {f"max_{field}": min(values)},
                {c for c, p in projections.items() if p[column] == min(values)},
            )
        )
    lower = {c: p["cost"]["lower_bound_nano_usd"] for c, p in projections.items()}
    top = max(lower.values())
    expected.append(({"min_cost_nano_usd": top}, {c for c, v in lower.items() if v == top}))
    total = {c: lower[c] + (p["cost"]["estimated_nano_usd"] or 0) for c, p in projections.items()}
    complete = {c for c, p in projections.items() if p["cost"]["complete"]}
    check(complete, "no replayed call has a complete cost")
    cap = max(total[c] for c in complete)
    for query, ids in expected:
        check(set(list_ids(server, query)[0]) == ids, (query, ids))
    ids, excluded = list_ids(server, {"max_cost_nano_usd": cap})
    check(set(ids) == complete, ("max_cost_nano_usd", ids))
    known = {c for c in projections if c not in complete and total[c] <= cap}
    check(excluded == len(known), ("excluded_incomplete", excluded, len(known)))

    # Transcript text: a word in one call's placed transcript only, in any ASCII case.
    words = {
        c: {w for line in p["transcript"] for w in re.findall(r"[a-z]{5,}", line["text"].lower())}
        for c, p in projections.items()
    }
    for call_id, own in words.items():
        others = set().union(*(w for c, w in words.items() if c != call_id))
        word = sorted(own - others)[0]
        check(list_ids(server, {"q": word.upper()})[0] == [call_id], ("q", word))
    return len(expected) + 4 + len(words)


# --- the call view -----------------------------------------------------------------------------


def check_peaks(server: Server, media: Mapping[str, Any]) -> int:
    status, headers, body = server.request("GET", media["peaks_path"])
    check(status == 200 and len(body) >= DAT_HEADER.size, (media["peaks_path"], status))
    version, flags, _rate, per_pixel, length, channels = DAT_HEADER.unpack_from(body)
    check((version, flags & 1, per_pixel) == (2, 1, 256), ("peaks header", version, flags))
    check(channels == len(media["channels"]), ("peaks channels", channels, media["channels"]))
    check(len(body) == DAT_HEADER.size + length * channels * 2, "peaks body length")
    etag = headers.get("etag")
    check(etag, "peaks have no ETag")
    check(
        server.request("GET", media["peaks_path"], headers={"If-None-Match": etag})[0] == 304,
        "peaks 304",
    )
    return len(body)


def check_audio(server: Server, media: Mapping[str, Any]) -> int:
    status, headers, body = server.request(
        "GET", media["content_path"], headers={"Range": "bytes=0-1023"}
    )
    check(status == 206 and len(body) == 1024, (media["content_path"], status, len(body)))
    check(headers.get("content-type", "").split(";")[0] == media["content_type"], headers)
    check(headers.get("content-range", "").startswith("bytes 0-1023/"), headers)
    check(body.startswith(AUDIO_MAGIC[media["content_type"]]), "the audio's first bytes")
    return int(headers["content-range"].rsplit("/", 1)[1])


def check_call_view(server: Server, call_id: str) -> dict[str, Any]:
    status, headers, body = server.request("GET", f"/v1/calls/{call_id}")
    check(status == 200 and headers.get("cache-control") == "no-store", (call_id, headers))
    etag = headers.get("etag")
    check(etag, "the call detail has no ETag")
    check(
        server.request("GET", f"/v1/calls/{call_id}", headers={"If-None-Match": etag})[0] == 304,
        "call detail 304",
    )
    detail = json.loads(body)
    projection = detail["projection"]
    bundle_id = detail["latest_revision"]["bundle_id"]
    server.json(f"/v1/incidents/{urllib.parse.quote(bundle_id, safe='')}")

    media = [
        {
            "source": item["source"],
            "content_type": item["content_type"],
            "lanes": [channel["speaker"] for channel in item["channels"]],
            "peaks_bytes": check_peaks(server, item),
            "audio_bytes": check_audio(server, item),
        }
        for item in projection["media"]
    ]
    spans = llm_inputs = 0
    for turn in projection["turns"]:
        for span in turn["spans"]:
            path = f"/v1/calls/{call_id}/spans/{span['trace_id']}/{span['span_id']}"
            stored = server.json(path)
            check(
                (stored["trace_id"], stored["span_id"]) == (span["trace_id"], span["span_id"]), path
            )
            keys = {attribute["semantic_key"] for attribute in stored["attributes"]}
            llm_inputs += span["role"] == "llm" and bool(keys & LLM_INPUT_KEYS)
            spans += 1
    check(llm_inputs, f"{call_id}: no LLM span carries its input for the drawer")
    return {
        "framework": projection["framework"],
        "agent": projection["agent"],
        "media": media,
        "turns": [
            {
                key: turn["audible_gap"][key]
                for key in ("status", "value_ms", "uncertainty_ms", "bound_ms", "reason")
            }
            | {"reported_latency_ms": turn["reported_latency_ms"]}
            for turn in projection["turns"]
        ],
        "cost": projection["cost"],
        "transcript_lines": len(projection["transcript"]),
        "spans_fetched": spans,
    }


def smoke(command: list[str], work: Path, *, keep_serving: bool) -> dict[str, Any]:
    with running(command, work) as server:
        sessions = [replay(server.send).session_key for replay in (replay_pipecat, replay_livekit)]
        call_ids = []
        for session_key in sessions:
            status, _, body = server.request(
                "POST",
                "/v1/calls:resolve",
                json.dumps({"session_key": session_key}).encode(),
                {"Content-Type": "application/json"},
            )
            check(status == 200, ("resolve", status, body[:300]))
            call_ids.append(json.loads(body)["call_id"])
        calls = {call_id: wait_projected(server, call_id) for call_id in call_ids}

        pages = ["/calls"] + [f"/calls/{call_id}" for call_id in call_ids]
        pages += [f"/sessions/{d['latest_revision']['bundle_id']}" for d in calls.values()]
        assets = check_spa(server, pages)
        server.json("/v1/incidents?limit=50")
        server.json("/v1/live/sessions")
        queries = check_list(server, calls)
        views = {call_id: check_call_view(server, call_id) for call_id in call_ids}
        summary = {
            "viewer_assets": assets,
            "list_queries": queries,
            "calls": views,
        }
        if keep_serving:
            base = f"http://127.0.0.1:{server.port}"
            print(json.dumps(summary, indent=2), flush=True)
            print(f"serving {base}/calls (close standard input to stop)", flush=True)
            for call_id, view in views.items():
                print(f"  {view['framework']}: {base}/calls/{call_id}", flush=True)
            sys.stdin.read()
        return summary


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--earshot", help="the earshot executable (default: python -m earshot.cli)")
    parser.add_argument("--keep-serving", action="store_true")
    options = parser.parse_args(arguments)
    command = [options.earshot] if options.earshot else [sys.executable, "-m", "earshot.cli"]
    work = Path(tempfile.mkdtemp(prefix="earshot-viewer-smoke-"))
    try:
        summary = smoke(command, work, keep_serving=options.keep_serving)
    except SmokeFailure as failure:
        log = (work / "serve.log").read_text(errors="replace")[-3000:]
        print(f"viewer smoke FAILED: {failure}\n--- earshot serve log ---\n{log}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if not options.keep_serving:
        print(json.dumps(summary, indent=2))
    print("viewer smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
