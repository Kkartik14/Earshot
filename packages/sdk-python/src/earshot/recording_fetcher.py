"""Fetch one provider recording over HTTPS, guarded against SSRF and credential leaks.

Standard library only. A URL a provider puts in a webhook is untrusted: anyone who
can sign a delivery (a leaked static secret, say) chooses it. So every hop of a
fetch, the first request and each redirect, is held to the same rules:

- **https on port 443**, an ASCII host and no userinfo; anything else is
  ``EARSHOT_FETCH_URL_INVALID``. A payload URL must also name a host its provider's
  allowlist names, on **every** hop (``EARSHOT_FETCH_HOST_NOT_ALLOWED``). A URL the
  caller built from an ID on a provider's own API host has no allowlist; its hops
  need only the address rule.
- **One lookup per hop, pinned.** The host is resolved once (the resolver); every
  answer must pass the address rule (``public_address``), or nothing is fetched
  (``EARSHOT_FETCH_ADDRESS_NOT_ALLOWED``); the connection goes to an answer of that
  same lookup, never to a second one, so a name that answers a public address and
  then a private one cannot be used to reach the private one.
- **The address rule** allows only ``is_global`` addresses that are not multicast,
  with any IPv4 address an IPv6 address embeds (IPv4-mapped, 6to4, Teredo, NAT64
  ``64:ff9b::/96``) judged again; IPv4-compatible ``::/96`` and local-use NAT64
  ``64:ff9b:1::/48`` are refused outright. It allows by property, never by a list
  of ranges to deny.
- **TLS** verifies the certificate chain and the URL's host name on the pinned
  connection (``server_hostname``); a context that does not is refused when the
  fetcher is built.
- **Headers.** Every request carries a fixed ``Accept`` and ``User-Agent``. A
  credential is an argument bound to one origin, never a header map: it is sent
  while the fetch is still on that origin, and once any redirect changes the
  origin no caller header is sent again, even back on the first origin.
- **At most three redirects** (``EARSHOT_FETCH_TOO_MANY_REDIRECTS``).
- **Audio only**: an ``audio/*`` type with no content encoding
  (``EARSHOT_FETCH_NOT_AUDIO``); the job also reads WAV, FLAC and Ogg headers.
- **Limits**: a declared length over the cap is refused before the body, and so is
  a body that streams past it (``EARSHOT_FETCH_TOO_LARGE``); 10 s to connect, 30 s
  without a byte, 10 minutes in all (``EARSHOT_FETCH_TIMEOUT``).
- ``*_PROXY`` variables are ignored: the fetcher opens its own sockets.

A ``FetchError`` names the host it failed on, never the URL: a payload URL is a
bearer capability (its query signs it), so it is never logged or shown.

**Seams.** The resolver and the SSL context are constructor arguments only; no
setting reaches them. For a test-mode replay of a hosted provider (a signed
delivery whose URL names the provider's real storage host), ``EARSHOT_TEST_MODE=1``
with ``EARSHOT_TEST_FETCH_ORIGIN=http://127.0.0.1:<port>`` sends every hop to that
loopback origin instead of resolving and connecting to the URL's host; the URL
rules, the allowlist and the header rules still apply. Outside test mode the
variable does nothing.
"""

from __future__ import annotations

import contextlib
import http.client
import ipaddress
import os
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from urllib.parse import SplitResult, urljoin, urlsplit

from .versions import PACKAGE_VERSION

HTTPS_PORT = 443
MAX_REDIRECTS = 3
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
ACCEPT = "audio/*"
USER_AGENT = f"earshot/{PACKAGE_VERSION} (recording fetch)"
READ_BYTES = 256 * 1024
MAX_HOST_LENGTH = 253

TEST_MODE = "EARSHOT_TEST_MODE"
TEST_FETCH_ORIGIN = "EARSHOT_TEST_FETCH_ORIGIN"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

