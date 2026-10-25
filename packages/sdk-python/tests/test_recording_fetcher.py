"""The recording fetcher's defences, each against a real local HTTPS server.

The server presents a certificate from a test CA that only these tests' own SSL
context trusts, and ``MappedResolver`` (the declared resolver seam) maps the test
names to it as public addresses. Everything else is the real guard: https on port
443 only, the address rule on every answer, one lookup per hop, certificate and
host-name verification, the host allowlist on every hop of a payload URL, at most
three redirects, no caller header across an origin change, audio only, the size
cap and the timeouts.
"""

from __future__ import annotations

import socket
import ssl
import threading
import time

import pytest

from earshot.recording_fetcher import (
    FETCH_ADDRESS_NOT_ALLOWED,
    FETCH_GONE,
    FETCH_HOST_NOT_ALLOWED,
    FETCH_NOT_AUDIO,
    FETCH_REFUSED,
    FETCH_TIMEOUT,
    FETCH_TLS_FAILED,
    FETCH_TOO_LARGE,
    FETCH_TOO_MANY_REDIRECTS,
    FETCH_UPSTREAM_UNAVAILABLE,
    FETCH_URL_INVALID,
    TEST_FETCH_ORIGIN,
    Credential,
    FetchError,
    FetchLimits,
    FetchRequest,
    FetchStopped,
    HostAllowlist,
    RecordingFetcher,
    public_address,
)
from fetch_support import (
    PUBLIC_ADDRESS,
    AudioServer,
    MappedResolver,
    Reply,
    answer,
    make_certificates,
    resolver_for,
    wav_bytes,
)

AUDIO = wav_bytes()
FIXED_HEADERS = {"host", "accept", "user-agent"}
# Provider credential header names: none may reach another origin.
PROVIDER_HEADERS = ("authorization", "xi-api-key", "x-api-key", "x-vapi-secret")
ANYWHERE = None  # a fixed provider API URL: its hops need only the address rule


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    return make_certificates(tmp_path_factory.mktemp("fetch-ca"))


@pytest.fixture
def server(certificates):
    with AudioServer(certificates) as running:
        yield running


@pytest.fixture
def fetcher(server, certificates):
    return RecordingFetcher(
        resolver=resolver_for(server), ssl_context=certificates.client_context()
    )


def _audio(**changes) -> Reply:
    return Reply(body=[AUDIO], **changes)


def _redirect(location: str, status: int = 302) -> Reply:
    return Reply(status, (("Location", location), ("Content-Type", "text/plain")), [b""])


def _fetch(fetcher: RecordingFetcher, url: str, allowed=ANYWHERE, credential=None) -> bytes:
    request = FetchRequest(url, allowed_hosts=allowed, credential=credential)
    with fetcher.open(request) as response:
        return b"".join(response.chunks())


def _refused(fetcher, url, allowed=ANYWHERE, credential=None) -> FetchError:
    with pytest.raises(FetchError) as caught:
        _fetch(fetcher, url, allowed, credential)
    return caught.value


# --- the success path, and what it sends ----------------------------------------------


def test_a_verified_https_fetch_returns_the_audio_with_only_fixed_headers(server, fetcher):
    server.route("fetch.test", "/a.wav", _audio())

    request = FetchRequest("https://fetch.test/a.wav?X-Amz-Signature=abc", allowed_hosts=ANYWHERE)
    with fetcher.open(request) as response:
        body = b"".join(response.chunks())
        assert (response.content_type, response.host) == ("audio/wav", "fetch.test")

    assert body == AUDIO
    [seen] = server.seen()
    assert (seen.host, seen.path) == ("fetch.test", "/a.wav?X-Amz-Signature=abc")
    assert set(seen.headers) == FIXED_HEADERS
    assert seen.headers["accept"] == "audio/*"


def test_proxy_variables_are_ignored(server, fetcher, monkeypatch):
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "HTTP_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    server.route("fetch.test", "/a.wav", _audio())

    assert _fetch(fetcher, "https://fetch.test/a.wav") == AUDIO


# --- URLs: https, port 443, no userinfo -------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://fetch.test/a.wav",
        "https://fetch.test:8443/a.wav",
        "https://user:secret@fetch.test/a.wav",
        "ftp://fetch.test/a.wav",
        "https:///a.wav",
        "/a.wav",
        "https://fetch.tést/a.wav",
    ],
)
def test_only_an_https_url_on_port_443_without_userinfo_is_fetched(server, fetcher, url):
    server.route("fetch.test", "/a.wav", _audio())

    assert _refused(fetcher, url).code == FETCH_URL_INVALID
    assert server.seen() == []


