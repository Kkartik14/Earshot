from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RUNNER = ROOT / "scripts" / "run_pipecat_matrix.py"
REPORT = ROOT / "docs" / "pipecat-provider-matrix-2026-09-28.json"


def _runner_module():
    spec = importlib.util.spec_from_file_location("earshot_pipecat_matrix_runner", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_matrix_report_enumerates_the_full_tvic_cartesian_product() -> None:
    runner = _runner_module()
    report = json.loads(REPORT.read_text())
    expected = {
        f"{stt}__{llm}__{tts}"
        for stt in runner.STT_PROVIDERS
        for llm in runner.LLM_PROVIDERS
        for tts in runner.TTS_PROVIDERS
    }
    entries = report["cells"] + report["unavailable"]

    assert len(runner.CELLS) == 24
    assert report["dimensions"]["total_cells"] == 24
    assert {entry["cell"] for entry in entries} == expected
    assert len(report["cells"]) == 16
    assert len(report["unavailable"]) == 8
    assert all(entry["valid"] for entry in report["cells"])
    assert all(entry["status"] == "unavailable" for entry in report["unavailable"])


def test_matrix_report_retains_provider_failure_evidence_and_latency_policy() -> None:
    report = json.loads(REPORT.read_text())
    failed = [entry for entry in report["cells"] if entry["status"] != "completed"]

    assert len(failed) == 12
    assert all(entry["operations"]["provider_failure"] == "error" for entry in failed)
    assert report["provider_probes"] == {
        "elevenlabs": {"status_code": 402},
        "openai": {"status_code": 429},
    }
    assert report["latency_policy"]["observer_budget_ms"] == 250
    assert report["latency_policy"]["remote_provider_ttfb"] == "advisory_not_pass_fail"
