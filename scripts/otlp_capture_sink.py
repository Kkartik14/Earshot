"""Record raw OTLP/HTTP trace exports to disk, in arrival order. Stdlib only.

    python scripts/otlp_capture_sink.py --out .earshot/otlp_capture --port 4318

Each ``POST /v1/traces`` body is decompressed (gzip/deflate) and written as
``NNN.pb`` plus an ``NNN.json`` sidecar; the reply is 200 with an empty protobuf body.
"""

from __future__ import annotations

import argparse
import gzip
import itertools
import json
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _decode(body: bytes, encoding: str) -> bytes:
    if encoding == "gzip":
        return gzip.decompress(body)
    if encoding == "deflate":
        try:
            return zlib.decompress(body)
        except zlib.error:
            return zlib.decompress(body, -zlib.MAX_WBITS)
    return body


def serve(out: Path, host: str, port: int) -> None:
    out.mkdir(parents=True, exist_ok=True)
    counter, lock = itertools.count(1), threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path.split("?", 1)[0] != "/v1/traces":
                self.send_error(404)
                return
            received = time.time_ns()
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            encoding = (self.headers.get("Content-Encoding") or "identity").strip().lower()
            body = _decode(raw, encoding)
            with lock:
                stem = out / f"{next(counter):03d}"
                stem.with_suffix(".pb").write_bytes(body)
                sidecar = {
                    "content_type": self.headers.get("Content-Type"),
                    "content_encoding": encoding,
                    "wire_size": len(raw),
                    "size": len(body),
                    "received_unix_nano": str(received),
                }
                stem.with_suffix(".json").write_text(json.dumps(sidecar, indent=2) + "\n")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.send_header("Content-Length", "0")
            self.end_headers()

    print(f"otlp capture sink: http://{host}:{port}/v1/traces -> {out}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path(".earshot/otlp_capture"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4318)
    args = parser.parse_args()
    serve(args.out, args.host, args.port)
