"""Gate: delivery strategies are selectable by name through the seam, not hard-wired.

The delivery registry is to transports what the exporter registry is to
projections: a caller names ``"durable"`` (or a strategy their own process
registered) and the SDK client builds it, without a hard-wired ``delivery_mode``
switch inside the client and without importing an exporter class. These tests pin
the properties that make the seam safe to depend on -- stable built-in names, an
inert import, no silent replacement -- and prove a delivery built through the seam
is byte-for-byte the delivery the client used to construct directly, and that a
user strategy is reachable through the normal ``delivery_mode`` surface.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

import earshot
from earshot.delivery import (
    DeliveryContext,
    DeliveryRegistry,
    DeliverySink,
    RegisteredDelivery,
    build_delivery,
    default_delivery_registry,
    delivery_modes,
    get_delivery,
    register_delivery,
    unregister_delivery,
)
from earshot.exporter import (
    INCIDENT_JSON,
    BoundedAsyncExporter,
    DurableExporter,
    ExporterStatus,
    ExportItem,
    SynchronousExporter,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
SDK_SRC = ROOT / "packages" / "sdk-python" / "src"


@pytest.fixture(autouse=True)
def reset_global_sdk_configuration() -> Iterator[None]:
    earshot.shutdown()
    earshot.configure()
    yield
    earshot.shutdown()
    earshot.configure()


@pytest.fixture
def registered() -> Iterator[list[str]]:
    """Names registered by one test, removed again however the test ends."""

    names: list[str] = []
    try:
        yield names
    finally:
        for name in names:
            unregister_delivery(name)


class _CapturingTransport:
    """A transport that records the exact bytes it was asked to send."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.sent: list[tuple[str, str, bytes]] = []

    def send(self, item: ExportItem) -> None:
        self.sent.append((item.bundle_id, item.content_type, item.payload))


class _CapturingDelivery:
    """A user delivery strategy: it satisfies the seam and keeps what it was given."""

    def __init__(self, context: DeliveryContext) -> None:
        self.context = context
        self.items: list[ExportItem] = []

    def submit(self, item: ExportItem) -> bool:
        self.items.append(item)
        return True

    def flush(self, timeout: float | None = None) -> bool:
        return True

    def shutdown(self, timeout: float = 5.0) -> bool:
        return True

    def status(self) -> ExporterStatus:
        return ExporterStatus(
            state="running",
            pid=os.getpid(),
            accepted=len(self.items),
            sent=len(self.items),
            dropped=0,
            failed=0,
            rejected=0,
            pending=0,
            queued_bytes=0,
        )


def test_built_in_delivery_modes_are_registered_under_stable_names() -> None:
    assert delivery_modes() == ("async", "durable", "sync")
    assert earshot.delivery_modes() == delivery_modes()
    assert earshot.get_client().delivery_modes() == delivery_modes()


def test_built_in_delivery_strategies_satisfy_the_delivery_sink_protocol() -> None:
    for strategy in (BoundedAsyncExporter, SynchronousExporter, DurableExporter):
        for verb in ("submit", "flush", "shutdown", "status"):
            assert callable(getattr(strategy, verb, None)), (strategy, verb)


def test_unknown_delivery_mode_names_the_known_ones_not_the_request() -> None:
    with pytest.raises(ValueError) as error:
        get_delivery("not-a-delivery")
    assert "not-a-delivery" not in str(error.value)
    assert "async" in str(error.value)


def test_a_delivery_built_through_the_seam_delivers_identical_bytes() -> None:
    item = ExportItem(
        bundle_id="bundle-seam-parity",
        payload=b'{"resourceSpans":[]}',
        content_type=INCIDENT_JSON,
    )

    seam_transport = _CapturingTransport()
    seam_delivery = build_delivery("sync", DeliveryContext(transport=seam_transport))
    assert seam_delivery.submit(item) is True

    direct_transport = _CapturingTransport()
    direct_delivery = SynchronousExporter(direct_transport)
    assert direct_delivery.submit(item) is True

    # Same bytes, same content type, same idempotency key: the registry changed how
    # the delivery is selected, not what it delivers.
    assert (
        seam_transport.sent
        == direct_transport.sent
        == [("bundle-seam-parity", INCIDENT_JSON, b'{"resourceSpans":[]}')]
    )


