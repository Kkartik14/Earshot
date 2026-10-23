"""Archive a real Pipecat OTLP capture in the ignored ``.earshot`` directory.

    python scripts/archive_pipecat_otlp_capture.py \\
        --capture .earshot/otlp_capture/run1 --run .earshot/pipecat_otlp/run1

``--capture`` is the output of ``scripts/otlp_capture_sink.py``; ``--run`` is the
``--out`` of ``examples/pipecat_otlp/run_call.py``. The archive retains transcript
and audio content, so its destination stays under the gitignored ``.earshot`` path.
Every request is decoded, scrubbed and re-serialized deterministically. The command
refuses to write an archive that still contains a non-model ``--env`` value of
at least eight characters (it reports key names, never values). The output must
be a new, direct child of ``.earshot/private-captures``.
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import hashlib
import json
import platform
import re
import shutil
import socket
import sys
import wave
from collections import Counter
from importlib.metadata import version
from pathlib import Path

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / ".earshot" / "private-captures" / "pipecat-otlp-1.5"
NON_SECRET_ENV_KEYS = {"GROQ_MODEL", "CARTESIA_MODEL"}
SCRUB_RULES = [
    "Resource attribute service.instance.id is replaced with 'captured-instance'.",
    "Resource attributes under host.* and process.* (if any) become 'captured-<key>'.",
    "Any string value equal to CARTESIA_VOICE_ID (tts voice_id, settings.voice) becomes "
    "'captured-cartesia-voice'.",
    "The local username and hostname are replaced with 'captured-user' / 'captured-host' "
    "wherever they occur inside string values.",
    "Each request is decompressed and re-serialized with SerializeToString(deterministic=True).",
    "The build aborts if a non-model .env value of at least eight characters (except GROQ_MODEL "
    "and CARTESIA_MODEL, which Pipecat records as gen_ai.request.model) occurs anywhere in the "
    "fixture bytes.",
    "Trace/span IDs, timestamps, transcripts and LLM/TTS text are kept as captured: the caller "
    "is a fixed synthetic script and the agent text is model output.",
]


def _env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines() if path.exists() else []:
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if match:
            values[match.group(1)] = match.group(2).strip().strip("'\"")
    return values


def _scrub_value(any_value, replacements: dict[str, str], exact: dict[str, str]) -> None:
    if any_value.WhichOneof("value") == "string_value":
        text = exact.get(any_value.string_value, any_value.string_value)
        for needle, placeholder in replacements.items():
            text = text.replace(needle, placeholder)
        any_value.string_value = text


def _scrub(request: ExportTraceServiceRequest, replacements, exact) -> None:
    for resource_spans in request.resource_spans:
        for attribute in resource_spans.resource.attributes:
            if attribute.key == "service.instance.id":
                attribute.value.string_value = "captured-instance"
            elif attribute.key.startswith(("host.", "process.")):
                attribute.value.string_value = f"captured-{attribute.key}"
            _scrub_value(attribute.value, replacements, exact)
        for scope_spans in resource_spans.scope_spans:
            for attribute in scope_spans.scope.attributes:
                _scrub_value(attribute.value, replacements, exact)
            for span in scope_spans.spans:
                attributes = list(span.attributes)
                attributes += [a for event in span.events for a in event.attributes]
                attributes += [a for link in span.links for a in link.attributes]
                for attribute in attributes:
                    _scrub_value(attribute.value, replacements, exact)


def _wav_info(path: Path) -> dict[str, object]:
    with wave.open(str(path), "rb") as wav:
        assert (wav.getnchannels(), wav.getsampwidth()) == (1, 2), path
        return {
            "sample_rate": wav.getframerate(),
            "channels": 1,
            "duration_s": round(wav.getnframes() / wav.getframerate(), 3),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }


def _archive_output(path: Path) -> Path:
    archive_root = (ROOT / ".earshot" / "private-captures").resolve()
    output = path.expanduser().resolve()
    if output.parent != archive_root:
        raise SystemExit(
            "refusing archive: --out must be a direct child of .earshot/private-captures"
        )
    if output.exists():
        raise SystemExit(f"refusing archive: output already exists: {output}")
    return output


def build(capture: Path, run: Path, out: Path, env: dict[str, str]) -> dict[str, object]:
    out = _archive_output(out)
    run_manifest = json.loads((run / "manifest.json").read_text())
    voice = env.get("CARTESIA_VOICE_ID")
    exact = {voice: "captured-cartesia-voice"} if voice else {}
    replacements = {
        needle: placeholder
        for needle, placeholder in (
            (getpass.getuser(), "captured-user"),
            (socket.gethostname(), "captured-host"),
        )
        if len(needle) >= 3
    }
    (out / "audio").mkdir(parents=True)

    requests, names, scopes = [], Counter(), {}
    for index, source in enumerate(sorted(capture.glob("*.pb")), start=1):
        sidecar = json.loads(source.with_suffix(".json").read_text())
        request = ExportTraceServiceRequest()
        request.ParseFromString(source.read_bytes())
        _scrub(request, replacements, exact)
        body = request.SerializeToString(deterministic=True)
        target = out / f"{index:03d}.pb"
        target.write_bytes(body)
        spans = [
            (scope_spans.scope.name, span.name)
            for resource_spans in request.resource_spans
            for scope_spans in resource_spans.scope_spans
            for span in scope_spans.spans
        ]
        names.update(name for _, name in spans)
        for scope, name in spans:
            scopes.setdefault(scope, set()).add(name)
        requests.append(
            {
                "file": target.name,
                "sha256": hashlib.sha256(body).hexdigest(),
                "span_count": len(spans),
                "span_names": dict(sorted(Counter(name for _, name in spans).items())),
                "received_unix_nano": sidecar["received_unix_nano"],
                "wire": {
                    "content_type": sidecar["content_type"],
                    "content_encoding": sidecar["content_encoding"],
                    "size": sidecar["wire_size"],
                },
            }
        )

    for track in ("user", "agent"):
        shutil.copyfile(run / f"{track}.wav", out / "audio" / f"{track}.wav")
    manifest = {
        "description": "Local archive of OTLP/HTTP requests and recordings from one real "
        "Pipecat call. This archive can contain transcript and audio content.",
        "provenance": {
            "captured_on": datetime.date.today().isoformat(),
            "source_kind": "real_call",
            "conversation_id": run_manifest["conversation_id"],
            "caller_script": run_manifest["caller_script"],
            "providers": run_manifest["providers"],
            "versions": {
                package: version(package)
                for package in (
                    "pipecat-ai",
                    "opentelemetry-sdk",
                    "opentelemetry-exporter-otlp-proto-http",
                    "opentelemetry-proto",
                    "deepgram-sdk",
                )
            }
            | {"python": platform.python_version()},
            "exported_span_count_tee": run_manifest["exported_span_count"],
            "driver": "examples/pipecat_otlp/run_call.py",
            "sink": "scripts/otlp_capture_sink.py",
            "builder": "scripts/archive_pipecat_otlp_capture.py",
        },
        "scrub_rules": SCRUB_RULES,
        "requests": requests,
        "span_count": sum(names.values()),
        "span_names": dict(sorted(names.items())),
        "scopes": {scope: sorted(found) for scope, found in sorted(scopes.items())},
        "audio": {
            "started_at_unix_nano": run_manifest["started_at_unix_nano"],
            "processing": "none: tracks are written exactly as AudioBufferProcessor emitted them",
            "user": {"file": "audio/user.wav", **_wav_info(out / "audio" / "user.wav")},
            "agent": {"file": "audio/agent.wav", **_wav_info(out / "audio" / "agent.wav")},
        },
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    secrets = {k: v for k, v in env.items() if k not in NON_SECRET_ENV_KEYS and len(v) >= 8}
    for path in sorted(p for p in out.rglob("*") if p.is_file()):
        data = path.read_bytes()
        leaked = [key for key, value in secrets.items() if value.encode() in data]
        if leaked:
            shutil.rmtree(out)
            raise SystemExit(f"refusing archive: {path.name} contains the value of {leaked}")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive a scrubbed Pipecat OTLP capture locally.")
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--env", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    manifest = build(args.capture, args.run, args.out, _env_values(args.env))
    print(f"{args.out}: {len(manifest['requests'])} requests, {manifest['span_names']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
