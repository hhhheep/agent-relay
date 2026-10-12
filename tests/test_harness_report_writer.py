from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent.parent / "scripts" / "harness" / "run_research_report_writer.py"
_SPEC = importlib.util.spec_from_file_location("harness_report_writer", _SCRIPT)
assert _SPEC and _SPEC.loader
writer = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(writer)


def _evidence(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "task_id": "report-test",
                "title": "Bounded result",
                "question": "What did the stage establish?",
                "report_type": "research_summary",
                "execution_status": "completed",
                "scientific_status": "bounded",
                "claims": [
                    {"claim_id": "c1", "kind": "measurement", "text": "AUC was 0.91", "source": "metrics.json"}
                ],
                "forbidden_claims": ["This is formal proof."],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_report_writer_stage_writes_one_report_sidecar_and_usage(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    run_dir = tmp_path / "execution" / "writer-job"
    monkeypatch.setenv("SOLUTION_AGENT_USAGE_RUN_DIR", str(run_dir))
    monkeypatch.setenv("SOLUTION_AGENT_USAGE_RUN_ID", "writer-job")
    evidence = tmp_path / "evidence.json"
    _evidence(evidence)

    def provider_call(system: str, user: str, *, max_output_tokens: int):
        assert "grounded only" in system
        assert "AUC was 0.91" in user
        assert max_output_tokens == 2048
        return (
            {
                "report_markdown": "# Result\n\nThe measured AUC was 0.91 [c1].",
                "claims_used": ["c1"],
                "warnings": [],
                "unsupported_claims": [],
            },
            {
                "id": "resp-1",
                "model": "deepseek-v4-flash-0731",
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 120,
                    "total_tokens": 1120,
                    "prompt_tokens_details": {"cached_tokens": 600},
                },
            },
        )

    result = writer.run(
        evidence_path=evidence,
        output_dir=tmp_path / "out",
        max_output_tokens=2048,
        provider_call=provider_call,
    )

    assert Path(result["report_path"]).is_file()
    sidecar = json.loads(Path(result["sidecar_path"]).read_text(encoding="utf-8"))
    assert sidecar["claims_used"] == ["c1"]
    assert sidecar["provider"]["usage"]["new_input_tokens"] == 400
    metrics = json.loads(Path(result["metrics_path"]).read_text(encoding="utf-8"))
    assert metrics["model_execution_evidence"]["proxy_only"] is False
    assert metrics["total_tokens"] == 1120
    publication = json.loads(Path(result["publication_packet_path"]).read_text(encoding="utf-8"))
    assert publication["kind"] == "draft_only"
    assert publication["writer_must_not_commit"] is True
    assert publication["draft_sha256"] == result["output_sha256"]
    from solution_agent.harness.usage_ledger import UsageLedger

    events = UsageLedger(run_dir, run_id="writer-job").read()
    assert len(events) == 1
    assert events[0].side == "external_writer"
    assert events[0].phase == "report"
    assert events[0].usage_source == "provider"
    assert events[0].total_tokens == 1120


def test_report_writer_stage_rejects_unknown_claim_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    evidence = tmp_path / "evidence.json"
    _evidence(evidence)

    def provider_call(system: str, user: str, *, max_output_tokens: int):
        return (
            {
                "report_markdown": "unsupported",
                "claims_used": ["invented"],
                "warnings": [],
                "unsupported_claims": [],
            },
            {"model": "deepseek-v4-flash-0731", "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        )

    try:
        writer.run(evidence_path=evidence, output_dir=tmp_path / "out", provider_call=provider_call)
    except ValueError as exc:
        assert "unknown claim_ids" in str(exc)
    else:
        raise AssertionError("unknown claim id should fail")


def test_report_writer_emits_researchflow_handoff_without_editing_record(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "writer"
    input_root = tmp_path / "vault"
    output_root = workspace / "artifacts"
    authority_root = workspace / "research" / "records"
    for path in (workspace, input_root, output_root, authority_root):
        path.mkdir(parents=True, exist_ok=True)
    record = authority_root / "experiment.md"
    record.write_text("# Original\n", encoding="utf-8")
    evidence = input_root / "evidence.json"
    _evidence(evidence)
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("SOLUTION_AGENT_INPUT_ROOTS", os.pathsep.join([str(workspace), str(input_root)]))
    monkeypatch.setenv("SOLUTION_AGENT_OUTPUT_ROOTS", str(workspace))
    monkeypatch.setenv("SOLUTION_AGENT_AUTHORITY_ROOTS", str(authority_root))

    def provider_call(system: str, user: str, *, max_output_tokens: int):
        return (
            {
                "report_markdown": "# Draft\n\nMeasured AUC 0.91 [c1].",
                "claims_used": ["c1"],
                "warnings": [],
                "unsupported_claims": [],
            },
            {"model": "deepseek-v4-flash-0731", "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        )

    result = writer.run(
        evidence_path=evidence,
        output_dir=output_root,
        destination_kind="researchflow",
        destination_path=record,
        destination_record_id="experiment",
        expected_destination_sha256="a" * 64,
        update_reason="adopt audited draft",
        provider_call=provider_call,
    )

    assert record.read_text(encoding="utf-8") == "# Original\n"
    packet = json.loads(Path(result["publication_packet_path"]).read_text(encoding="utf-8"))
    assert packet["commit_protocol"] == "researchflow_get_sha_put_with_409_reconcile"
    assert packet["destination_record_id"] == "experiment"
    assert packet["expected_destination_sha256"] == "a" * 64


def test_report_writer_refuses_managed_authority_output(tmp_path: Path, monkeypatch) -> None:
    evidence = tmp_path / "evidence.json"
    authority = tmp_path / "research" / "records"
    authority.mkdir(parents=True)
    _evidence(evidence)
    monkeypatch.chdir(tmp_path)

    try:
        writer.run(evidence_path=evidence, output_dir=authority, provider_call=lambda *args, **kwargs: ({}, {}))
    except ValueError as exc:
        assert "managed authority root" in str(exc)
    else:
        raise AssertionError("managed ResearchFlow directory must be write-protected")


def test_provider_uses_non_thinking_json_mode_and_one_corrective_retry(monkeypatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("BAILIAN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
    responses = [
        {
            "id": "first",
            "model": "deepseek-v4-flash-0731",
            "choices": [{"finish_reason": "stop", "message": {"content": "not-json"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        },
        {
            "id": "second",
            "model": "deepseek-v4-flash-0731",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            {
                                "report_markdown": "# Result",
                                "claims_used": ["c1"],
                                "warnings": [],
                                "unsupported_claims": [],
                            }
                        ),
                    },
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17},
        },
    ]

    class Response:
        status = 200

        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    requests = []

    def fake_urlopen(request, timeout):
        requests.append(json.loads(request.data.decode("utf-8")))
        return Response(responses.pop(0))

    monkeypatch.setattr(writer.urllib.request, "urlopen", fake_urlopen)
    generated, raw = writer._provider_request("system", "user", max_output_tokens=256)
    assert generated["report_markdown"] == "# Result"
    assert raw["usage"]["total_tokens"] == 29
    metadata = raw["_harness_response_metadata"]
    assert [item["parse_status"] for item in metadata] == ["non_json", "parsed"]
    assert metadata[1]["reasoning_content_present"] is False
    assert metadata[1]["selected_source"] == "content"
    assert all(item["enable_thinking"] is False for item in requests)
    assert all("reasoning_effort" not in item for item in requests)


def test_provider_http_error_preserves_only_safe_diagnostics(monkeypatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("BAILIAN_BASE_URL", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1")
    body = json.dumps(
        {
            "error": {
                "code": "ClientError",
                "type": "ClientError",
                "param": None,
                "message": "<400> InternalError.Algo: [Backend buffer overflow.]",
            },
            "request_id": "request-safe-id",
            "echoed_secret": "must-not-persist",
        }
    ).encode("utf-8")

    def fake_urlopen(request, timeout):
        raise writer.urllib.error.HTTPError(request.full_url, 400, "Bad Request", None, io.BytesIO(body))

    monkeypatch.setattr(writer.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(writer.ProviderResponseError) as raised:
        writer._provider_request("system", "user", max_output_tokens=256)
    metadata = raised.value.metadata[0]
    assert metadata["failure_category"] == "provider_internal"
    assert metadata["retryable"] is True
    assert metadata["provider_error"]["request_id"] == "request-safe-id"
    assert "must-not-persist" not in json.dumps(metadata)


def test_provider_failure_writes_metadata_not_prompt_or_secret(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    evidence = tmp_path / "evidence.json"
    _evidence(evidence)

    def failed_provider(system: str, user: str, *, max_output_tokens: int):
        raise writer.ProviderResponseError(
            "provider returned no parseable JSON object after one corrective retry",
            [{"response_id": "r1", "content_present": False, "parse_status": "empty"}],
        )

    with pytest.raises(writer.ProviderResponseError):
        writer.run(evidence_path=evidence, output_dir=tmp_path / "out", provider_call=failed_provider)
    failure = json.loads((tmp_path / "out" / "report_writer_failure.json").read_text(encoding="utf-8"))
    serialized = json.dumps(failure)
    assert failure["prompt_persisted"] is False
    assert failure["credential_persisted"] is False
    assert "AUC was 0.91" not in serialized
    assert "test-key" not in serialized