# Stable error codes. ``retry``: the job tries again later; ``failed``: terminal;
# ``gone``: the URL no longer serves the recording (the job decides).
FETCH_URL_INVALID = "EARSHOT_FETCH_URL_INVALID"
FETCH_HOST_NOT_ALLOWED = "EARSHOT_FETCH_HOST_NOT_ALLOWED"
FETCH_ADDRESS_NOT_ALLOWED = "EARSHOT_FETCH_ADDRESS_NOT_ALLOWED"
FETCH_DNS_FAILED = "EARSHOT_FETCH_DNS_FAILED"
FETCH_CONNECT_FAILED = "EARSHOT_FETCH_CONNECT_FAILED"
FETCH_TLS_FAILED = "EARSHOT_FETCH_TLS_FAILED"
FETCH_TIMEOUT = "EARSHOT_FETCH_TIMEOUT"
FETCH_INTERRUPTED = "EARSHOT_FETCH_INTERRUPTED"
FETCH_TOO_MANY_REDIRECTS = "EARSHOT_FETCH_TOO_MANY_REDIRECTS"
FETCH_UPSTREAM_UNAVAILABLE = "EARSHOT_FETCH_UPSTREAM_UNAVAILABLE"
FETCH_GONE = "EARSHOT_FETCH_GONE"
FETCH_REFUSED = "EARSHOT_FETCH_REFUSED"
FETCH_NOT_AUDIO = "EARSHOT_FETCH_NOT_AUDIO"
FETCH_TOO_LARGE = "EARSHOT_FETCH_TOO_LARGE"

RETRY = "retry"
FAILED = "failed"
GONE = "gone"

_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
_LOCAL_USE_NAT64 = ipaddress.IPv6Network("64:ff9b:1::/48")
_IPV4_COMPATIBLE = ipaddress.IPv6Network("::/96")


