"""Scrub a real LiveKit OTLP capture into ``fixtures/captured/livekit-otlp-1.8``.

    python scripts/build_livekit_otlp_fixture.py \\
        --capture .earshot/otlp_capture/livekit-run1 --run .earshot/livekit_otlp/run1

``--capture`` is the output of ``scripts/otlp_capture_sink.py``; ``--run`` is the
``--out`` of ``examples/livekit_otlp/run_call.py``. The scrub follows
``build_pipecat_otlp_fixture.py``: every request is decoded, scrubbed (recursively,
through array and kvlist values) and re-serialized deterministically, the
``RecorderIO`` recording is copied byte for byte, and the build refuses to write a
fixture that still contains any value from ``--env`` (it reports key names, never
values). Needs ``opentelemetry-proto`` and PyAV (``av``, from the ``[livekit]`` extra).
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import hashlib
import json
import re
import shutil
import socket
import sys
from collections import Counter, defaultdict
from importlib.metadata import version
from pathlib import Path
from typing import Any

import av
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "fixtures" / "captured" / "livekit-otlp-1.8"
END_MARKER = ("livekit-agents", "session_close")
NON_SECRET_ENV_KEYS = {"GROQ_MODEL", "CARTESIA_MODEL"}
PRETTIER_WIDTH = 90  # .prettierrc printWidth
SCRUB_RULES = [
    "Resource attribute service.instance.id is replaced with 'captured-instance'.",
    "Resource attributes under host.* and process.* (if any) become 'captured-<key>'.",
    "Any string value equal to CARTESIA_VOICE_ID becomes 'captured-cartesia-voice'.",
    "Inside every string value (resource, scope, span, event and link attributes, recursing "
    "into array and kvlist values): any absolute path up to site-packages/ becomes "
    "'/captured-site-packages/', the home directory becomes '/captured-home', and the local "
    "username and hostname become 'captured-user' / 'captured-host' (LiveKit's "
    "event_loop_blocked spans carry stack traces with local paths).",
    "Each request is re-serialized with SerializeToString(deterministic=True).",
    "The build aborts if any .env value (except GROQ_MODEL and CARTESIA_MODEL, public model "
    "ids that LiveKit records as gen_ai.request.model) occurs anywhere in the fixture bytes.",
    "Trace/span ids, timestamps, the fake job's room SID and job id, provider request ids, "
    "transcripts and LLM/TTS text are kept as captured: the caller is a fixed synthetic "
    "script and the agent text is model output.",
    "audio/recording.ogg is the RecorderIO file byte for byte (no re-encode, no trim).",
]


def _env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines() if path.exists() else []:
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2).strip().strip("'\"")
    return values


SITE_PACKAGES = re.compile(r"/[^\s\"']*/site-packages/")


class Scrubber:
    def __init__(self, env: dict[str, str]) -> None:
        voice = env.get("CARTESIA_VOICE_ID")
        self.exact = {voice: "captured-cartesia-voice"} if voice else {}
        pairs = [
            (str(Path.home()), "/captured-home"),
            (getpass.getuser(), "captured-user"),
            (socket.gethostname(), "captured-host"),
            (socket.gethostname().split(".")[0], "captured-host"),
        ]
        self.substrings = [
            (needle, label) for needle, label in pairs if needle and len(needle) >= 3
        ]

    def value(self, any_value: Any) -> None:
        kind = any_value.WhichOneof("value")
        if kind == "string_value":
            text = self.exact.get(any_value.string_value, any_value.string_value)
            text = SITE_PACKAGES.sub("/captured-site-packages/", text)
            for needle, placeholder in self.substrings:
                text = text.replace(needle, placeholder)
            any_value.string_value = text
        elif kind == "array_value":
            for item in any_value.array_value.values:
                self.value(item)
        elif kind == "kvlist_value":
            for item in any_value.kvlist_value.values:
                self.value(item.value)

    def request(self, request: ExportTraceServiceRequest) -> None:
        for resource_spans in request.resource_spans:
            for attribute in resource_spans.resource.attributes:
                if attribute.key == "service.instance.id":
                    attribute.value.string_value = "captured-instance"
                elif attribute.key.startswith(("host.", "process.")):
                    attribute.value.string_value = f"captured-{attribute.key}"
                self.value(attribute.value)
            for scope_spans in resource_spans.scope_spans:
                for attribute in scope_spans.scope.attributes:
                    self.value(attribute.value)
                for span in scope_spans.spans:
                    attributes = list(span.attributes)
                    attributes += [a for event in span.events for a in event.attributes]
                    attributes += [a for link in span.links for a in link.attributes]
                    for attribute in attributes:
                        self.value(attribute.value)


def _spans(request: ExportTraceServiceRequest) -> list[tuple[str, str, Any]]:
    return [
        (scope_spans.scope.name, scope_spans.scope.version, span)
        for resource_spans in request.resource_spans
        for scope_spans in resource_spans.scope_spans
        for span in scope_spans.spans
    ]


def _shape(spans: list[tuple[str, str, Any]]) -> dict[str, Any]:
    """Exact attribute keys and event names per span name, with presence counts."""
    shape: dict[str, Any] = {}
    for name in sorted({span.name for _, _, span in spans}):
        group = [span for _, _, span in spans if span.name == name]
        keys = Counter(attribute.key for span in group for attribute in span.attributes)
        events: dict[str, Any] = defaultdict(lambda: {"count": 0, "attributes": set()})
        for span in group:
            for event in span.events:
                events[event.name]["count"] += 1
                events[event.name]["attributes"].update(a.key for a in event.attributes)
        shape[name] = {
            "spans": len(group),
            "attributes": dict(sorted(keys.items())),
            "events": {
                event: {"count": data["count"], "attributes": sorted(data["attributes"])}
                for event, data in sorted(events.items())
            },
        }
    return shape


def _ogg_info(path: Path) -> dict[str, Any]:
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        samples = sum(frame.samples for frame in container.decode(stream))
        info = {
            "codec": stream.codec_context.name,
            "channels": stream.channels,
            "sample_rate": stream.rate,
            "decoded_duration_s": round(samples / stream.rate, 6),
            "encoder": container.metadata.get("encoder") or stream.metadata.get("encoder"),
        }
    data = path.read_bytes()
    return info | {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _ground_truth(run: dict[str, Any]) -> dict[str, Any]:
    """Driver-side timing, in seconds from the recording's sample 0 (not in the OTLP)."""
    zero = int(run["recording"]["started_at_unix_nano"])

    def at(unix_nano: str | int | float) -> float:
        return round((int(unix_nano) - zero) / 1e9, 6)

    caller = at(run["caller_started_at_unix_nano"])
    return {
        "caller_wav_sample_0_s": caller,
        "caller_lines": [
            {
                "text": line["text"],
                "start_s": round(caller + line["start_s"], 6),
                "duration_s": line["duration_s"],
            }
            for line in run["caller_lines"]
        ],
        "agent_playback": [
            {
                "start_s": at(round(segment["started_at"] * 1e6) * 1000),
                "played_s": segment["played_s"],
                "interrupted": segment["interrupted"],
                "runs": [
                    [at(round(wall * 1e6) * 1000), round(offset, 6), round(seconds, 6)]
                    for wall, offset, seconds in segment["runs"]
                ],
            }
            for segment in run["speaker_segments"]
        ],
    }


