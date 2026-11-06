from __future__ import annotations

import copy
import io
import json
import math
from pathlib import Path

import pytest

from earshot.cli import main
from earshot.host_profile import check_host_profile

FIXTURES = Path(__file__).parent / "fixtures" / "host_profile"


@pytest.mark.parametrize(
    ("fixture", "versions", "span_count"),
    [
        ("0.1.0.json", ["0.1.0"], 1),
        ("0.2.0-reported.json", ["0.2.0"], 4),
        ("0.2.0-omitted.json", ["0.2.0"], 3),
    ],
)
def test_supported_profiles_accept_baseline_reported_and_omitted_values(
    fixture: str, versions: list[str], span_count: int
) -> None:
    report = check_host_profile((FIXTURES / fixture).read_bytes())

    assert report.valid
    assert report.host_profile_versions == tuple(versions)
    assert report.spans_checked == span_count


def _reported_request() -> dict:
    return json.loads((FIXTURES / "0.2.0-reported.json").read_text())


def _attributes(span: dict) -> list[dict]:
    return span["attributes"]


def _attribute(span: dict, key: str) -> dict:
    return next(attribute for attribute in _attributes(span) if attribute["key"] == key)


@pytest.mark.parametrize(
    ("case", "mutate", "code"),
    [
        (
            "zero STT usage",
            lambda request: _attribute(
                request["resourceSpans"][0]["scopeSpans"][0]["spans"][2],
                "earshot.stt.usage.audio_seconds",
            )["value"].update(doubleValue=0.0),
            "invalid_stt_audio_seconds",
        ),
        (
            "non-finite STT usage",
            lambda request: _attribute(
                request["resourceSpans"][0]["scopeSpans"][0]["spans"][2],
                "earshot.stt.usage.audio_seconds",
            )["value"].update(doubleValue=math.inf),
            "invalid_stt_audio_seconds",
        ),
        (
            "wrong STT AnyValue type",
            lambda request: _attribute(
                request["resourceSpans"][0]["scopeSpans"][0]["spans"][2],
                "earshot.stt.usage.audio_seconds",
            ).update(value={"intValue": "375"}),
            "wrong_attribute_type",
        ),
        (
            "wrong TTS AnyValue type",
            lambda request: _attribute(
                request["resourceSpans"][0]["scopeSpans"][0]["spans"][3],
                "earshot.tts.usage.character_count",
            ).update(value={"stringValue": "217"}),
            "wrong_attribute_type",
        ),
        (
            "wrong span role",
            lambda request: _attribute(
                request["resourceSpans"][0]["scopeSpans"][0]["spans"][2],
                "earshot.stt.usage.audio_seconds",
            ).update(key="earshot.tts.usage.character_count"),
            "wrong_span",
        ),
        (
            "wrong scope version",
            lambda request: request["resourceSpans"][0]["scopeSpans"][0]["scope"].update(
                version="0.1.0"
            ),
            "wrong_scope_version",
        ),
        (
            "duplicate transcript",
            lambda request: _attributes(
                request["resourceSpans"][0]["scopeSpans"][0]["spans"][1]
            ).append(
                copy.deepcopy(
                    _attribute(
                        request["resourceSpans"][0]["scopeSpans"][0]["spans"][1],
                        "earshot.transcript.text",
                    )
                )
            ),
            "duplicate_attribute",
        ),
        (
            "blank transcript",
            lambda request: _attribute(
                request["resourceSpans"][0]["scopeSpans"][0]["spans"][1], "earshot.transcript.text"
            ).update(value={"stringValue": "   "}),
            "invalid_transcript_text",
        ),
    ],
)
def test_invalid_profile_fixtures_report_stable_codes(case: str, mutate, code: str) -> None:
    request = _reported_request()
    mutate(request)

    report = check_host_profile(request)

    assert not report.valid, case
    assert code in {issue.code for issue in report.issues}, case


def test_checker_ignores_unknown_extensions_but_rejects_unsupported_profile_versions() -> None:
    request = _reported_request()
    request["resourceSpans"][0]["scopeSpans"][0]["scope"]["version"] = "0.3.0"
    request["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"].append(
        {"key": "vendor.custom", "value": {"stringValue": "ignored"}}
    )

    report = check_host_profile(request)

    assert not report.valid
    assert {issue.code for issue in report.issues} == {"unsupported_version"}


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"resourceSpans": ["not-an-object"]}, "invalid_resource_span"),
        ({"resourceSpans": [{"scopeSpans": [None]}]}, "invalid_scope_span"),
    ],
)
def test_checker_rejects_non_object_otlp_envelope_members(payload: dict, code: str) -> None:
    report = check_host_profile(payload)

    assert not report.valid
    assert code in {issue.code for issue in report.issues}