def test_a_redirect_to_plain_http_is_refused(server, fetcher):
    server.route("fetch.test", "/a.wav", _redirect("http://fetch.test/b.wav"))

    error = _refused(fetcher, "https://fetch.test/a.wav")

    assert error.code == FETCH_URL_INVALID
    assert len(server.seen()) == 1


def test_at_most_three_redirects_are_followed(server, fetcher):
    for hop in range(4):
        server.route("fetch.test", f"/{hop}", _redirect(f"/{hop + 1}"))
    server.route("fetch.test", "/4", _audio())

    assert _refused(fetcher, "https://fetch.test/0").code == FETCH_TOO_MANY_REDIRECTS
    assert [seen.path for seen in server.seen()] == ["/0", "/1", "/2", "/3"]
    server.requests.clear()
    assert _fetch(fetcher, "https://fetch.test/1") == AUDIO  # three redirects, then audio


# --- the host allowlist (payload URLs) ---------------------------------------------------


def test_a_payload_url_must_name_an_allowed_host(server, fetcher):
    server.route("other.test", "/a.wav", _audio())

    error = _refused(fetcher, "https://other.test/a.wav", HostAllowlist(["fetch.test"]))

    assert (error.code, error.host) == (FETCH_HOST_NOT_ALLOWED, "other.test")
    assert server.seen() == []


def test_every_hop_of_a_payload_url_must_name_an_allowed_host(server, fetcher):
    server.route("fetch.test", "/a.wav", _redirect("https://other.test/a.wav"))
    server.route("other.test", "/a.wav", _audio())

    error = _refused(fetcher, "https://fetch.test/a.wav", HostAllowlist(["fetch.test"]))

    assert (error.code, error.host) == (FETCH_HOST_NOT_ALLOWED, "other.test")
    assert [seen.host for seen in server.seen()] == ["fetch.test"]
    server.requests.clear()
    allowed = HostAllowlist(["fetch.test", "*.cdn.test", "other.test"])
    assert _fetch(fetcher, "https://fetch.test/a.wav", allowed) == AUDIO


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("storage.vapi.ai", True),
        ("STORAGE.vapi.ai", False),  # hosts arrive lowercased; entries match exactly
        ("a.cdn.test", True),
        ("cdn.test", False),
        ("a.b.cdn.test", False),
        ("evilstorage.vapi.ai", False),
        ("storage.vapi.ai.evil.test", False),
    ],
)
def test_allowlist_entries_are_exact_hosts_or_one_leftmost_label_wildcard(host, allowed):
    assert HostAllowlist(["storage.vapi.ai", "*.cdn.test"]).allows(host) is allowed


@pytest.mark.parametrize(
    "entry", ["*.test", "*", "a.*.test", "**.cdn.test", "cdn.*", "", "a b.test", "-a.test"]
)
def test_an_allowlist_entry_must_be_a_host_or_a_wildcard_under_a_domain(entry):
    with pytest.raises(ValueError):
        HostAllowlist([entry])


# --- credentials never cross an origin -------------------------------------------------


def test_a_fixed_api_redirect_needs_only_the_address_rule_and_drops_every_header(server, fetcher):
    server.route("api.vapi.ai", "/call/c1/mono-recording", _redirect("https://other.test/s?sig=1"))
    server.route("other.test", "/s", _audio())

    body = _fetch(
        fetcher,
        "https://api.vapi.ai/call/c1/mono-recording",
        credential=Credential("api.vapi.ai", "Authorization", "Bearer vapi-key"),
    )

    assert body == AUDIO
    api, signed = server.seen()
    assert api.headers["authorization"] == "Bearer vapi-key"
    assert set(signed.headers) == FIXED_HEADERS
    assert not set(signed.headers) & set(PROVIDER_HEADERS)


@pytest.mark.parametrize("header", ["xi-api-key", "X-API-KEY", "Authorization"])
def test_a_credential_is_never_attached_again_after_an_origin_change(server, fetcher, header):
    server.route("fetch.test", "/a", _redirect("https://other.test/b"))
    server.route("other.test", "/b", _redirect("https://fetch.test/c"))
    server.route("fetch.test", "/c", _audio())

    _fetch(fetcher, "https://fetch.test/a", credential=Credential("fetch.test", header, "k"))

    first, second, third = server.seen()
    assert first.headers[header.lower()] == "k"
    assert set(second.headers) == FIXED_HEADERS
    assert set(third.headers) == FIXED_HEADERS