def test_a_user_delivery_is_selectable_through_the_client(registered) -> None:
    captured: list[_CapturingDelivery] = []

    def factory(context: DeliveryContext) -> DeliverySink:
        delivery = _CapturingDelivery(context)
        captured.append(delivery)
        return delivery

    registration = earshot.register_delivery("capture", factory)
    registered.append("capture")
    assert isinstance(registration, RegisteredDelivery)
    assert "capture" in earshot.delivery_modes()

    client = earshot.Client(endpoint="http://localhost:4319", delivery_mode="capture")
    try:
        recorder = client.session(bundle_id="captured-incident")
        recorder.close()

        # The user strategy the client selected by name received the incident: the
        # delivery seam is reachable exactly like the projection seam, no client
        # change required.
        assert recorder.export_accepted is True
        assert captured and [item.bundle_id for item in captured[-1].items] == ["captured-incident"]
    finally:
        assert client.shutdown()


def test_the_built_in_delivery_modes_still_work_exactly_as_before(monkeypatch) -> None:
    monkeypatch.setattr(earshot.sdk, "HttpExportTransport", _CapturingTransport)
    client = earshot.Client(endpoint="http://localhost:4319", delivery_mode="async")
    try:
        assert client.config.delivery_mode == "async"
        client.session(bundle_id="async-through-seam").close()
        assert client.flush(2)
        assert client.status().sent == 1
    finally:
        assert client.shutdown()


def test_a_registered_delivery_name_is_never_silently_replaced(registered) -> None:
    def factory(context: DeliveryContext) -> DeliverySink:
        return _CapturingDelivery(context)

    register_delivery("capture", factory)
    registered.append("capture")

    with pytest.raises(ValueError):
        register_delivery("capture", factory)
    replacement = register_delivery("capture", factory, replace=True)
    assert replacement.factory is factory


def test_registration_rejects_a_blank_name_or_a_non_callable() -> None:
    registry = DeliveryRegistry()
    with pytest.raises(ValueError):
        registry.register(" ", lambda context: _CapturingDelivery(context))
    with pytest.raises(ValueError):
        registry.register("", lambda context: _CapturingDelivery(context))
    with pytest.raises(TypeError):
        registry.register("capture", object())  # type: ignore[arg-type]


def test_a_private_registry_starts_empty_and_orders_names_independent_of_insertion() -> None:
    first, second = DeliveryRegistry(), DeliveryRegistry()
    assert first.names() == ()

    def factory(context: DeliveryContext) -> DeliverySink:
        return _CapturingDelivery(context)

    first.register("zeta", factory)
    first.register("alpha", factory)
    second.register("alpha", factory)
    second.register("zeta", factory)

    assert first.names() == second.names() == ("alpha", "zeta")
    # A private registry is genuinely private: it does not reach the process one.
    assert "zeta" not in delivery_modes()


def test_unregister_reports_whether_anything_was_removed(registered) -> None:
    register_delivery("capture", lambda context: _CapturingDelivery(context))
    registered.append("capture")
    assert unregister_delivery("capture") is True
    assert unregister_delivery("capture") is False
    assert "capture" not in delivery_modes()


def test_the_process_registry_is_one_object() -> None:
    assert default_delivery_registry() is default_delivery_registry()


def test_importing_the_delivery_registry_touches_no_network() -> None:
    program = (
        "import sys\n"
        "def audit(event, args):\n"
        "    if event in {'socket.connect', 'socket.getaddrinfo', 'urllib.Request'}:\n"
        "        raise RuntimeError(event)\n"
        "sys.addaudithook(audit)\n"
        "import earshot\n"
        "print(','.join(earshot.delivery_modes()))\n"
    )
    environment = dict(os.environ, PYTHONPATH=str(SDK_SRC))
    completed = subprocess.run(
        [sys.executable, "-c", program],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "async,durable,sync"
