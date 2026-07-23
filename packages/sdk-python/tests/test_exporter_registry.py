"""Gate: exporters are selectable by name through the SDK client, not by import.

The registry is the pluggable seam: a caller names ``"otlp"`` (or an exporter their
own process registered) and receives a projection, without importing a projection
module and without a code change inside earshot. These tests pin the properties
that make that seam safe to depend on -- stable built-in names, governed export
policy, an inert import, and no silent replacement of a registered name.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import earshot
from earshot.cli import main as cli_main
from earshot.codec import encode_incident_json
from earshot.contract import ExportPolicy, IncidentBundle
from earshot.exporters import to_openinference, to_otlp
from earshot.exporters.registry import (
    ExporterRegistry,
    default_registry,
    export_incident,
    exporter_names,
    get_exporter,
    register_exporter,
    unregister_exporter,
)
from earshot.privacy import ExportPolicyError

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
SDK_SRC = ROOT / "packages" / "sdk-python" / "src"


@pytest.fixture
def registered() -> Iterator[list[str]]:
    """Names registered by one test, removed again however the test ends."""

    names: list[str] = []
    try:
        yield names
    finally:
        for name in names:
            unregister_exporter(name)


def _passthrough(bundle: IncidentBundle) -> dict[str, Any]:
    """A user exporter: a projection earshot knows nothing about."""

    return {"acme": {"bundle_id": bundle.profile.manifest.bundle_id}}


def _denying(bundle: IncidentBundle) -> IncidentBundle:
    policies = list(bundle.profile.privacy.capture_classes)
    policies[0] = policies[0].model_copy(
        update={"export": ExportPolicy(allowed=False, policy_id="deny-export")}
    )
    privacy = bundle.profile.privacy.model_copy(update={"capture_classes": tuple(policies)})
    return bundle.model_copy(
        update={"profile": bundle.profile.model_copy(update={"privacy": privacy})}
    )


# -- built-ins are reachable by their stable names ------------------------------


def test_built_in_exporters_are_registered_under_stable_names() -> None:
    assert exporter_names() == ("openinference", "otlp")
    # The registry hands back the same public functions, not a wrapper: the
    # by-name path and the import-and-call path cannot drift apart.
    assert get_exporter("otlp").export is to_otlp
    assert get_exporter("openinference").export is to_openinference


def test_both_otlp_projections_declare_the_otlp_export_destination() -> None:
    # A policy that permits the ``otlp`` destination governs both documents; a
    # second destination name would silently widen every policy already written.
    assert get_exporter("otlp").destination == "otlp"
    assert get_exporter("openinference").destination == "otlp"


def test_export_by_name_matches_the_public_projection_functions(valid_bundle) -> None:
    assert export_incident(valid_bundle, format="otlp") == to_otlp(valid_bundle)
    assert export_incident(valid_bundle, format="openinference") == to_openinference(valid_bundle)


def test_unknown_exporter_names_the_known_ones_not_the_request(valid_bundle) -> None:
    with pytest.raises(ValueError) as error:
        export_incident(valid_bundle, format="not-an-exporter")
    # The requested name arrives from outside and must not be echoed back.
    assert "not-an-exporter" not in str(error.value)
    assert "otlp" in str(error.value)


# -- the client is the normal way to reach them ---------------------------------


def test_the_client_exports_a_finished_incident_by_name(valid_bundle) -> None:
    client = earshot.get_client()
    assert client.export(valid_bundle, format="otlp") == to_otlp(valid_bundle)
    assert client.exporter_formats() == exporter_names()
    assert earshot.export(valid_bundle) == to_otlp(valid_bundle)
    assert earshot.exporter_formats() == exporter_names()


def test_a_user_exporter_is_selectable_through_the_client(valid_bundle, registered) -> None:
    earshot.register_exporter("acme", _passthrough)
    registered.append("acme")

    assert "acme" in earshot.exporter_formats()
    assert earshot.export(valid_bundle, format="acme") == {
        "acme": {"bundle_id": valid_bundle.profile.manifest.bundle_id}
    }
    assert earshot.get_client().export(valid_bundle, format="acme") == _passthrough(valid_bundle)


def test_a_user_exporter_reaches_the_cli_without_a_cli_change(
    tmp_path, valid_bundle, registered, capsys
) -> None:
    earshot.register_exporter("acme", _passthrough)
    registered.append("acme")
    source = tmp_path / "incident.json"
    source.write_bytes(encode_incident_json(valid_bundle))

    assert cli_main(["export", str(source), "--format", "acme"]) == 0
    assert json.loads(capsys.readouterr().out) == _passthrough(valid_bundle)


# -- registration is explicit, governed, and inert ------------------------------


def test_a_registered_name_is_never_silently_replaced(registered) -> None:
    earshot.register_exporter("acme", _passthrough)
    registered.append("acme")

    with pytest.raises(ValueError):
        earshot.register_exporter("acme", _passthrough)
    replacement = earshot.register_exporter("acme", to_otlp, replace=True)
    assert replacement.export is to_otlp


def test_a_user_exporter_defaults_to_its_own_export_destination(registered) -> None:
    registration = earshot.register_exporter("acme", _passthrough)
    registered.append("acme")
    # Inheriting ``otlp``'s destination would let a new exporter ride a permission
    # written for a different backend.
    assert registration.destination == "acme"


def test_a_named_export_refuses_a_bundle_whose_policy_denies_the_destination(
    valid_bundle,
) -> None:
    denied = _denying(valid_bundle)
    with pytest.raises(ExportPolicyError):
        export_incident(denied, format="otlp")
    with pytest.raises(ExportPolicyError):
        earshot.export(denied, format="openinference")
    # The projection function itself is unchanged: the caller who imports it
    # directly still holds the policy, exactly as before the registry existed.
    assert to_otlp(denied)["resourceSpans"]


def test_registration_rejects_a_blank_name_or_a_non_callable() -> None:
    registry = ExporterRegistry()
    with pytest.raises(ValueError):
        registry.register(" ", to_otlp)
    with pytest.raises(ValueError):
        registry.register("", to_otlp)
    with pytest.raises(TypeError):
        registry.register("acme", object())  # type: ignore[arg-type]


def test_a_private_registry_starts_empty_and_orders_names_independent_of_insertion() -> None:
    first, second = ExporterRegistry(), ExporterRegistry()
    assert first.names() == ()

    first.register("zeta", to_otlp)
    first.register("alpha", to_openinference)
    second.register("alpha", to_openinference)
    second.register("zeta", to_otlp)

    assert first.names() == second.names() == ("alpha", "zeta")
    # A private registry is genuinely private: it does not reach the process one.
    assert "zeta" not in exporter_names()


def test_unregister_reports_whether_anything_was_removed(registered) -> None:
    earshot.register_exporter("acme", _passthrough)
    registered.append("acme")
    assert unregister_exporter("acme") is True
    assert unregister_exporter("acme") is False
    assert "acme" not in exporter_names()


def test_importing_the_registry_touches_no_network() -> None:
    # Registration must be a dict fill and nothing else: an import that dialled an
    # endpoint would make merely importing earshot an observable event.
    program = (
        "import sys\n"
        "def audit(event, args):\n"
        "    if event in {'socket.connect', 'socket.getaddrinfo', 'urllib.Request'}:\n"
        "        raise RuntimeError(event)\n"
        "sys.addaudithook(audit)\n"
        "from earshot.exporters.registry import exporter_names\n"
        "print(','.join(exporter_names()))\n"
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
    assert completed.stdout.strip() == "openinference,otlp"


def test_the_process_registry_is_one_object(valid_bundle) -> None:
    assert default_registry() is default_registry()
    assert default_registry().export(valid_bundle, format="otlp") == earshot.export(valid_bundle)


def test_registered_exporters_are_exported_from_the_package_surface() -> None:
    assert earshot.exporters.register_exporter is register_exporter
    assert earshot.exporters.export_incident is export_incident