def test_a_same_origin_redirect_keeps_the_credential(server, fetcher):
    server.route("fetch.test", "/a", _redirect("/b"))
    server.route("fetch.test", "/b", _audio())

    _fetch(fetcher, "https://fetch.test/a", credential=Credential("fetch.test", "xi-api-key", "k"))

    assert [seen.headers.get("xi-api-key") for seen in server.seen()] == ["k", "k"]


def test_a_credential_is_bound_to_the_first_origin(fetcher):
    with pytest.raises(ValueError):
        _fetch(fetcher, "https://fetch.test/a", credential=Credential("other.test", "X", "k"))


# --- the address rule ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "allowed"),
    [
        ("93.184.216.34", True),
        ("2606:4700::1111", True),
        ("127.0.0.1", False),  # loopback
        ("::1", False),
        ("10.1.2.3", False),  # RFC 1918
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),  # link-local, cloud metadata
        ("fe80::1", False),
        ("fd00:ec2::254", False),  # unique local, the EC2 IPv6 metadata address
        ("100.64.0.1", False),  # CGNAT (Tailscale peers)
        ("0.0.0.0", False),
        ("255.255.255.255", False),
        ("224.0.0.1", False),  # multicast
        ("ff02::1", False),
        ("192.0.2.1", False),  # documentation
        ("198.18.0.1", False),  # benchmarking
        ("240.0.0.1", False),  # reserved
        ("::ffff:127.0.0.1", False),  # IPv4-mapped loopback
        ("::ffff:93.184.216.34", True),
        ("::7f00:1", False),  # IPv4-compatible loopback (deprecated, still routable)
        ("64:ff9b::a9fe:a9fe", False),  # NAT64 of the metadata address
        ("64:ff9b::a01:203", False),  # NAT64 of 10.1.2.3
        ("64:ff9b::5db8:d822", True),  # NAT64 of a public address
        ("64:ff9b:1::a9fe:a9fe", False),  # local-use NAT64
        ("2002:7f00:1::", False),  # 6to4 of loopback
        ("2002:a01:203::", False),  # 6to4 of 10.1.2.3
        ("2001:0:4136:e378:8000:63bf:3fff:fdd2", False),  # Teredo
        ("not an address", False),
    ],
)
def test_the_address_rule_allows_only_global_addresses(address, allowed):
    assert public_address(address) is allowed


def test_every_answer_must_be_public_or_nothing_is_fetched(server, certificates):
    server.route("fetch.test", "/a.wav", _audio())
    resolver = MappedResolver(
        {"fetch.test": [[answer(PUBLIC_ADDRESS, server.port), answer("10.0.0.5", server.port)]]}
    )
    fetcher = RecordingFetcher(resolver=resolver, ssl_context=certificates.client_context())

    error = _refused(fetcher, "https://fetch.test/a.wav")

    assert (error.code, error.host) == (FETCH_ADDRESS_NOT_ALLOWED, "fetch.test")
    assert server.seen() == []


def test_a_name_the_system_resolver_maps_to_loopback_is_refused(certificates):
    fetcher = RecordingFetcher(ssl_context=certificates.client_context())

    assert _refused(fetcher, "https://localhost/a.wav").code == FETCH_ADDRESS_NOT_ALLOWED


def test_a_rebinding_answer_is_never_used(server, certificates):
    """One lookup per hop, and the connection goes to the address the rule judged:
    a name that answers a public address and then loopback is fetched from the first
    answer and never looked up again."""

    with AudioServer(certificates) as other:
        server.route("fetch.test", "/a.wav", _audio())
        other.route("fetch.test", "/a.wav", _audio())
        resolver = MappedResolver(
            {
                "fetch.test": [
                    [answer(PUBLIC_ADDRESS, server.port)],
                    [answer("127.0.0.1", other.port)],
                ]
            }
        )
        fetcher = RecordingFetcher(resolver=resolver, ssl_context=certificates.client_context())

        assert _fetch(fetcher, "https://fetch.test/a.wav") == AUDIO
        assert resolver.lookups == [("fetch.test", 443)]
        assert (len(server.seen()), other.seen()) == (1, [])


