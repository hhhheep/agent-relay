from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .artifact_parser import parse_fenced_artifacts, write_artifacts
from .contract_artifacts import (
    contract_artifact_request_from_task,
    contract_artifact_request_is_valid,
    expand_contract_artifact_request,
    repair_csv_artifact_tables,
)
from .context_collector import collect_context_pack, default_context_request
from .materializer_policy import direct_closure_policy
from .model_clients import MaterializerClient, ReaderClient, ReaderVerifierClient
from .schema import now_iso
from .task_loader import load_task
from .validators import load_work_order, summarize, validate_generated_bundle, validate_verifier_patches


PACKAGE_DIR = Path(__file__).resolve().parent
_ALLOWED_TASK_TYPES = {
    "training_validation",
    "probe_inference",
    "material_regeneration",
    "failure_normalization",
}
_TASK_TYPE_ALIASES = {
    "analysis": "probe_inference",
    "audit": "probe_inference",
    "decision": "probe_inference",
    "diagnosis": "probe_inference",
    "evidence_synthesis": "probe_inference",
    "inventory": "probe_inference",
    "judge": "probe_inference",
    "report": "probe_inference",
    "review": "probe_inference",
    "synthesis": "probe_inference",
}
_DEFAULT_FORBIDDEN_ACTIONS = [
    "modify_source_code",
    "execute_training",
    "change_dataset_split",
    "overwrite_checkpoint",
    "delete_files",
]