def test_checker_rejects_non_utf8_json_bytes() -> None:
    payload = (FIXTURES / "0.1.0.json").read_text().encode("utf-16")

    report = check_host_profile(payload)

    assert not report.valid
    assert {issue.code for issue in report.issues} == {"invalid_json"}


def test_checker_uses_strict_json_and_rejects_bare_nonfinite_constants() -> None:
    request = _reported_request()
    turn = request["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    turn["attributes"].append({"key": "vendor.extension", "value": {"doubleValue": math.nan}})

    report = check_host_profile(json.dumps(request))

    assert not report.valid
    assert "invalid_json" in {issue.code for issue in report.issues}


def test_checker_rejects_json_number_with_extreme_exponent_without_raising() -> None:
    payload = '{"resourceSpans": [], "extension": 1e999999999999999999999999999999999999999}'

    report = check_host_profile(payload)

    assert not report.valid
    assert "invalid_json" in {issue.code for issue in report.issues}


@pytest.mark.parametrize("tts_value", [217.0, "217.0", "2.17e2", "+217", "0217"])
def test_checker_accepts_protojson_numeric_strings_and_integral_number_forms(tts_value) -> None:
    request = _reported_request()
    turn, _, stt, tts = request["resourceSpans"][0]["scopeSpans"][0]["spans"]
    _attribute(turn, "earshot.turn.reported_latency_ms")["value"]["doubleValue"] = "812.5"
    _attribute(stt, "earshot.stt.usage.audio_seconds")["value"]["doubleValue"] = "3.75e-1"
    _attribute(tts, "earshot.tts.usage.character_count")["value"]["intValue"] = tts_value

    assert check_host_profile(json.dumps(request)).valid


def test_checker_rejects_fractional_protojson_int64_string() -> None:
    request = _reported_request()
    tts = request["resourceSpans"][0]["scopeSpans"][0]["spans"][3]
    _attribute(tts, "earshot.tts.usage.character_count")["value"]["intValue"] = "217.5"

    report = check_host_profile(json.dumps(request))

    assert not report.valid
    assert "invalid_tts_character_count" in {issue.code for issue in report.issues}


def test_checker_accepts_max_json_safe_tts_count() -> None:
    request = _reported_request()
    tts = request["resourceSpans"][0]["scopeSpans"][0]["spans"][3]
    _attribute(tts, "earshot.tts.usage.character_count")["value"]["intValue"] = str(2**53 - 1)

    assert check_host_profile(json.dumps(request)).valid


@pytest.mark.parametrize("tts_value", [2**53, str(2**53), str(2**63 - 1)])
def test_checker_rejects_tts_count_above_json_safe_range(tts_value) -> None:
    request = _reported_request()
    tts = request["resourceSpans"][0]["scopeSpans"][0]["spans"][3]
    _attribute(tts, "earshot.tts.usage.character_count")["value"]["intValue"] = tts_value

    report = check_host_profile(json.dumps(request))

    assert not report.valid
    assert "invalid_tts_character_count" in {issue.code for issue in report.issues}


@pytest.mark.parametrize(
    "wire_value",
    [
        '"9007199254740991.1"',
        "9007199254740991.1",
        '"2_17"',
    ],
)
def test_checker_rejects_lossy_or_non_protojson_tts_integer_forms(wire_value) -> None:
    payload = (
        (FIXTURES / "0.2.0-reported.json")
        .read_text()
        .replace('"intValue": "217"', f'"intValue": {wire_value}')
    )

    report = check_host_profile(payload)

    assert not report.valid
    assert "invalid_tts_character_count" in {issue.code for issue in report.issues}


def test_checker_accepts_integral_python_float_for_tts_integer_mapping() -> None:
    request = _reported_request()
    tts = request["resourceSpans"][0]["scopeSpans"][0]["spans"][3]
    _attribute(tts, "earshot.tts.usage.character_count")["value"]["intValue"] = 217.0

    report = check_host_profile(request)

    assert report.valid


def test_checker_accepts_exact_integral_protojson_exponent() -> None:
    payload = (
        (FIXTURES / "0.2.0-reported.json")
        .read_text()
        .replace('"intValue": "217"', '"intValue": 2.17e2')
    )

    assert check_host_profile(payload).valid


def test_checker_rejects_tts_int64_values_that_exceed_python_integer_parse_limits() -> None:
    request = _reported_request()
    tts = request["resourceSpans"][0]["scopeSpans"][0]["spans"][3]
    _attribute(tts, "earshot.tts.usage.character_count")["value"]["intValue"] = "1" * 5000
    numeric_payload = (
        (FIXTURES / "0.2.0-reported.json")
        .read_text()
        .replace('"intValue": "217"', '"intValue": ' + "1" * 5000)
    )

    string_report = check_host_profile(request)
    numeric_report = check_host_profile(numeric_payload)

    assert not string_report.valid
    assert "invalid_tts_character_count" in {issue.code for issue in string_report.issues}
    assert not numeric_report.valid
    assert "invalid_json" in {issue.code for issue in numeric_report.issues}


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (
            lambda request: _attribute(
                request["resourceSpans"][0]["scopeSpans"][0]["spans"][0],
                "earshot.turn.reported_latency_ms",
            )["value"].update(doubleValue=0.0),
            "invalid_turn_reported_latency",
        ),
        (
            lambda request: _attribute(
                request["resourceSpans"][0]["scopeSpans"][0]["spans"][0],
                "earshot.turn.reported_latency_ms",
            )["value"].update(doubleValue=math.inf),
            "invalid_turn_reported_latency",
        ),
        (
            lambda request: _attribute(
                request["resourceSpans"][0]["scopeSpans"][0]["spans"][0],
                "earshot.turn.reported_latency_ms",
            ).update(value={"stringValue": "812.5"}),
            "wrong_attribute_type",
        ),
        (
            lambda request: _attribute(
                request["resourceSpans"][0]["scopeSpans"][0]["spans"][2],
                "earshot.stt.usage.audio_seconds",
            ).update(key="earshot.turn.reported_latency_ms"),
            "wrong_span",
        ),
    ],
)
def test_checker_requires_reported_host_latency_to_be_a_positive_double(mutate, code: str) -> None:
    request = _reported_request()
    mutate(request)

    report = check_host_profile(request)

    assert not report.valid
    assert code in {issue.code for issue in report.issues}