def test_every_hop_is_looked_up_and_judged_again(server, certificates):
    server.route("fetch.test", "/a", _redirect("/b"))
    server.route("fetch.test", "/b", _audio())
    resolver = MappedResolver(
        {"fetch.test": [[answer(PUBLIC_ADDRESS, server.port)], [answer("127.0.0.1", server.port)]]}
    )
    fetcher = RecordingFetcher(resolver=resolver, ssl_context=certificates.client_context())

    assert _refused(fetcher, "https://fetch.test/a").code == FETCH_ADDRESS_NOT_ALLOWED
    assert resolver.lookups == [("fetch.test", 443), ("fetch.test", 443)]
    assert [seen.path for seen in server.seen()] == ["/a"]


# --- TLS ---------------------------------------------------------------------------------


def test_a_certificate_the_trust_store_does_not_trust_is_refused(server):
    server.route("fetch.test", "/a.wav", _audio())
    fetcher = RecordingFetcher(resolver=resolver_for(server))  # the system trust store

    assert _refused(fetcher, "https://fetch.test/a.wav").code == FETCH_TLS_FAILED
    assert server.seen() == []


def test_a_certificate_for_another_name_is_refused(server, certificates):
    resolver = MappedResolver({"wrong.test": [[answer(PUBLIC_ADDRESS, server.port)]]})
    fetcher = RecordingFetcher(resolver=resolver, ssl_context=certificates.client_context())

    assert _refused(fetcher, "https://wrong.test/a.wav").code == FETCH_TLS_FAILED


def test_a_context_that_does_not_verify_is_refused(certificates):
    unverified = certificates.client_context()
    unverified.check_hostname = False
    with pytest.raises(ValueError):
        RecordingFetcher(ssl_context=unverified)
    unverified.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError):
        RecordingFetcher(ssl_context=unverified)


# --- what comes back -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        (("Content-Type", "text/html"),),
        (("Content-Type", "application/octet-stream"),),
        (),
        (("Content-Type", "audio/wav"), ("Content-Encoding", "gzip")),
    ],
)
def test_only_audio_is_accepted(server, fetcher, headers):
    server.route("fetch.test", "/a.wav", Reply(headers=headers, body=[AUDIO]))

    assert _refused(fetcher, "https://fetch.test/a.wav").code == FETCH_NOT_AUDIO


@pytest.mark.parametrize(
    ("status", "code", "outcome"),
    [
        (503, FETCH_UPSTREAM_UNAVAILABLE, "retry"),
        (429, FETCH_UPSTREAM_UNAVAILABLE, "retry"),
        (404, FETCH_GONE, "gone"),
        (403, FETCH_GONE, "gone"),
        (410, FETCH_GONE, "gone"),
        (401, FETCH_REFUSED, "failed"),
        (206, FETCH_REFUSED, "failed"),
    ],
)
def test_a_status_says_whether_to_try_again(server, fetcher, status, code, outcome):
    server.route("fetch.test", "/a.wav", Reply(status, body=[b"x"]))

    error = _refused(fetcher, "https://fetch.test/a.wav")

    assert (error.code, error.outcome) == (code, outcome)


def test_a_declared_length_over_the_cap_is_refused_before_the_body(server, certificates):
    fetcher = RecordingFetcher(
        resolver=resolver_for(server),
        ssl_context=certificates.client_context(),
        limits=FetchLimits(max_bytes=len(AUDIO) - 1),
    )
    server.route("fetch.test", "/a.wav", _audio())

    assert _refused(fetcher, "https://fetch.test/a.wav").code == FETCH_TOO_LARGE


def test_a_streamed_body_over_the_cap_is_refused(server, certificates):
    fetcher = RecordingFetcher(
        resolver=resolver_for(server),
        ssl_context=certificates.client_context(),
        limits=FetchLimits(max_bytes=len(AUDIO) - 1),
    )
    server.route("fetch.test", "/a.wav", Reply(body=[AUDIO[:100], AUDIO[100:]], chunked=True))

    assert _refused(fetcher, "https://fetch.test/a.wav").code == FETCH_TOO_LARGE


def test_a_server_that_stops_sending_is_timed_out(server, certificates):
    fetcher = RecordingFetcher(
        resolver=resolver_for(server),
        ssl_context=certificates.client_context(),
        limits=FetchLimits(idle_timeout_seconds=0.5),
    )
    server.route("fetch.test", "/a.wav", Reply(stall=True, body=[AUDIO]))

    started = time.monotonic()
    error = _refused(fetcher, "https://fetch.test/a.wav")

    assert (error.code, error.outcome) == (FETCH_TIMEOUT, "retry")
    assert time.monotonic() - started < 5