def build(capture: Path, run_dir: Path, out: Path, env: dict[str, str]) -> dict[str, Any]:
    run = json.loads((run_dir / "manifest.json").read_text())
    if run.get("status") != "completed":
        raise SystemExit(f"{run_dir}/manifest.json is not a completed run")
    scrubber = Scrubber(env)
    if out.exists():
        shutil.rmtree(out)
    (out / "audio").mkdir(parents=True)

    requests, every_span, resource_keys = [], [], set()
    for index, source in enumerate(sorted(capture.glob("*.pb")), start=1):
        sidecar = json.loads(source.with_suffix(".json").read_text())
        request = ExportTraceServiceRequest()
        request.ParseFromString(source.read_bytes())
        scrubber.request(request)
        body = request.SerializeToString(deterministic=True)
        target = out / f"{index:03d}.pb"
        target.write_bytes(body)
        spans = _spans(request)
        every_span += [
            (scope, scope_version, span, target.name) for scope, scope_version, span in spans
        ]
        resource_keys.update(
            attribute.key
            for resource_spans in request.resource_spans
            for attribute in resource_spans.resource.attributes
        )
        requests.append(
            {
                "file": target.name,
                "sha256": hashlib.sha256(body).hexdigest(),
                "span_count": len(spans),
                "span_names": dict(sorted(Counter(span.name for _, _, span in spans).items())),
                "received_unix_nano": sidecar["received_unix_nano"],
                "wire": {
                    "content_type": sidecar["content_type"],
                    "content_encoding": sidecar["content_encoding"],
                    "size": sidecar["wire_size"],
                },
            }
        )

    spans = [(scope, scope_version, span) for scope, scope_version, span, _ in every_span]
    names = Counter(span.name for _, _, span in spans)
    if sum(names.values()) != run["exported_span_count"]:
        raise SystemExit(
            f"sink has {sum(names.values())} spans, the tee counted {run['exported_span_count']}"
        )
    markers = [
        (span, file) for scope, _, span, file in every_span if (scope, span.name) == END_MARKER
    ]
    if len(markers) != 1:
        raise SystemExit(f"expected one {END_MARKER} span, found {len(markers)}")
    marker, marker_file = markers[0]
    tail = sorted(
        (span for _, _, span in spans if span.end_time_unix_nano > marker.end_time_unix_nano),
        key=lambda span: span.end_time_unix_nano,
    )
    scopes: dict[str, Any] = {}
    for scope, scope_version, span in spans:
        entry = scopes.setdefault(scope, {"version": scope_version, "span_names": set()})
        entry["span_names"].add(span.name)

    shutil.copyfile(run_dir / "recording.ogg", out / "audio" / "recording.ogg")
    manifest = {
        "description": "OTLP/HTTP protobuf requests exported by one real LiveKit Agents call, "
        "in arrival order, plus that call's RecorderIO stereo recording.",
        "provenance": {
            "captured_on": datetime.date.today().isoformat(),
            "source_kind": "real_call",
            "session_key": run["session_key"],
            "session_key_attribute": "earshot.session.id",
            "mode": "headless Tier A: an in-process fake job "
            "(AgentServer.simulate_job(fake_job=True), THREAD executor, unregistered); "
            "no LiveKit server, no room, no WebRTC",
            "room_sid": run["room_sid"],
            "caller_script": run["caller_script"],
            "caller_audio": run["caller_audio"],
            "providers": run["providers"],
            "versions": run["versions"],
            "builder_versions": {
                package: version(package) for package in ("opentelemetry-proto", "protobuf", "av")
            },
            "exported_span_count_tee": run["exported_span_count"],
            "driver": "examples/livekit_otlp/run_call.py",
            "sink": "scripts/otlp_capture_sink.py",
            "builder": "scripts/build_livekit_otlp_fixture.py",
        },
        "scrub_rules": SCRUB_RULES,
        "requests": requests,
        "span_count": sum(names.values()),
        "span_names": dict(sorted(names.items())),
        "scopes": {
            scope: {"version": entry["version"], "span_names": sorted(entry["span_names"])}
            for scope, entry in sorted(scopes.items())
        },
        "resource_attribute_keys": sorted(resource_keys),
        "end_marker": {
            "scope": END_MARKER[0],
            "span": END_MARKER[1],
            "file": marker_file,
            "end_time_unix_nano": str(marker.end_time_unix_nano),
        },
        "after_end_marker": [
            {
                "span": span.name,
                "end_after_marker_ms": round(
                    (span.end_time_unix_nano - marker.end_time_unix_nano) / 1e6, 3
                ),
            }
            for span in tail
        ],
        "span_shapes": _shape(spans),
        "audio": {
            "file": "audio/recording.ogg",
            "channel_speakers": run["recording"]["channels"],
            "started_at_unix_nano": run["recording"]["started_at_unix_nano"],
            "started_at_source": "RecorderIO.recording_started_at (time.time() at "
            "RecorderIO.start(), microsecond precision)",
            "processing": "none: the bytes RecorderIO wrote",
            **_ogg_info(out / "audio" / "recording.ogg"),
        },
        "ground_truth": _ground_truth(run),
    }
    (out / "manifest.json").write_text(_prettier_json(manifest) + "\n")

    secrets = {k: v for k, v in env.items() if k not in NON_SECRET_ENV_KEYS and len(v) >= 8}
    for path in sorted(p for p in out.rglob("*") if p.is_file()):
        data = path.read_bytes()
        leaked = [key for key, value in secrets.items() if value.encode() in data]
        if leaked:
            shutil.rmtree(out)
            raise SystemExit(f"refusing fixture: {path.name} contains the value of {leaked}")
    return manifest