class FetchError(Exception):
    """A fetch that did not get the recording: a stable code, what it means for the
    job (``retry``, ``failed`` or ``gone``) and the host it failed on, never a URL."""

    def __init__(self, code: str, outcome: str, host: str | None, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.outcome = outcome
        self.host = host


class FetchStopped(Exception):
    """The fetcher was closed (the server is stopping) while a fetch ran."""


@dataclass(frozen=True, slots=True)
class FetchLimits:
    max_bytes: int = 512 * 1024 * 1024
    connect_timeout_seconds: float = 10.0
    idle_timeout_seconds: float = 30.0
    total_timeout_seconds: float = 600.0

    def __post_init__(self) -> None:
        if self.max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        for name in ("connect_timeout_seconds", "idle_timeout_seconds", "total_timeout_seconds"):
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class ResolvedAddress:
    """One answer of a lookup: the address the address rule judges, and where the
    connection goes (``sockaddr``; for the system resolver, that address and the
    URL's port)."""

    address: str
    family: int
    sockaddr: tuple


Resolver = Callable[[str, int], Sequence[ResolvedAddress]]


def system_resolver(host: str, port: int) -> list[ResolvedAddress]:
    """The operating system's resolver: every TCP answer for ``host``."""

    return [
        ResolvedAddress(str(sockaddr[0]), family, sockaddr)
        for family, _, _, _, sockaddr in socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
    ]


def public_address(text: str) -> bool:
    """Whether a fetch may connect to ``text``: a global, non-multicast address,
    and so is every IPv4 address it embeds."""

    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    if not address.is_global or address.is_multicast:
        return False
    if isinstance(address, ipaddress.IPv4Address):
        return True
    if address in _IPV4_COMPATIBLE or address in _LOCAL_USE_NAT64:
        return False
    embedded: list[ipaddress.IPv4Address] = []
    if address.ipv4_mapped is not None:
        embedded.append(address.ipv4_mapped)
    if address.sixtofour is not None:
        embedded.append(address.sixtofour)
    if address.teredo is not None:
        embedded.extend(address.teredo)
    if address in _NAT64:
        embedded.append(ipaddress.IPv4Address(int(address) & 0xFFFF_FFFF))
    return all(public_address(str(inner)) for inner in embedded)


def _valid_label(label: str) -> bool:
    return (
        0 < len(label) <= 63
        and label[0] != "-"
        and label[-1] != "-"
        and all(character.isascii() and (character.isalnum() or character == "-")
                for character in label)
    )  # fmt: skip


class HostAllowlist:
    """Hosts a payload URL may name: exact hosts, or ``*.<domain>``, which matches
    one more label under a domain of at least two labels (``*.cdn.example.com``
    matches ``a.cdn.example.com``, not ``cdn.example.com`` or ``a.b.cdn.example.com``)."""

    def __init__(self, entries: Iterable[str]) -> None:
        exact: set[str] = set()
        wildcards: set[str] = set()
        for raw in entries:
            entry = raw.strip().lower() if isinstance(raw, str) else ""
            wildcard = entry.startswith("*.")
            name = entry[2:] if wildcard else entry
            labels = name.split(".")
            if (
                not name
                or len(name) > MAX_HOST_LENGTH
                or not all(_valid_label(label) for label in labels)
                or (wildcard and len(labels) < 2)
                or len(labels) < 2
            ):
                raise ValueError(f"not a host or a *.domain allowlist entry: {raw!r}")
            (wildcards if wildcard else exact).add(name)
        self._exact = frozenset(exact)
        self._wildcards = frozenset(wildcards)

    def allows(self, host: str) -> bool:
        if host in self._exact:
            return True
        first, _, rest = host.partition(".")
        return bool(rest) and _valid_label(first) and rest in self._wildcards

    @property
    def entries(self) -> tuple[str, ...]:
        return (*sorted(self._exact), *sorted(f"*.{name}" for name in self._wildcards))


@dataclass(frozen=True, slots=True)
class Credential:
    """A header sent only to ``https://<host>:443``, and only until the fetch first
    leaves that origin."""

    host: str
    header: str
    value: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class FetchRequest:
    """What to fetch. ``allowed_hosts`` is a payload URL's allowlist (every hop must
    match it); ``None`` marks a URL built on a provider's own API host."""

    url: str = field(repr=False)
    allowed_hosts: HostAllowlist | None = None
    credential: Credential | None = None


@dataclass(frozen=True, slots=True)
class _Hop:
    host: str
    target: str


def _hop(url: str, allowed_hosts: HostAllowlist | None) -> _Hop:
    """The checked host and request target of one hop's URL."""

    try:
        parts: SplitResult = urlsplit(url)
        port = parts.port
    except ValueError:
        raise FetchError(FETCH_URL_INVALID, FAILED, None, "unparseable URL") from None
    host = parts.hostname
    if parts.scheme != "https" or not host:
        raise FetchError(FETCH_URL_INVALID, FAILED, host, "not an https URL with a host")
    if port not in (None, HTTPS_PORT):
        raise FetchError(FETCH_URL_INVALID, FAILED, host, "only port 443 is fetched")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise FetchError(FETCH_URL_INVALID, FAILED, host, "a URL with userinfo is not fetched")
    if not host.isascii() or len(host) > MAX_HOST_LENGTH:
        raise FetchError(FETCH_URL_INVALID, FAILED, None, "the host is not an ASCII name")
    if allowed_hosts is not None and not allowed_hosts.allows(host):
        raise FetchError(FETCH_HOST_NOT_ALLOWED, FAILED, host, "not on the allowlist")
    target = parts.path or "/"
    if parts.query:
        target += f"?{parts.query}"
    return _Hop(host, target)


def fetch_test_origin() -> tuple[str, int] | None:
    """The loopback origin a test-mode replay sends every hop to, or ``None``."""

    if os.environ.get(TEST_MODE) != "1":
        return None
    raw = os.environ.get(TEST_FETCH_ORIGIN)
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        port = None
        parts = None
    if (
        parts is None
        or parts.scheme != "http"
        or parts.hostname not in _LOOPBACK_HOSTS
        or port is None
        or parts.path not in ("", "/")
        or parts.query
        or parts.username is not None
    ):
        raise ValueError(f"{TEST_FETCH_ORIGIN} must be a loopback origin, http://127.0.0.1:<port>")
    return parts.hostname, port


class _Connection(http.client.HTTPConnection):
    """One request on a socket the fetcher already opened (and checked)."""

    default_port = HTTPS_PORT

    def __init__(self, host: str, sock: socket.socket) -> None:
        super().__init__(host, HTTPS_PORT)
        self.sock = sock

    def connect(self) -> None:  # never connect again: the pinned socket is the only one
        raise http.client.NotConnected(self.host)


class FetchResponse:
    """An accepted response: audio of a known type, its body read by ``chunks``."""

    def __init__(
        self,
        fetcher: RecordingFetcher,
        response: http.client.HTTPResponse,
        sock: socket.socket,
        *,
        host: str,
        content_type: str,
        content_length: int | None,
        deadline: float,
    ) -> None:
        self._fetcher = fetcher
        self._response = response
        self._socket = sock
        self.host = host
        self.content_type = content_type
        self.content_length = content_length
        self._deadline = deadline

    def chunks(self) -> Iterator[bytes]:
        """The body, chunk by chunk, within the size cap and the timeouts."""

        limits = self._fetcher.limits
        received = 0
        while True:
            self._fetcher._check_open()
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise FetchError(FETCH_TIMEOUT, RETRY, self.host, "the fetch ran too long")
            try:
                self._socket.settimeout(min(limits.idle_timeout_seconds, remaining))
                chunk = self._response.read1(READ_BYTES)
            except TimeoutError:
                raise FetchError(FETCH_TIMEOUT, RETRY, self.host, "the body stalled") from None
            except (OSError, http.client.HTTPException, ValueError):
                self._fetcher._check_open()
                raise FetchError(FETCH_INTERRUPTED, RETRY, self.host, "the body broke") from None
            if not chunk:
                break
            received += len(chunk)
            if received > limits.max_bytes:
                raise FetchError(FETCH_TOO_LARGE, FAILED, self.host, "the body is too large")
            yield chunk
        self._fetcher._check_open()
        if self.content_length is not None and received != self.content_length:
            raise FetchError(FETCH_INTERRUPTED, RETRY, self.host, "the body ended early")


class RecordingFetcher:
    """Fetch recordings under the rules in this module's docstring."""

    def __init__(
        self,
        *,
        resolver: Resolver = system_resolver,
        ssl_context: ssl.SSLContext | None = None,
        limits: FetchLimits | None = None,
    ) -> None:
        context = ssl_context if ssl_context is not None else ssl.create_default_context()
        if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
            raise ValueError("a recording fetch must verify the certificate and its host name")
        self._resolve = resolver
        self._context = context
        self.limits = limits or FetchLimits()
        self._test_origin = fetch_test_origin()
        self._closing = threading.Event()
        self._sockets_lock = threading.Lock()
        self._sockets: set[socket.socket] = set()

    # --- one fetch ---------------------------------------------------------------------

    @contextlib.contextmanager
    def open(self, request: FetchRequest) -> Iterator[FetchResponse]:
        """Follow ``request`` to an accepted audio response (``FetchError`` otherwise)."""

        url = request.url
        credential = request.credential
        if credential is not None and _hop(url, None).host != credential.host:
            raise ValueError("a credential is bound to the origin of the first URL")
        deadline = time.monotonic() + self.limits.total_timeout_seconds
        origin: str | None = None
        for _ in range(MAX_REDIRECTS + 1):
            self._check_open()
            hop = _hop(url, request.allowed_hosts)
            if origin is not None and hop.host != origin:
                credential = None  # the origin changed: no caller header crosses, ever
            origin = hop.host
            sock = self._connect(hop.host, deadline)
            try:
                response = self._send(sock, hop, credential, deadline)
                if response.status in REDIRECT_STATUSES:
                    location = response.getheader("Location")
                    if not location:
                        raise FetchError(FETCH_URL_INVALID, FAILED, hop.host, "no Location")
                    url = urljoin(url, location.strip())
                    continue
                accepted = self._accept(response, hop.host, sock, deadline)
                yield accepted
                return
            finally:
                self._release(sock)
        raise FetchError(FETCH_TOO_MANY_REDIRECTS, FAILED, origin, "too many redirects")

    def close(self) -> None:
        """Stop every fetch in flight (their sockets are shut down) and any later one."""

        self._closing.set()
        with self._sockets_lock:
            sockets = list(self._sockets)
        for sock in sockets:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)

    # --- helpers -------------------------------------------------------------------------

    def _check_open(self) -> None:
        if self._closing.is_set():
            raise FetchStopped

    def _track(self, sock: socket.socket) -> None:
        with self._sockets_lock:
            self._sockets.add(sock)
        if self._closing.is_set():
            self._release(sock)
            raise FetchStopped

    def _release(self, sock: socket.socket) -> None:
        with self._sockets_lock:
            self._sockets.discard(sock)
        with contextlib.suppress(OSError):
            sock.close()

    def _remaining(self, deadline: float, host: str) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchError(FETCH_TIMEOUT, RETRY, host, "the fetch ran too long")
        return remaining

    def _connect(self, host: str, deadline: float) -> socket.socket:
        """A connected socket for ``host`` (TLS, pinned to one checked lookup), or to
        the test-mode origin."""

        timeout = min(self.limits.connect_timeout_seconds, self._remaining(deadline, host))
        if self._test_origin is not None:
            try:
                sock = socket.create_connection(self._test_origin, timeout=timeout)
            except OSError:
                raise FetchError(FETCH_CONNECT_FAILED, RETRY, host, "test origin") from None
            self._track(sock)
            return sock
        try:
            answers = list(self._resolve(host, HTTPS_PORT))
        except (OSError, UnicodeError):
            raise FetchError(FETCH_DNS_FAILED, RETRY, host, "the host does not resolve") from None
        if not answers:
            raise FetchError(FETCH_DNS_FAILED, RETRY, host, "the host does not resolve")
        if not all(public_address(answer.address) for answer in answers):
            raise FetchError(FETCH_ADDRESS_NOT_ALLOWED, FAILED, host, "not a public address")
        timed_out = False
        for answer in answers:
            raw = socket.socket(answer.family, socket.SOCK_STREAM)
            self._track(raw)
            try:
                raw.settimeout(timeout)
                raw.connect(answer.sockaddr)
            except TimeoutError:
                timed_out = True
                self._release(raw)
                continue
            except OSError:
                self._release(raw)
                self._check_open()
                continue
            break
        else:
            code = FETCH_TIMEOUT if timed_out else FETCH_CONNECT_FAILED
            raise FetchError(code, RETRY, host, "no answer accepted a connection")
        try:
            sock = self._context.wrap_socket(raw, server_hostname=host)
        except TimeoutError:
            self._release(raw)
            raise FetchError(FETCH_TIMEOUT, RETRY, host, "the TLS handshake stalled") from None
        except (ssl.SSLError, OSError):
            self._release(raw)
            self._check_open()
            raise FetchError(FETCH_TLS_FAILED, RETRY, host, "the TLS handshake failed") from None
        with self._sockets_lock:
            self._sockets.discard(raw)  # the TLS socket now owns the descriptor
        self._track(sock)
        return sock

    def _send(
        self,
        sock: socket.socket,
        hop: _Hop,
        credential: Credential | None,
        deadline: float,
    ) -> http.client.HTTPResponse:
        connection = _Connection(hop.host, sock)
        try:
            sock.settimeout(
                min(self.limits.idle_timeout_seconds, self._remaining(deadline, hop.host))
            )
            connection.putrequest("GET", hop.target, skip_accept_encoding=True)
            connection.putheader("Accept", ACCEPT)
            connection.putheader("User-Agent", USER_AGENT)
            if credential is not None:
                connection.putheader(credential.header, credential.value)
            connection.endheaders()
            return connection.getresponse()
        except TimeoutError:
            raise FetchError(FETCH_TIMEOUT, RETRY, hop.host, "no response") from None
        except (OSError, http.client.HTTPException, ValueError):
            self._check_open()
            raise FetchError(FETCH_INTERRUPTED, RETRY, hop.host, "the request broke") from None

    def _accept(
        self,
        response: http.client.HTTPResponse,
        host: str,
        sock: socket.socket,
        deadline: float,
    ) -> FetchResponse:
        status = response.status
        if status == 429 or 500 <= status <= 599:
            raise FetchError(FETCH_UPSTREAM_UNAVAILABLE, RETRY, host, f"status {status}")
        if status in (403, 404, 410):
            raise FetchError(FETCH_GONE, GONE, host, f"status {status}")
        if status != 200:
            raise FetchError(FETCH_REFUSED, FAILED, host, f"status {status}")
        content_type = (response.getheader("Content-Type") or "").split(";")[0].strip().lower()
        encoding = (response.getheader("Content-Encoding") or "").strip().lower()
        if not content_type.startswith("audio/") or encoding not in ("", "identity"):
            raise FetchError(FETCH_NOT_AUDIO, FAILED, host, "the response is not audio")
        declared = response.getheader("Content-Length")
        length: int | None = None
        if declared is not None and response.getheader("Transfer-Encoding") is None:
            try:
                length = int(declared)
            except ValueError:
                raise FetchError(FETCH_REFUSED, FAILED, host, "a malformed length") from None
            if length > self.limits.max_bytes:
                raise FetchError(FETCH_TOO_LARGE, FAILED, host, "the body is too large")
        return FetchResponse(
            self,
            response,
            sock,
            host=host,
            content_type=content_type,
            content_length=length,
            deadline=deadline,
        )