def _verifier_trigger_reasons(work_order: dict[str, Any], reader_meta: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    if reader_meta.get("requires_verifier"):
        reasons.append("reader_meta.requires_verifier")
    try:
        confidence = float(reader_meta.get("confidence", 1.0))
    except (TypeError, ValueError):
        confidence = 1.0
    if confidence < 0.85:
        reasons.append(f"confidence_below_threshold({confidence:.2f})")
    if work_order.get("task_type") == "patch_low_risk":
        reasons.append("task_type_patch_low_risk")
    return reasons


def _needs_verifier(work_order: dict[str, Any], reader_meta: dict[str, Any]) -> bool:
    return bool(_verifier_trigger_reasons(work_order, reader_meta))


@dataclass
class MaterializerRun:
    task_path: Path
    output_root: Path
    reader_client: ReaderClient
    materializer_client: MaterializerClient
    backup_materializer_client: MaterializerClient | None = None
    reader_verifier_client: ReaderVerifierClient | None = None
    feedback_text: str = ""
    workspace_root: Path | None = None  # if set, write execution_spec.yaml on materialized

    def run(self) -> dict[str, Any]:
        task = load_task(self.task_path)
        run_dir = self.output_root / task.task_id
        raw_dir = run_dir / "raw"
        generated_dir = run_dir / "generated"
        validation_dir = run_dir / "validation"
        for directory in (raw_dir, generated_dir, validation_dir):
            directory.mkdir(parents=True, exist_ok=True)
        started_at = now_iso()
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "task_id": task.task_id,
            "source_task_path": str(self.task_path),
            "run_dir": str(run_dir),
            "started_at": started_at,
            "finished_at": None,
            "attempts": [],
            "notes": [
                "remote_materializer does not execute training",
                "remote_materializer does not mutate registry",
            ],
        }
        self._write_manifest(run_dir, manifest)

        task_markdown = self.task_path.read_text(encoding="utf-8")
        schema_text = _read_text(PACKAGE_DIR / "schemas" / "work_order.schema.json")
        reader_prompt = _read_text(PACKAGE_DIR / "prompts" / "reader_prompt.md")
        reader_raw = self.reader_client.create_work_order(task_markdown, schema_text, reader_prompt)
        (raw_dir / "reader_raw.json").write_text(reader_raw, encoding="utf-8")
        work_order_path = run_dir / "work_order.json"
        work_order_path.write_text(_extract_json_object(reader_raw), encoding="utf-8")

        work_order, work_order_check = load_work_order(work_order_path)
        if work_order.get("status") == "ambiguous":
            normalized = _normalize_ambiguous_work_order(task, self.task_path, work_order)
            if normalized is not None:
                work_order_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
                work_order, work_order_check = load_work_order(work_order_path)
        else:
            normalized = _normalize_ready_work_order(task, self.task_path, work_order)
            if normalized != work_order:
                work_order_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
                work_order, work_order_check = load_work_order(work_order_path)
        routing_log: dict[str, Any] = {
            "reader_verifier_configured": self.reader_verifier_client is not None,
            "reader_verifier_triggered": False,
            "trigger_reasons": [],
            "verifier_review_status": None,
            "verifier_patches_applied": False,
            "verifier_patches_rejected": False,
            "primary_materializer_attempts": 0,
            "backup_materializer_configured": self.backup_materializer_client is not None,
            "backup_materializer_triggered": False,
        }
        if work_order_check.status != "pass":
            return self._finish(run_dir, task.task_id, "validation_failed", work_order, [work_order_check], 0, "work_order validation failed", manifest, routing_log)
        if work_order.get("status") == "ambiguous":
            return self._finish(run_dir, task.task_id, "ambiguous", work_order, [work_order_check], 0, str(work_order.get("reason") or "ambiguous task"), manifest, routing_log)

        reader_meta = work_order.get("reader_meta") or {}
        trigger_reasons = _verifier_trigger_reasons(work_order, reader_meta)
        if self.reader_verifier_client is not None and trigger_reasons:
            routing_log["reader_verifier_triggered"] = True
            routing_log["trigger_reasons"] = trigger_reasons
            verifier_prompt = _read_text(PACKAGE_DIR / "prompts" / "reader_verifier_prompt.md")
            verifier_raw = self.reader_verifier_client.verify_work_order(
                task_markdown, json.dumps(work_order, ensure_ascii=False, indent=2), verifier_prompt
            )
            (raw_dir / "reader_verifier_raw.json").write_text(verifier_raw, encoding="utf-8")
            try:
                review = json.loads(_extract_json_object(verifier_raw))
            except Exception:
                review = {"review_status": "pass"}
            review_status = str(review.get("review_status") or "pass")
            routing_log["verifier_review_status"] = review_status
            manifest["reader_verifier"] = {"status": review_status, "issues": list(review.get("issues") or [])}
            self._write_manifest(run_dir, manifest)
            if review_status == "escalate":
                return self._finish(run_dir, task.task_id, "escalation_required", work_order, [work_order_check], 0, "reader_verifier_escalated", manifest, routing_log)
            if review_status == "revise_required":
                patches = review.get("recommended_work_order_patches") or {}
                if patches and isinstance(patches, dict):
                    patch_check = validate_verifier_patches(patches, work_order)
                    if patch_check.status == "pass":
                        work_order = {**work_order, **patches}
                        work_order_path.write_text(json.dumps(work_order, ensure_ascii=False, indent=2), encoding="utf-8")
                        routing_log["verifier_patches_applied"] = True
                        manifest["reader_verifier"]["patches_applied"] = True
                    else:
                        routing_log["verifier_patches_rejected"] = True
                        manifest["reader_verifier"]["patches_rejected"] = patch_check.errors
                    self._write_manifest(run_dir, manifest)

        context_request = work_order.get("context_request")
        if not isinstance(context_request, dict):
            context_request = default_context_request(work_order)
        context_collection = collect_context_pack(
            repo_root=Path.cwd(),
            run_dir=run_dir,
            task_path=self.task_path,
            work_order_path=work_order_path,
            context_request=context_request,
        )
        manifest["context_pack"] = {
            "status": context_collection.status,
            "included_files": context_collection.included_files,
            "rejected_files": context_collection.rejected_files,
        }
        self._write_manifest(run_dir, manifest)
        if context_collection.status != "pass":
            self._write_escalation_packet(run_dir, work_order, context_collection.checks)
            return self._finish(
                run_dir, task.task_id, "escalation_required", work_order,
                [work_order_check, *context_collection.checks], 0,
                "context_pack_validation_failed", manifest, routing_log,
            )

        qwen_prompt = _read_text(PACKAGE_DIR / "prompts" / "qwen_materializer_prompt.md")
        templates_text = _read_context_pack(run_dir / "context_pack")
        schemas_text = _read_many(PACKAGE_DIR / "schemas")
        # When feedback_text is set the RetryController owns the outer loop; run single attempt.
        if self.feedback_text:
            max_retries = 0
        else:
            max_retries = int((work_order.get("retry_policy") or {}).get("qwen_max_retries", 2) or 2)
        all_checks = [work_order_check]
        retry_count = 0
        for attempt in range(max_retries + 1):
            retry_count = attempt
            attempt_started_at = now_iso()
            qwen_raw = self.materializer_client.materialize(
                json.dumps(work_order, ensure_ascii=False, indent=2),
                qwen_prompt, templates_text, schemas_text,
                self.feedback_text,
            )
            (raw_dir / f"qwen_materializer_raw_attempt_{attempt + 1}.md").write_text(qwen_raw, encoding="utf-8")
            artifacts = parse_fenced_artifacts(qwen_raw)
            write_artifacts(artifacts, generated_dir, _writable_materializer_outputs(work_order))
            _ensure_materialized_execution_helpers(generated_dir, task, work_order)
            checks = [work_order_check, *validate_generated_bundle(generated_dir, work_order)]
            all_checks = checks
            attempt_summary = summarize(checks)
            routing_log["primary_materializer_attempts"] = attempt + 1
            manifest["attempts"].append({
                "attempt": attempt + 1,
                "started_at": attempt_started_at,
                "finished_at": now_iso(),
                "validation_status": attempt_summary["status"],
                "failed_validators": attempt_summary["failed_validators"],
                "raw_output": f"raw/qwen_materializer_raw_attempt_{attempt + 1}.md",
            })
            self._write_manifest(run_dir, manifest)
            if summarize(checks)["status"] == "pass":
                return self._finish(run_dir, task.task_id, "materialized", work_order, checks, retry_count, None, manifest, routing_log, task.execution)

        if self.backup_materializer_client is not None:
            routing_log["backup_materializer_triggered"] = True
            backup_started_at = now_iso()
            backup_raw = self.backup_materializer_client.materialize(
                json.dumps(work_order, ensure_ascii=False, indent=2),
                qwen_prompt, templates_text, schemas_text,
            )
            (raw_dir / "backup_materializer_raw.md").write_text(backup_raw, encoding="utf-8")
            artifacts = parse_fenced_artifacts(backup_raw)
            write_artifacts(artifacts, generated_dir, _writable_materializer_outputs(work_order))
            _ensure_materialized_execution_helpers(generated_dir, task, work_order)
            checks = [work_order_check, *validate_generated_bundle(generated_dir, work_order)]
            all_checks = checks
            backup_summary = summarize(checks)
            manifest["attempts"].append({
                "attempt": "backup",
                "started_at": backup_started_at,
                "finished_at": now_iso(),
                "validation_status": backup_summary["status"],
                "failed_validators": backup_summary["failed_validators"],
                "raw_output": "raw/backup_materializer_raw.md",
            })
            self._write_manifest(run_dir, manifest)
            if backup_summary["status"] == "pass":
                return self._finish(run_dir, task.task_id, "materialized", work_order, checks, retry_count, None, manifest, routing_log, task.execution)

        self._write_escalation_packet(run_dir, work_order, all_checks)
        return self._finish(run_dir, task.task_id, "escalation_required", work_order, all_checks, retry_count, "validator_failed_after_retries", manifest, routing_log)

    def _finish(  # noqa: PLR0913
        self,
        run_dir: Path,
        task_id: str,
        status: str,
        work_order: dict[str, Any],
        checks: list[Any],
        qwen_retry_count: int,
        escalation_reason: str | None,
        manifest: dict[str, Any] | None = None,
        routing_log: dict[str, Any] | None = None,
        execution_block: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        validation_dir = run_dir / "validation"
        validation_summary = summarize(checks)
        validation_payload = {
            "task_id": task_id,
            "status": validation_summary["status"],
            **validation_summary,
            "created_at": now_iso(),
        }
        (validation_dir / "validation_result.json").write_text(json.dumps(validation_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        (validation_dir / "validator_stdout.log").write_text(json.dumps(validation_summary, ensure_ascii=False, indent=2), encoding="utf-8")
        (validation_dir / "validator_stderr.log").write_text("", encoding="utf-8")
        generated_files = sorted(str(path.relative_to(run_dir)).replace("\\", "/") for path in (run_dir / "generated").glob("*") if path.is_file())
        validation_errors: list[str] = [
            error
            for check in checks
            for error in (check.errors if hasattr(check, "errors") else [])
        ]
        package = {
            "task_id": task_id,
            "task_type": work_order.get("task_type"),
            "status": status,
            "summary_short": _build_summary_short(status, escalation_reason, validation_summary),
            "generated_files": generated_files,
            "validation_summary": {
                "status": validation_summary["status"],
                "failed_validators": validation_summary["failed_validators"],
            },
            "validation_errors": validation_errors,
            "retry_count": {"reader": 0, "qwen": qwen_retry_count},
            "escalation_reason": escalation_reason,
            "generated_dir": "generated" if generated_files else None,
            "context_pack_dir": "context_pack" if (run_dir / "context_pack").exists() else None,
            "routing_decisions_path": "routing_decisions.json" if routing_log is not None else None,
            "model_usage_path": "model_usage.json" if routing_log is not None else None,
            "created_at": now_iso(),
        }
        (run_dir / "result_package.yaml").write_text(yaml.safe_dump(package, sort_keys=False, allow_unicode=True), encoding="utf-8")

        # Write execution_spec.yaml when materialized + execution block present + workspace_root known
        if status == "materialized" and execution_block and execution_block.get("script_id") and self.workspace_root:
            from ..execution_port.spec_builder import write_execution_spec
            write_execution_spec(
                execution=execution_block,
                task_id=task_id,
                task_type=str(work_order.get("task_type") or ""),
                worktree=self.workspace_root,
                dest=run_dir / "execution_spec.yaml",
            )
            package["execution_spec"] = "execution_spec.yaml"
        elif status == "materialized":
            generated_spec = run_dir / "generated" / "execution_spec.yaml"
            if generated_spec.exists():
                (run_dir / "execution_spec.yaml").write_text(generated_spec.read_text(encoding="utf-8"), encoding="utf-8")
                package["execution_spec"] = "execution_spec.yaml"
        if routing_log is not None:
            routing_log["final_status"] = status
            (run_dir / "routing_decisions.json").write_text(json.dumps(routing_log, ensure_ascii=False, indent=2), encoding="utf-8")
            model_usage = {
                "reader": {"calls": 1},
                "reader_verifier": {
                    "configured": routing_log.get("reader_verifier_configured", False),
                    "calls": 1 if routing_log.get("reader_verifier_triggered") else 0,
                },
                "primary_materializer": {
                    "calls": routing_log.get("primary_materializer_attempts", 0),
                },
                "backup_materializer": {
                    "configured": routing_log.get("backup_materializer_configured", False),
                    "calls": 1 if routing_log.get("backup_materializer_triggered") else 0,
                },
            }
            (run_dir / "model_usage.json").write_text(json.dumps(model_usage, ensure_ascii=False, indent=2), encoding="utf-8")
        if manifest is not None:
            manifest["finished_at"] = package["created_at"]
            manifest["status"] = status
            self._write_manifest(run_dir, manifest)
        return package

    def _write_escalation_packet(self, run_dir: Path, work_order: dict[str, Any], checks: list[Any]) -> None:
        summary = summarize(checks)
        lines = [
            "# Escalation Packet",
            "",
            f"- task_id: {work_order.get('task_id')}",
            f"- task_type: {work_order.get('task_type')}",
            "- reason: validator_failed_after_retries",
            "",
            "## Failed Validators",
            "",
        ]
        lines.extend(f"- {item}" for item in summary["failed_validators"])
        (run_dir / "escalation_packet.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _write_manifest(self, run_dir: Path, manifest: dict[str, Any]) -> None:
        (run_dir / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_summary_short(status: str, escalation_reason: str | None, validation_summary: dict[str, Any]) -> str:
    if status == "materialized":
        return "Artifacts generated and validated."
    if status == "ambiguous":
        return f"Task is ambiguous: {escalation_reason or 'reason unknown'}"
    if status == "validation_failed":
        failed = validation_summary.get("failed_validators") or []
        return f"Validation failed: {', '.join(str(v) for v in failed[:3])}" if failed else "Validation failed."
    if status == "escalation_required":
        return f"Escalation required: {escalation_reason or 'reason unknown'}"
    return f"remote_materializer status: {status}"


def _writable_materializer_outputs(work_order: dict[str, Any]) -> list[str]:
    outputs = [str(item) for item in work_order.get("required_outputs") or []]
    if "contract_artifact_request.yaml" not in outputs:
        outputs.append("contract_artifact_request.yaml")
    return outputs


def _normalize_ambiguous_work_order(task: Any, task_path: Path, reader_payload: dict[str, Any]) -> dict[str, Any] | None:
    """Derive machine-owned work_order fields when the reader is too cautious."""
    raw_task_type = str((task.raw_front_matter or {}).get("task_type") or "").strip()
    task_type = _normalize_materializer_task_type(raw_task_type, task)
    if task_type not in _ALLOWED_TASK_TYPES:
        return None
    required_outputs = _required_outputs_from_task(task)
    if not required_outputs:
        return None
    allowed_root = str((task.raw_front_matter or {}).get("allowed_output_root") or "").strip()
    if not allowed_root:
        allowed_root = f"runs/{task.task_id}/"
    if not allowed_root.endswith("/"):
        allowed_root += "/"
    return {
        "status": "ready",
        "task_id": task.task_id,
        "task_type": task_type,
        "source_task_path": str(task_path),
        "jobs": [
            {
                "name": "materialize_required_outputs",
                "title": task.title,
                "goal": _first_body_line(task.body) or task.title,
            }
        ],
        "required_outputs": required_outputs,
        "input_files": _input_files_from_task(task.body),
        "allowed_output_root": allowed_root,
        "forbidden_actions": list(_DEFAULT_FORBIDDEN_ACTIONS),
        "validation_steps": [
            "validate work_order schema",
            "validate generated files",
            "validate commands.md allowlist",
            "validate output_contract.yaml required_files",
        ],
        "retry_policy": {
            "reader_max_retries": 1,
            "qwen_max_retries": 2,
        },
        "escalation_policy": {
            "escalate_if": [
                "required_outputs_missing",
                "validation_failed_after_retries",
                "task_requires_source_code_changes",
                "task_requires_arbitrary_shell_execution",
            ]
        },
        "reader_meta": {
            "normalized_from_ambiguous": True,
            "reader_reason": reader_payload.get("reason"),
            "front_matter_task_type": raw_task_type,
            "normalized_task_type": task_type,
        },
    }


def _normalize_ready_work_order(task: Any, task_path: Path, reader_payload: dict[str, Any]) -> dict[str, Any]:
    """Fill machine-owned safety defaults without weakening validator checks."""
    if not isinstance(reader_payload, dict):
        return reader_payload
    normalized = dict(reader_payload)
    if not str(normalized.get("source_task_path") or "").strip():
        normalized["source_task_path"] = str(task_path)
    raw_task_type = str(normalized.get("task_type") or (task.raw_front_matter or {}).get("task_type") or "").strip()
    normalized_task_type = _normalize_materializer_task_type(raw_task_type, task)
    if normalized_task_type:
        normalized["task_type"] = normalized_task_type
    explicit_materializer_outputs = _materializer_required_outputs_from_task(task)
    if explicit_materializer_outputs:
        normalized["required_outputs"] = explicit_materializer_outputs
    elif getattr(task, "worker", "") == "materialized_execution":
        normalized["required_outputs"] = ["execution_plan.json", "execution_spec.yaml"]
    existing_forbidden = normalized.get("forbidden_actions")
    forbidden = [str(item) for item in existing_forbidden] if isinstance(existing_forbidden, list) else []
    for action in _DEFAULT_FORBIDDEN_ACTIONS:
        if action not in forbidden:
            forbidden.append(action)
    normalized["forbidden_actions"] = forbidden
    reader_meta = normalized.get("reader_meta")
    if not isinstance(reader_meta, dict):
        reader_meta = {}
    _strip_reader_invented_paths(normalized, reader_meta, task.body)
    reader_meta.setdefault("normalized_safety_defaults", True)
    if raw_task_type and normalized_task_type and raw_task_type != normalized_task_type:
        reader_meta["front_matter_task_type"] = raw_task_type
        reader_meta["normalized_task_type"] = normalized_task_type
        reader_meta["task_type_alias_policy"] = "human-facing report/analysis/audit aliases normalize to materializer-safe probe_inference"
    if explicit_materializer_outputs:
        closure_policy = direct_closure_policy()
        reader_meta["materializer_required_outputs_source"] = closure_policy.required_outputs_source
        if _all_outputs_are_closure_artifacts(explicit_materializer_outputs):
            reader_meta["materializer_output_mode"] = closure_policy.mode_name
            reader_meta.update(closure_policy.reader_meta)
    elif getattr(task, "worker", "") == "materialized_execution":
        reader_meta["materializer_required_outputs_source"] = "materialized_execution_default_plan_and_execution_spec"
        reader_meta["execution_required_outputs"] = _execution_required_outputs_from_task(task)
    normalized["reader_meta"] = reader_meta
    return normalized


def _normalize_materializer_task_type(raw_task_type: str, task: Any) -> str:
    task_type = str(raw_task_type or "").strip().lower()
    if task_type in _ALLOWED_TASK_TYPES:
        return task_type
    if task_type in _TASK_TYPE_ALIASES:
        return _TASK_TYPE_ALIASES[task_type]
    stage = str((getattr(task, "raw_front_matter", {}) or {}).get("execution_stage") or "").strip().lower()
    if stage in _TASK_TYPE_ALIASES:
        return _TASK_TYPE_ALIASES[stage]
    return task_type


def _strip_reader_invented_paths(work_order: dict[str, Any], reader_meta: dict[str, Any], task_body: str) -> None:
    """Keep Reader from turning implicit asset roles into fake paths.

    Reader runs before context_pack is assembled. It may identify that a
    checkpoint, split, or label file is needed, but concrete paths are only
    trusted when they were explicitly present in the task text. Otherwise the
    materializer must resolve the role from context_pack/workspace_context or
    stop with missing_context instead of inheriting a guessed path.
    """
    task_text = str(task_body or "")
    removed: dict[str, list[str]] = {}
    for field in ("input_files",):
        values = work_order.get(field)
        if not isinstance(values, list):
            continue
        kept: list[Any] = []
        for value in values:
            text = str(value)
            if _reader_path_is_trusted(text, task_text):
                kept.append(value)
            else:
                removed.setdefault(field, []).append(text)
        work_order[field] = kept
    upstream = reader_meta.get("upstream_inputs_required")
    if isinstance(upstream, list):
        kept = []
        for value in upstream:
            text = str(value)
            if _reader_path_is_trusted(text, task_text):
                kept.append(value)
            else:
                removed.setdefault("reader_meta.upstream_inputs_required", []).append(text)
        reader_meta["upstream_inputs_required"] = kept
    if removed:
        reader_meta["removed_untrusted_reader_paths"] = removed
        reader_meta["path_resolution_policy"] = (
            "Concrete input paths removed unless explicitly present in task.md; "
            "materializer must resolve asset roles from context_pack."
        )


def _reader_path_is_trusted(value: str, task_text: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    if stripped.startswith(("role:", "known_checkpoint:", "project_asset:", "artifact:")):
        return True
    if not _looks_like_path(stripped):
        return True
    return stripped in task_text


def _looks_like_path(value: str) -> bool:
    lowered = value.lower()
    if "/" in value or "\\" in value:
        return True
    return lowered.endswith((
        ".pt", ".pth", ".ckpt", ".safetensors", ".csv", ".json", ".yaml",
        ".yml", ".parquet", ".txt", ".png", ".jpg", ".jpeg",
    ))


def _ensure_materialized_execution_helpers(generated_dir: Path, task: Any, work_order: dict[str, Any]) -> None:
    required = {str(item) for item in work_order.get("required_outputs") or []}
    request_path = generated_dir / "contract_artifact_request.yaml"
    if request_path.exists() and not contract_artifact_request_is_valid(request_path):
        outputs = _safe_execution_output_names([item for item in required if item != "execution_spec.yaml"])
        if outputs:
            request = contract_artifact_request_from_task(task, outputs)
            request_path.write_text(yaml.safe_dump(request, sort_keys=False, allow_unicode=False), encoding="utf-8")
    expand_contract_artifact_request(generated_dir, required)
    repair_csv_artifact_tables(generated_dir, required)
    if task.execution and "commands.md" in required:
        commands = generated_dir / "commands.md"
        if not commands.exists():
            script_id = str(task.execution.get("script_id") or "")
            commands.write_text(
                "# Commands\n\n"
                "This file is human-readable only and is not executed by the runner.\n\n"
                f"- execution_port will run script_id `{script_id}` from the validated execution_spec.yaml.\n",
                encoding="utf-8",
            )
    if task.execution and "output_contract.yaml" in required:
        contract = generated_dir / "output_contract.yaml"
        if not contract.exists():
            required_artifacts = [str(item) for item in task.execution.get("required_artifacts") or []]
            payload = {"required_files": required_artifacts or ["metrics.json"]}
            contract.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    if "execution_spec.yaml" in required and not (generated_dir / "execution_spec.yaml").exists():
        _try_write_default_execution_spec(generated_dir, task, work_order)
        expand_contract_artifact_request(generated_dir, required)


def _try_write_default_execution_spec(generated_dir: Path, task: Any, work_order: dict[str, Any]) -> None:
    if task.execution:
        return
    if getattr(task, "worker", "") != "materialized_execution":
        return
    registry = _enrich_script_registry_excerpt(_load_context_yaml(generated_dir, "script_registry_excerpt.yaml"))
    scripts = registry.get("scripts") if isinstance(registry.get("scripts"), dict) else {}
    plan = _load_generated_json(generated_dir / "execution_plan.json")
    script_id = _infer_default_script_id(task, plan, scripts, registry)
    if not script_id:
        return

    paths = _default_remote_paths(generated_dir, task)
    output_dir = paths["output_dir"]
    artifact_roots = _unique_nonempty([*paths["artifact_roots"], output_dir])
    reader_meta = work_order.get("reader_meta") if isinstance(work_order.get("reader_meta"), dict) else {}
    explicit_materializer_outputs = (
        reader_meta.get("materializer_required_outputs_source") == "task_explicit_materializer_outputs"
    )
    outputs = []
    if explicit_materializer_outputs:
        outputs = _safe_execution_output_names([
            item
            for item in (work_order.get("required_outputs") or [])
            if str(item).strip() != "execution_spec.yaml"
        ])
    if not outputs:
        outputs = _safe_execution_output_names(_execution_required_outputs_from_task(task))
    script_entry = scripts.get(script_id) if isinstance(scripts.get(script_id), dict) else {}
    default_execution = script_entry.get("default_execution") if isinstance(script_entry.get("default_execution"), dict) else {}
    default_kind = str(default_execution.get("kind") or "").strip()
    if default_kind == "contract_artifact_writer":
        if not outputs:
            outputs = ["artifact_summary.md"]
        request_path = generated_dir / "contract_artifact_request.yaml"
        if not contract_artifact_request_is_valid(request_path):
            request = contract_artifact_request_from_task(task, outputs)
            request_path.write_text(yaml.safe_dump(request, sort_keys=False, allow_unicode=False), encoding="utf-8")
        config_arg = _remote_generated_file_path(generated_dir, request_path)
        command_args: dict[str, Any] = {"config": config_arg, "output_dir": output_dir}
        timeout_sec = 1800
        required_artifacts = [f"{output_dir.rstrip('/')}/{name}" for name in outputs]
        reason = "materialized_execution task did not provide execution block; normalizer synthesized a report/artifact writer handoff"
    elif default_execution:
        command_args = _default_command_args_from_registry(default_execution, output_dir, plan)
        timeout_sec = _safe_int(default_execution.get("timeout_sec"), _safe_int(script_entry.get("max_timeout_sec"), 3600))
        if not outputs:
            outputs = _default_required_files_from_registry(default_execution, script_entry)
        if not outputs:
            return
        required_artifacts = [f"{output_dir.rstrip('/')}/{name}" for name in outputs]
        reason = "materialized_execution task did not provide execution block; normalizer synthesized a registry-declared default execution handoff"
    else:
        return

    spec = {
        "schema_version": 1,
        "task_id": task.task_id,
        "job_type": "materialized_execution",
        "workspace": {
            "root": paths["workspace_root"],
            "cwd": paths["workspace_root"],
            "artifact_roots": artifact_roots,
        },
        "command": {
            "script_id": script_id,
            "args": command_args,
            "timeout_sec": timeout_sec,
        },
        "resources": {"gpu_required": False, "gpu_count": 0},
        "artifacts": {"required": required_artifacts},
        "success_criteria": {"exit_code": 0, "metrics_file": "", "metric_keys": []},
        "solution_agent_normalizations": [
            {
                "type": "default_execution_spec_from_task_intent",
                "reason": reason,
                "claim_policy": "missing evidence may limit claims, but it must not block bounded observation",
                "source": "remote_materializer.normalizer",
            }
        ],
    }
    (generated_dir / "execution_spec.yaml").write_text(
        yaml.safe_dump(spec, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    decision = {
        "task_id": task.task_id,
        "status": "execution_spec_synthesized",
        "script_id": script_id,
        "reason": reason,
        "required_artifacts": required_artifacts,
    }
    (generated_dir / "normalizer_decision.json").write_text(
        json.dumps(decision, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _infer_default_script_id(task: Any, plan: dict[str, Any], scripts: dict[str, Any], registry: dict[str, Any] | None = None) -> str:
    task_identity = "\n".join([str(task.task_id), str(getattr(task, "title", ""))]).lower()
    output_names = [name.lower() for name in _safe_execution_output_names(_execution_required_outputs_from_task(task))]
    text = "\n".join(
        [
            str(task.task_id),
            str(getattr(task, "title", "")),
            str(getattr(task, "body", "")),
            json.dumps(plan, ensure_ascii=False, sort_keys=True) if plan else "",
        ]
    ).lower()
    registry_choice = _registry_route_script_id(task, plan, scripts, registry or {}, output_names, task_identity, text)
    if registry_choice:
        return registry_choice
    return ""


def _registry_route_script_id(
    task: Any,
    plan: dict[str, Any],
    scripts: dict[str, Any],
    registry: dict[str, Any],
    output_names: list[str],
    task_identity: str,
    text: str,
) -> str:
    if _mixed_contract_and_experiment_outputs(output_names, registry):
        return ""
    stage = str((task.raw_front_matter or {}).get("execution_stage") or "").strip().lower()
    best: tuple[int, str] = (0, "")
    for script_id, entry in scripts.items():
        if not isinstance(entry, dict):
            continue
        route = entry.get("route_when") if isinstance(entry.get("route_when"), dict) else {}
        if not route:
            continue
        score = _route_score(route, registry, output_names, task_identity, text, stage, plan)
        if score > best[0]:
            best = (score, str(script_id))
    return best[1]


def _route_score(
    route: dict[str, Any],
    registry: dict[str, Any],
    output_names: list[str],
    task_identity: str,
    text: str,
    stage: str,
    plan: dict[str, Any],
) -> int:
    score = 0
    output_prefixes = _lower_list(route.get("output_prefixes"))
    if output_prefixes and any(name.startswith(tuple(output_prefixes)) for name in output_names):
        score += 100
    identity_terms = _lower_list(route.get("identity_terms"))
    if identity_terms and any(term in task_identity for term in identity_terms):
        score += 70
    strategy_terms = _lower_list(route.get("strategy_terms"))
    strategy = str(plan.get("execution_strategy") or "").lower()
    if strategy_terms and any(term in strategy for term in strategy_terms):
        score += 60
    text_terms = _lower_list(route.get("text_terms"))
    if text_terms and any(term in text for term in text_terms):
        score += 30
    stages = _lower_list(route.get("stages"))
    if stages and stage in stages:
        score += 20
    family = str(route.get("all_outputs_family") or "").strip()
    if family and output_names and all(_output_in_artifact_family(name, family, registry) for name in output_names):
        score += 130
    return score


def _mixed_contract_and_experiment_outputs(output_names: list[str], registry: dict[str, Any]) -> bool:
    if len(output_names) < 2:
        return False
    has_contract = any(
        _output_in_artifact_family(name, "contract_artifact", registry)
        and _normalized_output_name(name) not in {"action_requests.json", "artifact_summary.md", "worker_notes.md"}
        for name in output_names
    )
    has_real_metric = any(_looks_like_real_experiment_output(name) for name in output_names)
    return has_contract and has_real_metric


def _looks_like_real_experiment_output(name: str) -> bool:
    normalized = _normalized_output_name(name)
    if normalized.endswith((".ckpt", ".pt", ".pth", ".safetensors", ".jpg", ".jpeg", ".png")):
        return True
    metric_markers = (
        "_metrics.json",
        "_train_eval.csv",
        "_loss_curve.csv",
        "_sample_manifest.csv",
    )
    return normalized.endswith(metric_markers)


def _output_in_artifact_family(name: str, family: str, registry: dict[str, Any]) -> bool:
    families = registry.get("artifact_families") if isinstance(registry.get("artifact_families"), dict) else {}
    config = families.get(family) if isinstance(families.get(family), dict) else {}
    normalized = _normalized_output_name(name)
    exact = set(_lower_list(config.get("exact_names")))
    suffixes = tuple(_lower_list(config.get("suffixes")))
    markers = _lower_list(config.get("markers"))
    return normalized in exact or (suffixes and normalized.endswith(suffixes)) or any(marker in normalized for marker in markers)


def _normalized_output_name(name: str) -> str:
    return name.strip().strip("`").replace("\\", "/").split("/")[-1].lower()


def _lower_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip().lower() for item in value if str(item).strip()]


def _default_command_args_from_registry(default_execution: dict[str, Any], output_dir: str, plan: dict[str, Any]) -> dict[str, Any]:
    args: dict[str, Any] = {"output_dir": output_dir}
    configured_args = default_execution.get("args") if isinstance(default_execution.get("args"), dict) else {}
    args.update(configured_args)
    bindings = default_execution.get("input_bindings") if isinstance(default_execution.get("input_bindings"), dict) else {}
    for arg_name, binding in bindings.items():
        if not isinstance(binding, dict):
            continue
        suffixes = tuple(str(item).strip() for item in binding.get("read_path_suffixes") or [] if str(item).strip())
        if not suffixes:
            continue
        value = _plan_read_path(plan, suffixes)
        if value:
            args[str(arg_name)] = value
    return args


def _default_required_files_from_registry(default_execution: dict[str, Any], script_entry: dict[str, Any]) -> list[str]:
    required = default_execution.get("required_files")
    if isinstance(required, list) and required:
        return [str(item).strip() for item in required if str(item).strip()]
    expected = script_entry.get("expected_outputs") if isinstance(script_entry.get("expected_outputs"), dict) else {}
    files = expected.get("required_files") if isinstance(expected.get("required_files"), list) else []
    return [str(item).strip() for item in files if str(item).strip()]


def _safe_int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _plan_read_path(plan: dict[str, Any], suffixes: tuple[str, ...]) -> str:
    read_paths = plan.get("read_paths") if isinstance(plan.get("read_paths"), list) else []
    normalized_suffixes = tuple(item.lower() for item in suffixes)
    for raw in read_paths:
        text = str(raw or "").strip()
        if text.startswith("external:"):
            text = text.split(":", 1)[1].strip()
        lowered = text.lower()
        if text and any(lowered.endswith(suffix) for suffix in normalized_suffixes):
            return text
    return ""


def _default_remote_paths(generated_dir: Path, task: Any) -> dict[str, Any]:
    front = task.raw_front_matter or {}
    target = front.get("target_workplace") if isinstance(front.get("target_workplace"), dict) else {}
    workspace_root = str(target.get("remote_workdir") or "").strip()
    remote_root = str(target.get("remote_root") or "").strip()
    hints = _load_context_yaml(generated_dir, "execution_hints.yaml")
    output_policy = hints.get("output_policy") if isinstance(hints.get("output_policy"), dict) else {}
    default_output_dir = str(output_policy.get("default_output_dir") or "").strip()
    if not workspace_root:
        roots = output_policy.get("allowed_artifact_roots")
        if isinstance(roots, list):
            workspace_root = next((str(item).strip() for item in roots if str(item).strip()), "")
    if not workspace_root:
        workspace_root = str(Path.cwd()).replace("\\", "/")
    output_dir = _execution_output_dir(default_output_dir, remote_root, task.task_id)
    roots = output_policy.get("allowed_artifact_roots")
    artifact_roots = [workspace_root]
    if isinstance(roots, list):
        artifact_roots.extend(str(item).strip() for item in roots)
    if remote_root:
        artifact_roots.append(remote_root)
    return {
        "workspace_root": workspace_root,
        "output_dir": output_dir,
        "artifact_roots": _unique_nonempty(artifact_roots),
    }


def _execution_output_dir(default_output_dir: str, remote_root: str, task_id: str) -> str:
    cleaned = default_output_dir.replace("\\", "/").strip()
    marker = "/materializer/"
    if marker in cleaned:
        return cleaned.split(marker, 1)[0].rstrip("/") + "/artifacts"
    if cleaned:
        return cleaned.rstrip("/")
    if remote_root:
        return f"{remote_root.rstrip('/')}/{task_id}/artifacts"
    return f"runs/{task_id}/artifacts"


def _remote_generated_file_path(generated_dir: Path, local_file: Path) -> str:
    path = str(local_file).replace("\\", "/")
    if path.startswith("/") or (len(path) > 2 and path[1:3] == ":/"):
        return path
    return str((generated_dir / local_file.name).resolve()).replace("\\", "/")


def _load_context_yaml(generated_dir: Path, name: str) -> dict[str, Any]:
    path = generated_dir.parent / "context_pack" / name
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _enrich_script_registry_excerpt(registry: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(registry, dict):
        registry = {}
    try:
        from ..execution_port.script_registry import load_registry

        bundled = load_registry()
    except Exception:
        bundled = {}
    if not isinstance(bundled, dict):
        return registry

    merged: dict[str, Any] = {}
    for key, value in bundled.items():
        if key != "scripts":
            merged[key] = value
    for key, value in registry.items():
        if key != "scripts":
            merged[key] = value

    bundled_scripts = bundled.get("scripts") if isinstance(bundled.get("scripts"), dict) else {}
    excerpt_scripts = registry.get("scripts") if isinstance(registry.get("scripts"), dict) else {}
    scripts: dict[str, Any] = {}
    for script_id, bundled_entry in bundled_scripts.items():
        scripts[str(script_id)] = dict(bundled_entry) if isinstance(bundled_entry, dict) else bundled_entry
    for script_id, excerpt_entry in excerpt_scripts.items():
        key = str(script_id)
        if isinstance(scripts.get(key), dict) and isinstance(excerpt_entry, dict):
            combined = dict(scripts[key])
            combined.update(excerpt_entry)
            scripts[key] = combined
        else:
            scripts[key] = excerpt_entry
    merged["scripts"] = scripts
    return merged


def _load_generated_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _safe_execution_output_names(outputs: list[str]) -> list[str]:
    safe: list[str] = []
    for raw in outputs:
        name = _extract_output_name(raw)
        if not name or "/" in name or "\\" in name or ".." in Path(name).parts:
            continue
        if name not in safe:
            safe.append(name)
    return safe


def _extract_output_name(raw: str) -> str:
    text = str(raw).strip().strip("`")
    if not text:
        return ""
    if "`" in text:
        parts = text.split("`")
        for idx, part in enumerate(parts):
            if idx % 2 == 1 and _looks_like_output_file(part):
                return part.strip()
    for token in text.replace(",", " ").split():
        token = token.strip().strip("`.:;()[]")
        if _looks_like_output_file(token):
            return token
    return text if _looks_like_output_file(text) else ""


def _looks_like_output_file(value: str) -> bool:
    lowered = value.lower().strip()
    return lowered.endswith((".md", ".json", ".jsonl", ".yaml", ".yml", ".csv", ".txt"))


def _all_outputs_are_closure_artifacts(outputs: list[str]) -> bool:
    return bool(outputs) and all(_is_closure_artifact_name(output) for output in outputs)


def _is_closure_artifact_name(output: str) -> bool:
    name = _extract_output_name(output).lower()
    return direct_closure_policy().matches_output_name(name)


def _unique_nonempty(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in result:
            result.append(text)
    return result


def _required_outputs_from_task(task: Any) -> list[str]:
    materializer_outputs = _materializer_required_outputs_from_task(task)
    if materializer_outputs:
        return materializer_outputs
    if getattr(task, "worker", "") == "materialized_execution":
        return ["execution_plan.json", "execution_spec.yaml"]
    return _execution_required_outputs_from_task(task)


def _materializer_required_outputs_from_task(task: Any) -> list[str]:
    front = task.raw_front_matter or {}
    for key in ("materializer_required_outputs", "remote_materializer_outputs"):
        value = front.get(key)
        if isinstance(value, list):
            outputs = [str(item).strip() for item in value if str(item).strip()]
            if outputs:
                return outputs
    outputs = _section_bullets(task.body, "Required Materializer Outputs")
    if outputs:
        return outputs
    return []


def _execution_required_outputs_from_task(task: Any) -> list[str]:
    front = task.raw_front_matter or {}
    value = front.get("required_outputs")
    if isinstance(value, list):
        outputs = [str(item).strip() for item in value if str(item).strip()]
        if outputs:
            return outputs
    return _section_bullets(task.body, "Required Outputs")


def _input_files_from_task(body: str) -> list[str]:
    items: list[str] = []
    for bullet in _section_bullets(body, "Inputs") + _section_bullets(body, "Input Files"):
        text = bullet.split(":", 1)[-1].strip() if ":" in bullet else bullet
        if text and not text.startswith("script_id"):
            items.append(text)
    return items


def _section_bullets(body: str, heading: str) -> list[str]:
    target = heading.strip().lower()
    in_section = False
    saw_bullet = False
    items: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            title = line.lstrip("#").strip().lower()
            matches_target = title == target or title.startswith(f"{target} ")
            if in_section and not matches_target:
                break
            in_section = matches_target
            saw_bullet = False
            continue
        if in_section and line.startswith("- "):
            item = line[2:].strip().strip("`")
            if item:
                items.append(item)
                saw_bullet = True
        elif in_section and saw_bullet and line:
            break
    return items


def _first_body_line(body: str) -> str:
    for raw in body.splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and not line.startswith("- "):
            return line
    return ""


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _read_many(directory: Path) -> str:
    chunks: list[str] = []
    for path in sorted(directory.glob("*")):
        if path.is_file():
            chunks.append(f"## {path.name}\n\n{path.read_text(encoding='utf-8')}")
    return "\n\n".join(chunks)


def _read_context_pack(pack_dir: Path) -> str:
    from solution_agent.harness.context_budget import ContextItem, build_bounded_context

    preferred = [
        "manifest.yaml",
        "task_brief.md",
        "execution_hints.yaml",
        "workspace_context_excerpt.yaml",
        "script_registry_excerpt.yaml",
        "project_map_excerpt.yaml",
        "grounded_context.yaml",
        "grounded_context.md",
        "repo_map_excerpt.md",
        "retrieval_context.md",
        "retrieval_context.json",
        "selected_summaries.md",
        "artifact_summary.md",
        "selected_files.json",
        "context_manifest.json",
        "behavior_policy.md",
        "allowed_files.yaml",
        "expected_outputs.md",
        "relevant_code.md",
    ]
    required = {
        "manifest.yaml",
        "task_brief.md",
        "execution_hints.yaml",
        "workspace_context_excerpt.yaml",
        "script_registry_excerpt.yaml",
        "work_order.json",
        "behavior_policy.md",
        "allowed_files.yaml",
        "expected_outputs.md",
        "artifact_summary.md",
    }
    priorities = {
        "manifest.yaml": 100,
        "grounded_context.yaml": 95,
        "grounded_context.md": 90,
        "relevant_code.md": 90,
        "failing_tests.md": 85,
        "retrieval_context.json": 80,
        "retrieval_context.md": 75,
        "project_map_excerpt.yaml": 70,
        "repo_map_excerpt.md": 65,
        "selected_summaries.md": 60,
        "context_manifest.json": 40,
        "selected_files.json": 30,
        # task.md duplicates task_brief.md and is retained on disk for audit.
        "task.md": 10,
        "context_request.json": 5,
    }
    manifest_path = pack_dir / "manifest.yaml"
    max_tokens = 24000
    if manifest_path.is_file():
        try:
            manifest_payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            if isinstance(manifest_payload, dict):
                max_tokens = int((manifest_payload.get("budget") or {}).get("max_tokens") or max_tokens)
        except (OSError, TypeError, ValueError, yaml.YAMLError):
            pass

    items: list[ContextItem] = []
    for name in preferred:
        path = pack_dir / name
        if path.is_file():
            body = f"## context_pack/{path.name}\n\n{path.read_text(encoding='utf-8')}"
            items.append(
                ContextItem(
                    name=name,
                    text=body,
                    priority=priorities.get(name, 50),
                    required=name in required,
                    allow_truncate=name not in required,
                    min_tokens=64,
                )
            )

    pack = build_bounded_context(items, max_estimated_tokens=max_tokens)
    audit = {
        "schema_version": "solution-agent/model-context-selection/v1",
        "max_estimated_tokens": pack.max_estimated_tokens,
        "estimated_tokens": pack.estimated_tokens,
        "estimator": pack.estimator,
        "included": [
            {
                "name": item.name,
                "estimated_tokens": item.estimated_tokens,
                "truncated": item.truncated,
            }
            for item in pack.included
        ],
        "omitted": list(pack.omitted),
        "warning": "Preflight estimates are not provider billing usage.",
    }
    (pack_dir / "model_context_selection.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return "\n\n".join(item.text for item in pack.included)


def _extract_json_object(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        return text[start : end + 1]
    return text


def run_materializer(
    task_path: Path,
    output_root: Path,
    reader_client: ReaderClient,
    materializer_client: MaterializerClient,
    backup_materializer_client: MaterializerClient | None = None,
    reader_verifier_client: ReaderVerifierClient | None = None,
    feedback_text: str = "",
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    return MaterializerRun(
        task_path, output_root, reader_client, materializer_client,
        backup_materializer_client, reader_verifier_client,
        feedback_text, workspace_root,
    ).run()