def _prettier_json(value: Any, indent: int = 0, lead: int = 0, trail: int = 0) -> str:
    """JSON laid out the way Prettier prints it, so the generated manifest passes
    ``format:check``: objects stay expanded; an array goes on one line when it fits."""
    pad = " " * indent
    if isinstance(value, dict) and value:
        rows = []
        for position, (key, item) in enumerate(value.items()):
            label, comma = json.dumps(key) + ": ", "," if position < len(value) - 1 else ""
            body = _prettier_json(item, indent + 2, indent + 2 + len(label), len(comma))
            rows.append(f"{pad}  {label}{body}{comma}")
        return "{\n" + "\n".join(rows) + f"\n{pad}}}"
    if isinstance(value, list) and value:
        nested = all(isinstance(item, dict | list) and len(item) > 1 for item in value)
        must_break = len(value) > 1 and nested and len({type(item) for item in value}) == 1
        if not must_break and not any(isinstance(item, dict) and item for item in value):
            flat = "[" + ", ".join(_prettier_json(item) for item in value) + "]"
            if "\n" not in flat and lead + len(flat) + trail <= PRETTIER_WIDTH:
                return flat
        rows = [
            f"{pad}  {_prettier_json(item, indent + 2, indent + 2, 1)}"
            + ("," if position < len(value) - 1 else "")
            for position, item in enumerate(value)
        ]
        return "[\n" + "\n".join(rows) + f"\n{pad}]"
    return json.dumps(value, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the scrubbed LiveKit OTLP fixture.")
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    manifest = build(args.capture, args.run, args.out, _env_values(args.env))
    print(f"{args.out}: {len(manifest['requests'])} requests, {manifest['span_count']} spans")
    return 0


if __name__ == "__main__":
    sys.exit(main())
