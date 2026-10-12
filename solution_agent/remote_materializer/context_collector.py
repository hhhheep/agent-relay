from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from solution_agent.context_gateway import build_context_pack

from .context_policy import allowed_paths_for_level, decide_path, forbidden_paths_for_level
from .schema import now_iso
from .validators import CheckResult, validate_context_pack, validate_context_request_payload


PACKAGE_DIR = Path(__file__).resolve().parent


@dataclass
class ContextCollection:
    status: str
    checks: list[CheckResult]
    included_files: list[str]
    rejected_files: list[dict[str, str]]


def default_context_request(work_order: dict[str, Any]) -> dict[str, Any]:
    task_type = str(work_order.get("task_type") or "")
    context_level = "materialize"
    if task_type in {"material_regeneration", "failure_normalization"}:
        context_level = "prepare_config"
    return {
        "task_id": work_order.get("task_id"),
        "context_level": context_level,
        "needed_files": [],
        "reason": "default context request",
    }


def collect_context_pack(
    repo_root: Path,
    run_dir: Path,
    task_path: Path,
    work_order_path: Path,
    context_request: dict[str, Any] | None = None,
) -> ContextCollection:
    work_order = json.loads(work_order_path.read_text(encoding="utf-8"))
    request = context_request or default_context_request(work_order)
    pack_dir = run_dir / "context_pack"
    pack_dir.mkdir(parents=True, exist_ok=True)
    with (pack_dir / "task.md").open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(task_path.read_text(encoding="utf-8"))
    (pack_dir / "work_order.json").write_text(json.dumps(work_order, ensure_ascii=False, indent=2), encoding="utf-8")
    (pack_dir / "context_request.json").write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
    behavior = (PACKAGE_DIR / "templates" / "behavior_policy.md").read_text(encoding="utf-8")
    with (pack_dir / "behavior_policy.md").open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(behavior)
    gateway_manifest = build_context_pack(
        task_id=str(work_order.get("task_id") or task_path.parent.name),
        task_path=task_path,
        repo_root=repo_root,
        out_dir=run_dir,
        remote_worker=str(work_order.get("worker") or "remote_materializer"),
        workspace_profile=str(work_order.get("workspace_profile") or "local"),
        remote_conda_env=str(work_order.get("remote_conda_env") or "") or None,
    )

    context_level = str(request.get("context_level") or "")
    checks = [validate_context_request_payload(request)]
    allowed_yaml = {
        "context_level": context_level,
        "allowed_paths": allowed_paths_for_level(context_level),
        "forbidden_paths": forbidden_paths_for_level(context_level),
    }
    (pack_dir / "allowed_files.yaml").write_text(yaml.safe_dump(allowed_yaml, sort_keys=False), encoding="utf-8")

    included: list[str] = []
    rejected: list[dict[str, str]] = []
    relevant_chunks: list[str] = []
    for item in request.get("needed_files") or []:
        if isinstance(item, dict):
            path_str = str(item.get("path") or "").strip()
            reason_note = str(item.get("reason") or "").strip()
            if not path_str:
                rejected.append({"path": repr(item), "reason": "missing_path_key"})
                continue
        else:
            path_str = str(item)
            reason_note = ""
        decision = decide_path(context_level, path_str)
        if not decision.allowed:
            rejected.append({"path": decision.path, "reason": decision.reason})
            continue
        source = repo_root / decision.path
        if not source.exists() or not source.is_file():
            rejected.append({"path": decision.path, "reason": "missing_file"})
            continue
        text = source.read_text(encoding="utf-8", errors="replace")
        included.append(decision.path)
        header = f"## {decision.path}" + (f"  <!-- {reason_note} -->" if reason_note else "")
        relevant_chunks.append(f"{header}\n\n```text\n{text}\n```")

    with (pack_dir / "relevant_code.md").open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n\n".join(relevant_chunks))
    (pack_dir / "file_index_excerpt.md").write_text("\n".join(included) + ("\n" if included else ""), encoding="utf-8")
    (pack_dir / "recent_changes.md").write_text("No recent changes were included in this context pack.\n", encoding="utf-8")
    (pack_dir / "failing_tests.md").write_text("No failing test output was included in this context pack.\n", encoding="utf-8")
    (pack_dir / "expected_outputs.md").write_text(_expected_outputs_text(work_order), encoding="utf-8")
    (pack_dir / "memory.md").write_text("No remote materializer memory was included.\n", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "task_id": work_order.get("task_id"),
        "context_level": context_level,
        "created_at": now_iso(),
        "context_gateway": {
            "context_pack_id": gateway_manifest.get("context_pack_id"),
            "manifest": "manifest.yaml",
            "api_access_policy": "context_pack_only",
        },
        "included_files": included,
        "rejected_files": rejected,
        "source_context": "none" if not included else "selected",
    }
    (pack_dir / "context_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    checks.append(validate_context_pack(pack_dir, request))
    if rejected:
        check = CheckResult("context_collector")
        for item in rejected:
            check.fail(f"rejected {item['path']}: {item['reason']}")
        checks.append(check)
    status = "fail" if any(check.status != "pass" for check in checks) else "pass"
    return ContextCollection(status=status, checks=checks, included_files=included, rejected_files=rejected)


def _expected_outputs_text(work_order: dict[str, Any]) -> str:
    outputs = [str(item) for item in work_order.get("required_outputs") or []]
    if not outputs:
        return "No required outputs were declared.\n"
    return "\n".join(f"- {item}" for item in outputs) + "\n"
