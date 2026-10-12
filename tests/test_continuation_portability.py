from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml

from solution_agent.continuation.activation import activate_pending_to_inbox_stub, run_activation_preflight_contract_stub


def _copy_activation_contracts(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    source = repo_root / ".solution" / "continuation"
    target = tmp_path / ".solution" / "continuation"
    target.mkdir(parents=True, exist_ok=True)
    for src in source.glob("*.yaml"):
        (target / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def _patch_workspace(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("solution_agent.continuation.paths.root", lambda: tmp_path)
    _copy_activation_contracts(tmp_path)


def _bundle_hash(bundle_dir: Path) -> str:
    proposal_json = bundle_dir / "proposal.json"
    payload = json.loads(proposal_json.read_text(encoding="utf-8"))
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _write_bundle_artifacts(
    bundle_dir: Path,
    *,
    source_task_id: str,
    plan_id: str,
    proposal_id: str,
    source_result_hash: str,
    idempotency_key: str,
    proposal_path: str,
) -> dict:
    bundle_dir.mkdir(parents=True, exist_ok=True)
    proposal_payload = {
        "bundle_id": f"{source_task_id}_{plan_id}",
        "proposal_id": proposal_id,
        "source_task_id": source_task_id,
        "source_result_hash": source_result_hash,
        "plan_id": plan_id,
        "idempotency_key": idempotency_key,
        "proposal_count": 1,
        "proposals": [],
    }

    manifest_payload = {
        "manifest_id": f"{proposal_id}_manifest",
        "source_task_id": source_task_id,
        "source_result_hash": source_result_hash,
        "proposal_path": proposal_path,
        "plan_id": plan_id,
        "idempotency_key": idempotency_key,
        "proposal_count": 1,
        "claim_limitation": "local-only activation portability test",
        "forbidden_actions": [],
        "outputs": ["action_requests.json"],
    }
    gate_payload = {
        "report_id": f"{proposal_id}_gate",
        "bundle_id": f"{source_task_id}_{plan_id}",
        "source_task_id": source_task_id,
        "source_result_hash": source_result_hash,
        "idempotency_key": idempotency_key,
        "gate_status": "passed",
        "checks": [],
        "violations": [],
        "prompt_versions": {},
        "policy_version": "0.2.0",
    }

    (bundle_dir / "proposal.json").write_text(json.dumps(proposal_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    (bundle_dir / "proposal_dry_run_manifest.json").write_text(
        json.dumps(manifest_payload, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    (bundle_dir / "gate_report.json").write_text(json.dumps(gate_payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    (bundle_dir / "promotion_state.json").write_text(
        json.dumps(
            {
                "state": "promoted_pending",
                "updated_at": "2026-05-19T00:00:00Z",
                "updated_by": "test",
                "reason": "fixture",
                "target_task_id": f"{source_task_id}_{plan_id}",
                "proposal_hash": _bundle_hash(bundle_dir),
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (bundle_dir / "proposal.md").write_text("# Proposal\n\nportable fixture body.\n", encoding="utf-8")
    (bundle_dir / "promotion_history.jsonl").write_text(
        f'{{"event":"promotion","proposal_hash":"{_bundle_hash(bundle_dir)}","target_task_id":"{source_task_id}_{plan_id}"}}' + "\n",
        encoding="utf-8",
    )

    return {
        "target_task_id": f"{source_task_id}_{plan_id}",
        "proposal_id": proposal_id,
        "proposal_hash": _bundle_hash(bundle_dir),
        "source_task_id": source_task_id,
        "idempotency_key": idempotency_key,
    }


def _write_pending_task(tmp_path: Path, *, proposal_path: str, body: str = "Portable candidate body.") -> tuple[Path, dict]:
    source_task_id = "source_001"
    plan_id = "plan_001"
    proposal_id = "proposal_001"
    idempotency_key = "source_001:hash_001:0.1.0:plan"
    bundle_dir = tmp_path / "tasks" / "inbox" / "_proposed" / source_task_id / plan_id

    meta = _write_bundle_artifacts(
        bundle_dir,
        source_task_id=source_task_id,
        plan_id=plan_id,
        proposal_id=proposal_id,
        source_result_hash="hash_001",
        idempotency_key=idempotency_key,
        proposal_path=proposal_path,
    )

    front = {
        "task_id": meta["target_task_id"],
        "source_task_id": source_task_id,
        "source_result_hash": "hash_001",
        "proposal_id": proposal_id,
        "plan_id": plan_id,
        "bundle_id": f"{source_task_id}_{plan_id}",
        "proposal_count": 1,
        "claim_limitation": "local-only activation portability test",
        "forbidden_actions": [],
        "outputs": ["action_requests.json"],
        "idempotency_key": idempotency_key,
        "activation_approved": True,
    }

    pending_root = tmp_path / "tasks" / "inbox" / "_promoted_pending"
    pending_root.mkdir(parents=True, exist_ok=True)
    pending_path = pending_root / f"{meta['target_task_id']}.md"
    pending_path.write_text(
        "---\n" + yaml.safe_dump(front, sort_keys=False) + "---\n" + body + "\n",
        encoding="utf-8",
    )

    audit_path = tmp_path / "continuation" / "audit" / "events.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_payload = {
        "action": "promote",
        "target": meta["target_task_id"],
        "source_task_id": source_task_id,
        "inputs_hash": meta["proposal_hash"],
        "proposal_hash": meta["proposal_hash"],
    }
    if audit_path.exists():
        rows = [line for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        rows = []
    rows.append(json.dumps(audit_payload, sort_keys=True))
    audit_path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    return pending_path, meta


def _read_front_matter(text: str) -> dict:
    normalized = text.lstrip("\ufeff")
    if not normalized.startswith("---"):
        return {}
    parts = normalized.split("---", 2)
    if len(parts) < 3:
        return {}
    payload = yaml.safe_load(parts[1])
    return payload if isinstance(payload, dict) else {}


@pytest.mark.parametrize(
    "proposal_path",
    [
        "/tmp/continuation/_proposed/source_001/plan_001/proposal.md",
        "/mnt/data/continuation/_proposed/source_001/plan_001/proposal.md",
        "/ssd6/continuation/_proposed/source_001/plan_001/proposal.md",
        r"\\\\fileserver\\share\\continuation\\_proposed\\source_001\\plan_001\\proposal.md",
        r"Z:\\tmp\\continuation\\_proposed\\source_001\\plan_001\\proposal.md",
    ],
)
def test_activation_preflight_and_write_accepts_platform_probe_paths(tmp_path, monkeypatch, proposal_path: str):
    _patch_workspace(tmp_path, monkeypatch)
    pending, _ = _write_pending_task(tmp_path, proposal_path=proposal_path)

    preflight = run_activation_preflight_contract_stub(pending)
    assert preflight["ok"] is True
    assert preflight["pending_path"] == str(pending)

    result = activate_pending_to_inbox_stub(pending)
    assert result["ok"] is True
    assert result["written"] is True


def test_activation_output_is_utf8_text_with_trailing_newline(tmp_path, monkeypatch):
    _patch_workspace(tmp_path, monkeypatch)
    pending, _ = _write_pending_task(tmp_path, proposal_path="/tmp/continuation/proposal.md", body="Portable newline check 🚀")

    result = activate_pending_to_inbox_stub(pending)
    assert result["ok"] is True
    active_path = Path(result["active_target"])
    raw = active_path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8")
    assert text.endswith("\n")
    front = _read_front_matter(text)
    assert front["task_id"] == "source_001_plan_001"
    assert isinstance(front["claim_limitation"], str)


def test_continuation_activation_code_prefers_pathlib_helpers():
    source = (Path(__file__).resolve().parents[1] / "solution_agent" / "continuation" / "activation.py").read_text(encoding="utf-8")
    assert "from pathlib import Path" in source
    assert "os.path" not in source
    # backslash-only fragments are intentionally allowed only for portability test fixtures and regex patterns
    allowed_backslash_fragments = [
        "/ssd\\\\d+/",
        r"\\\\[^\\\\]+\\\\[^\\\\]+",
    ]
    for fragment in allowed_backslash_fragments:
        assert fragment in source or re.search(fragment, source) is not None
