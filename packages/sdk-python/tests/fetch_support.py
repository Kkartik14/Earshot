"""Recording-fetch tests: a test CA, a real local HTTPS server, and DNS for the fetcher.

``TestCertificates`` makes a CA and one server certificate (``fetch.test``,
``other.test``, ``api.vapi.ai``, ``api.elevenlabs.io``, …) with ``cryptography``.
Only a test's own ``ssl.SSLContext`` trusts the CA; it reaches the fetcher through
its constructor, the declared trust-store seam.

``AudioServer`` is a real threaded HTTP server on 127.0.0.1 (TLS with the test
certificate, or plain HTTP for the test-mode origin override). Each route answers
from a ``Reply`` (status, headers, body chunks, an optional hold before the body);
every request it receives is recorded with its ``Host`` and headers.

``MappedResolver`` is DNS for the fetcher, the declared resolver seam: it answers
each name with the address the address rule judges and where the connection goes
(the test server). A name with several answers gets them in order, one per lookup.
"""

from __future__ import annotations

import datetime
import http.server
import ipaddress
import socket
import ssl
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from earshot.recording_fetcher import ResolvedAddress

# What a test's resolver says ``fetch.test`` and its peers are: a public address,
# judged by the address rule as any answer is. The connection goes to the server.
PUBLIC_ADDRESS = "93.184.216.34"
OTHER_PUBLIC_ADDRESS = "93.184.216.35"
SERVER_NAMES = (
    "fetch.test",
    "other.test",
    "third.test",
    "a.cdn.test",
    "api.vapi.ai",
    "api.elevenlabs.io",
    "storage.vapi.ai",
    "retellai.s3.us-west-2.amazonaws.com",
)


@dataclass(frozen=True)
class TestCertificates:
    """A test CA and a server certificate it signed, as PEM files."""

    ca: Path
    certificate: Path
    key: Path

    def client_context(self) -> ssl.SSLContext:
        """A verifying client context that trusts only the test CA."""

        context = ssl.create_default_context(cafile=str(self.ca))
        return context

    def server_context(self) -> ssl.SSLContext:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.certificate), str(self.key))
        return context


def make_certificates(directory: Path, names: Sequence[str] = SERVER_NAMES) -> TestCertificates:
    """A CA and a server certificate for ``names``, written under ``directory``."""

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.UTC)
    validity = (now - datetime.timedelta(days=1), now + datetime.timedelta(days=2))

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Earshot fetch test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(validity[0])
        .not_valid_after(validity[1])
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    server_key = ec.generate_private_key(ec.SECP256R1())
    server = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(validity[0])
        .not_valid_after(validity[1])
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in names]), critical=False
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    paths = TestCertificates(
        directory / "ca.pem", directory / "server.pem", directory / "server-key.pem"
    )
    paths.ca.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    paths.certificate.write_bytes(server.public_bytes(serialization.Encoding.PEM))
    paths.key.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return paths


@dataclass
class Reply:
    """One answer: status, headers and body chunks. ``hold`` (when set) is waited
    on after the headers are sent and before the body; ``stall`` sends the headers
    and then nothing until the client goes away."""

    status: int = 200
    headers: Sequence[tuple[str, str]] = (("Content-Type", "audio/wav"),)
    body: Iterable[bytes] = ()
    chunked: bool = False
    hold: threading.Event | None = None
    held: threading.Event = field(default_factory=threading.Event)
    stall: bool = False
    delay_between_chunks: float = 0.0


@dataclass(frozen=True)
class SeenRequest:
    host: str
    path: str
    headers: dict[str, str]


class AudioServer:
    """A real HTTP(S) server on 127.0.0.1 answering ``(host, path)`` routes."""

    def __init__(self, certificates: TestCertificates | None) -> None:
        self.routes: dict[tuple[str, str], Reply | Callable[[], Reply]] = {}
        self.requests: list[SeenRequest] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:  # quiet
                return

            def do_GET(self) -> None:
                host = (self.headers.get("Host") or "").split(":")[0]
                seen = SeenRequest(
                    host, self.path, {name.lower(): value for name, value in self.headers.items()}
                )
                with owner._lock:
                    owner.requests.append(seen)
                route = owner.routes.get((host, self.path.split("?")[0]))
                if route is None:
                    reply = Reply(404, (("Content-Type", "text/plain"),), [b"no route"])
                else:
                    reply = route() if callable(route) else route
                self._answer(reply)

            def _answer(self, reply: Reply) -> None:
                chunks = [bytes(chunk) for chunk in reply.body]
                self.send_response(reply.status)
                for name, value in reply.headers:
                    self.send_header(name, value)
                if reply.chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                elif not any(name.lower() == "content-length" for name, _ in reply.headers):
                    self.send_header("Content-Length", str(sum(map(len, chunks))))
                self.end_headers()
                self.wfile.flush()
                reply.held.set()
                if reply.stall:
                    owner._stop.wait(30)
                    return
                if reply.hold is not None:
                    reply.hold.wait(30)
                for chunk in chunks:
                    if reply.delay_between_chunks:
                        owner._stop.wait(reply.delay_between_chunks)
                    try:
                        if reply.chunked:
                            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                        else:
                            self.wfile.write(chunk)
                        self.wfile.flush()
                    except OSError:
                        return
                if reply.chunked:
                    self.wfile.write(b"0\r\n\r\n")
                self.close_connection = True

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address) -> None:  # quiet TLS refusals
                return

        self._server = Server(("127.0.0.1", 0), Handler)
        if certificates is not None:
            self._server.socket = certificates.server_context().wrap_socket(
                self._server.socket, server_side=True
            )
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )

    def __enter__(self) -> AudioServer:
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)

    def route(self, host: str, path: str, reply: Reply | Callable[[], Reply]) -> None:
        self.routes[(host, path)] = reply

    def seen(self, host: str | None = None) -> list[SeenRequest]:
        with self._lock:
            return [request for request in self.requests if host is None or request.host == host]


class MappedResolver:
    """DNS for the fetcher: each name answers its next listed answer (the last
    one repeats), counting lookups. An unknown name does not resolve."""

    def __init__(self, answers: dict[str, list[list[ResolvedAddress]]]) -> None:
        self.answers = answers
        self.lookups: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> list[ResolvedAddress]:
        self.lookups.append((host, port))
        listed = self.answers.get(host)
        if not listed:
            raise socket.gaierror(socket.EAI_NONAME, "name does not resolve")
        index = sum(1 for name, _ in self.lookups if name == host) - 1
        return listed[min(index, len(listed) - 1)]


def answer(address: str, server_port: int) -> ResolvedAddress:
    """An answer naming ``address`` (any family) whose connection goes to the test
    server on 127.0.0.1:``server_port``."""

    ipaddress.ip_address(address)  # a test names a real address
    return ResolvedAddress(address, socket.AF_INET, ("127.0.0.1", server_port))


def resolver_for(server: AudioServer, names: Sequence[str] = SERVER_NAMES) -> MappedResolver:
    """Every name in ``names`` answers one public address, connected to ``server``."""

    return MappedResolver({name: [[answer(PUBLIC_ADDRESS, server.port)]] for name in names})


def wav_bytes(seconds: float = 0.5, *, rate: int = 8_000, channels: int = 1) -> bytes:
    """A PCM16 WAV of silence: ``seconds`` long."""

    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(b"\x00\x00" * channels * int(seconds * rate))
    return buffer.getvalue()
