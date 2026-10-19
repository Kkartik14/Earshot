from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RUNNER = ROOT / "scripts" / "run_pipecat_matrix.py"
REPORT = ROOT / "docs" / "pipecat-provider-matrix-2026-10-20.json"


def _runner_module():
    spec = importlib.util.spec_from_file_location("earshot_pipecat_matrix_runner", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_matrix_report_enumerates_the_tvics_supported_provider_product() -> None:
    runner = _runner_module()
    report = json.loads(REPORT.read_text())
    stt_providers = ("deepgram", "cartesia", "sarvam", "elevenlabs", "assemblyai", "soniox")
    llm_providers = ("groq", "openai")
    tts_providers = ("cartesia", "elevenlabs", "sarvam")
    expected = {
        f"{stt}__{llm}__{tts}"
        for stt in stt_providers
        for llm in llm_providers
        for tts in tts_providers
    }
    entries = report["cells"] + report["unavailable"]

    assert stt_providers == runner.STT_PROVIDERS
    assert llm_providers == runner.LLM_PROVIDERS
    assert tts_providers == runner.TTS_PROVIDERS
    assert len(runner.CELLS) == 36
    assert report["dimensions"]["total_cells"] == 36
    assert {entry["cell"] for entry in entries} == expected
    assert all(entry["valid"] for entry in report["cells"])
    assert all(entry["status"] == "unavailable" for entry in report["unavailable"])


def test_matrix_report_keeps_runtime_failures_separate_from_provider_probes() -> None:
    report = json.loads(REPORT.read_text())
    failed = [entry for entry in report["cells"] if entry["status"] != "completed"]

    assert failed
    assert all("provider_failure" not in entry["operations"] for entry in failed)
    assert set(report["provider_probes"]) <= {"elevenlabs", "openai"}
    assert all(
        set(probe) <= {"status_code", "error_type"} for probe in report["provider_probes"].values()
    )
    assert report["latency_policy"]["observer_budget_ms"] == 250
    assert report["latency_policy"]["remote_provider_ttfb"] == "advisory_not_pass_fail"


def test_cached_runtime_failure_makes_matrix_exit_nonzero(tmp_path, monkeypatch) -> None:
    runner = _runner_module()
    cell = runner.Cell("cartesia", "groq", "cartesia")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    artifact = json.loads((ROOT / "fixtures" / "valid" / "minimal.json").read_text())
    artifact["profile"]["session"]["status"] = "interrupted"
    (artifacts / f"{cell.name}.json").write_text(json.dumps(artifact))

    monkeypatch.setattr(runner, "CELLS", (cell,))
    monkeypatch.setattr(runner, "ARTIFACT_DIR", artifacts)
    monkeypatch.setattr(runner, "REPORT_PATH", tmp_path / "report.json")
    monkeypatch.setattr(runner, "_load_dotenv", lambda: None)
    monkeypatch.setattr(runner, "_provider_probes", lambda: {})
    monkeypatch.setattr(runner, "_missing_credentials", lambda _cell: ())
    monkeypatch.setattr(runner, "_missing_dependencies", lambda _cell: ())

    assert runner._parent(force=False) == 1
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["cells"][0]["status"] == "interrupted"
    assert report["cells"][0]["valid"] is True