def test_checker_accepts_positive_reported_latency_in_the_legacy_profile() -> None:
    request = json.loads((FIXTURES / "0.1.0.json").read_text())
    turn = request["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    turn["attributes"].append(
        {"key": "earshot.turn.reported_latency_ms", "value": {"doubleValue": 1.0}}
    )

    assert check_host_profile(request).valid


def test_missing_scope_version_retains_legacy_profile_0_1_behavior() -> None:
    request = _reported_request()
    tts = request["resourceSpans"][0]["scopeSpans"][0]["spans"][3]
    _attribute(tts, "earshot.tts.usage.character_count")["value"]["intValue"] = str(2**63 - 1)
    request["resourceSpans"][0]["scopeSpans"][0]["scope"].pop("version")
    request["resourceSpans"][0]["scopeSpans"][0]["spans"] = [
        request["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    ]

    report = check_host_profile(request)

    assert report.valid
    assert report.host_profile_versions == ("0.1.0",)


def test_cli_checks_an_offline_otlp_json_file(tmp_path, capsys) -> None:
    valid = tmp_path / "valid.json"
    valid.write_bytes((FIXTURES / "0.2.0-reported.json").read_bytes())

    assert main(["check-host-profile", str(valid)]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True

    invalid_request = _reported_request()
    _attribute(
        invalid_request["resourceSpans"][0]["scopeSpans"][0]["spans"][2],
        "earshot.stt.usage.audio_seconds",
    )["value"]["doubleValue"] = 0
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(invalid_request))

    assert main(["check-host-profile", str(invalid)]) == 1
    assert json.loads(capsys.readouterr().out)["valid"] is False


def test_cli_reads_an_otlp_json_request_from_stdin(monkeypatch, capsys) -> None:
    import sys

    monkeypatch.setattr(
        sys,
        "stdin",
        type("Stdin", (), {"buffer": io.BytesIO((FIXTURES / "0.1.0.json").read_bytes())})(),
    )

    assert main(["check-host-profile", "-"]) == 0
    assert json.loads(capsys.readouterr().out)["host_profile_versions"] == ["0.1.0"]