def test_a_trickle_is_ended_at_the_total_timeout(server, certificates):
    fetcher = RecordingFetcher(
        resolver=resolver_for(server),
        ssl_context=certificates.client_context(),
        limits=FetchLimits(idle_timeout_seconds=2.0, total_timeout_seconds=1.0),
    )
    trickle = [AUDIO[index : index + 64] for index in range(0, len(AUDIO), 64)]
    server.route(
        "fetch.test", "/a.wav", Reply(body=trickle, chunked=True, delay_between_chunks=0.2)
    )

    started = time.monotonic()
    assert _refused(fetcher, "https://fetch.test/a.wav").code == FETCH_TIMEOUT
    assert time.monotonic() - started < 4


def test_close_stops_a_fetch_in_flight(server, fetcher):
    hold = threading.Event()
    reply = _audio(hold=hold)
    server.route("fetch.test", "/a.wav", reply)
    outcome: list[BaseException] = []

    def run() -> None:
        try:
            _fetch(fetcher, "https://fetch.test/a.wav")
        except BaseException as error:
            outcome.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    assert reply.held.wait(10)
    fetcher.close()
    worker.join(timeout=10)
    hold.set()

    assert not worker.is_alive()
    assert [type(error) for error in outcome] == [FetchStopped]


# --- the test-mode origin override ------------------------------------------------------------


def test_the_origin_override_is_inert_outside_test_mode(monkeypatch, certificates):
    with AudioServer(None) as plain:
        plain.route("localhost", "/a.wav", _audio())
        monkeypatch.setenv(TEST_FETCH_ORIGIN, f"http://127.0.0.1:{plain.port}")
        for mode in (None, "0", "true"):
            if mode is None:
                monkeypatch.delenv("EARSHOT_TEST_MODE", raising=False)
            else:
                monkeypatch.setenv("EARSHOT_TEST_MODE", mode)
            fetcher = RecordingFetcher(ssl_context=certificates.client_context())

            # The real resolver and the address rule run: loopback is refused.
            assert _refused(fetcher, "https://localhost/a.wav").code == FETCH_ADDRESS_NOT_ALLOWED
        assert plain.seen() == []


def test_the_origin_override_sends_every_hop_to_the_test_origin_in_test_mode(monkeypatch):
    with AudioServer(None) as plain:
        plain.route("storage.vapi.ai", "/a.wav", _audio())
        monkeypatch.setenv("EARSHOT_TEST_MODE", "1")
        monkeypatch.setenv(TEST_FETCH_ORIGIN, f"http://127.0.0.1:{plain.port}")
        fetcher = RecordingFetcher()
        allowed = HostAllowlist(["storage.vapi.ai"])

        assert _fetch(fetcher, "https://storage.vapi.ai/a.wav?sig=1", allowed) == AUDIO
        [seen] = plain.seen()
        assert (seen.host, seen.path) == ("storage.vapi.ai", "/a.wav?sig=1")
        # The URL rules and the allowlist still hold.
        assert _refused(fetcher, "https://other.test/a.wav", allowed).code == (
            FETCH_HOST_NOT_ALLOWED
        )
        assert _refused(fetcher, "http://storage.vapi.ai/a.wav", allowed).code == (
            FETCH_URL_INVALID
        )


@pytest.mark.parametrize(
    "origin", ["http://10.0.0.1:80", "https://127.0.0.1:443", "http://127.0.0.1", "file:///x"]
)
def test_an_origin_override_that_is_not_a_loopback_http_origin_is_refused(monkeypatch, origin):
    monkeypatch.setenv("EARSHOT_TEST_MODE", "1")
    monkeypatch.setenv(TEST_FETCH_ORIGIN, origin)

    with pytest.raises(ValueError, match=TEST_FETCH_ORIGIN):
        RecordingFetcher()


def test_test_mode_alone_turns_no_guard_off(monkeypatch, certificates):
    monkeypatch.setenv("EARSHOT_TEST_MODE", "1")
    monkeypatch.delenv(TEST_FETCH_ORIGIN, raising=False)
    fetcher = RecordingFetcher(ssl_context=certificates.client_context())

    assert _refused(fetcher, "https://localhost/a.wav").code == FETCH_ADDRESS_NOT_ALLOWED
    assert _refused(fetcher, "http://localhost/a.wav").code == FETCH_URL_INVALID
    assert socket.gethostbyname("localhost").startswith("127.")
