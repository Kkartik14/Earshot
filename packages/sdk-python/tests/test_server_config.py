"""One configuration parser for ``earshot serve`` and ``apps/ingest/app.py``.

The parser reads environment variables (and, for ``serve``, flags that override
them) into one ``ServerSettings``. Invalid values refuse to start with a message
that names the variable and never repeats its value, because some carry secrets.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from earshot.server_config import ConfigurationError, load_server_settings

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]


def test_unset_variables_take_the_documented_defaults(tmp_path) -> None:
    settings = load_server_settings({"EARSHOT_DATA_DIR": str(tmp_path)})

    assert settings.database_url is None
    assert settings.blob_url == f"file://{tmp_path.resolve()}/blobs"
    assert (settings.host, settings.port) == ("127.0.0.1", 4319)
    assert settings.db_pool_size == 8
    assert (
        settings.call_end_grace_seconds,
        settings.call_long_quiet_seconds,
        settings.call_late_debounce_seconds,
        settings.recording_upload_hold_seconds,
    ) == (10.0, 300.0, 30.0, 600.0)
    assert (
        settings.otlp_max_protobuf_bytes,
        settings.otlp_max_json_bytes,
        settings.recording_max_bytes,
    ) == (33_554_432, 4_194_304, 536_870_912)
    assert settings.trust_local_network is False
    assert settings.hosted_event_names == frozenset()


def test_the_call_lifecycle_timings_come_from_the_settings(tmp_path) -> None:
    from earshot.storage_calls import CallTimings

    settings = load_server_settings(
        {
            "EARSHOT_DATA_DIR": str(tmp_path),
            "EARSHOT_CALL_END_GRACE_SECONDS": "2.5",
            "EARSHOT_CALL_LONG_QUIET_SECONDS": "60",
            "EARSHOT_CALL_LATE_DEBOUNCE_SECONDS": "7",
            "EARSHOT_RECORDING_UPLOAD_HOLD_SECONDS": "90",
        }
    )

    assert settings.call_timings() == CallTimings(
        end_grace_seconds=2.5,
        long_quiet_seconds=60.0,
        late_debounce_seconds=7.0,
        upload_hold_seconds=90.0,
    )
    assert load_server_settings({"EARSHOT_DATA_DIR": str(tmp_path)}).call_timings() == (
        CallTimings()
    )


def test_the_recording_size_limit_reaches_the_api(tmp_path) -> None:
    configured = load_server_settings(
        {"EARSHOT_DATA_DIR": str(tmp_path), "EARSHOT_RECORDING_MAX_BYTES": "2048"}
    )
    default = load_server_settings({"EARSHOT_DATA_DIR": str(tmp_path)})

    assert configured.api_config().recording_max_bytes == 2048
    assert default.api_config().recording_max_bytes == 536_870_912


def test_a_flag_overrides_its_variable_which_overrides_the_default() -> None:
    environment = {"EARSHOT_PORT": "5000", "EARSHOT_HOST": "127.0.0.2"}

    settings = load_server_settings(environment, port=6000)

    assert (settings.host, settings.port) == ("127.0.0.2", 6000)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", True), ("true", True), ("YES", True), ("0", False), ("false", False), ("", False)],
)
def test_switches_accept_the_usual_spellings(value: str, expected: bool) -> None:
    assert load_server_settings({"EARSHOT_BEHIND_TLS_PROXY": value}).behind_tls_proxy is expected


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("EARSHOT_TRUST_LOCAL_NETWORK", "enabled-please"),
        ("EARSHOT_PORT", "eighty-80"),
        ("EARSHOT_MAX_BODY_BYTES", "-1"),
        ("EARSHOT_CALL_LONG_QUIET_SECONDS", "nan"),
        ("EARSHOT_DB_POOL_SIZE", "1"),
        ("EARSHOT_OTLP_MAX_JSON_BYTES", "0"),
        ("EARSHOT_DATABASE_URL", "mysql://user:hunter2-secret@db/earshot"),
        ("EARSHOT_BLOB_URL", "ftp://bucket/hunter2-secret"),
        ("EARSHOT_BLOB_URL", "file:///var/blobs#hunter2-secret"),
        ("EARSHOT_BLOB_URL", "s3://bucket/prefix#hunter2-secret"),
    ],
)
def test_an_invalid_value_is_refused_by_name_without_repeating_it(name: str, value: str) -> None:
    with pytest.raises(ConfigurationError) as refused:
        load_server_settings({name: value})

    assert name in str(refused.value)
    assert value not in str(refused.value)
    assert "hunter2" not in str(refused.value)


CALL_TIMINGS = (
    "EARSHOT_CALL_END_GRACE_SECONDS",
    "EARSHOT_CALL_LONG_QUIET_SECONDS",
    "EARSHOT_CALL_LATE_DEBOUNCE_SECONDS",
    "EARSHOT_RECORDING_UPLOAD_HOLD_SECONDS",
)


@pytest.mark.parametrize("name", CALL_TIMINGS)
@pytest.mark.parametrize("value", ["1e10", "1e300", "315360001", "1e-12"])
def test_a_call_timing_outside_its_bounds_is_refused_at_startup(name: str, value: str) -> None:
    """Reject timings that exceed the nanosecond range or are shorter than one
    nanosecond when the server starts, rather than on the first request."""

    with pytest.raises(ConfigurationError) as refused:
        load_server_settings({name: value})

    assert name in str(refused.value)
    assert value not in str(refused.value)


@pytest.mark.parametrize("name", CALL_TIMINGS)
def test_a_call_timing_at_its_bounds_is_accepted(name: str) -> None:
    for value in ("315360000", "0.000000001"):
        load_server_settings({name: value}).call_timings()


def test_the_asgi_module_exits_3_naming_an_out_of_range_timing(tmp_path) -> None:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("EARSHOT_")
    }
    environment.update(
        {
            "PYTHONPATH": str(ROOT / "packages" / "sdk-python" / "src"),
            "EARSHOT_DATA_DIR": str(tmp_path / "data"),
            "EARSHOT_CALL_LONG_QUIET_SECONDS": "1e10",
        }
    )

    result = subprocess.run(
        [sys.executable, "-c", "import apps.ingest.app"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 3, result.stderr[-2000:]
    assert "EARSHOT_CALL_LONG_QUIET_SECONDS" in result.stderr


def test_s3_blob_urls_are_parsed_and_never_carry_credentials() -> None:
    settings = load_server_settings(
        {"EARSHOT_BLOB_URL": "s3://evidence/prod?endpoint=http://127.0.0.1:59000&path_style=1"}
    )
    assert settings.blob_url.startswith("s3://evidence/prod?")

    with pytest.raises(ConfigurationError) as refused:
        load_server_settings({"EARSHOT_BLOB_URL": "s3://AKIAKEY:wJalr-secret@evidence/prod"})
    assert "AWS" in str(refused.value)
    assert "wJalr" not in str(refused.value) and "AKIAKEY" not in str(refused.value)


def test_the_asgi_app_reads_the_same_settings_as_serve(tmp_path) -> None:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("EARSHOT_")
    }
    environment.update(
        {
            "PYTHONPATH": str(ROOT / "packages" / "sdk-python" / "src"),
            "EARSHOT_DATA_DIR": str(tmp_path / "data"),
            # A container listener confined to a trusted boundary (compose.yaml).
            "EARSHOT_HOST": "0.0.0.0",
            "EARSHOT_TRUST_LOCAL_NETWORK": "true",
            "EARSHOT_HOSTED_EVENT_NAMES": "call.started,call.ended",
        }
    )
    probe = (
        "import json; from apps.ingest.app import app; config = app.state.config; "
        "print(json.dumps([config.trust_local_network, sorted(config.hosted_event_names)]))"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert json.loads(result.stdout.splitlines()[-1]) == [True, ["call.ended", "call.started"]]
