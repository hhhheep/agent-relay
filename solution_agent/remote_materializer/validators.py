from __future__ import annotations

import csv
import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..model_execution_evidence import (
    extract_model_execution_evidence,
    validate_model_execution_evidence,
    validate_proxy_claim_text,
)
from .context_policy import CONTEXT_LEVELS, decide_path
from .materializer_policy import direct_closure_policy, load_materializer_policy


TASK_TYPES = {"training_validation", "probe_inference", "material_regeneration", "failure_normalization"}

REQUIRED_OUTPUTS_BY_TASK_TYPE: dict[str, set[str]] = {
    "probe_inference": {
        "probe_infer_config.yaml",
        "probe_axes.csv",
        "sample_manifest.csv",
        "commands.md",
        "output_contract.yaml",
        "expected_tables.md",
    },
    "training_validation": {
        "train_config.yaml",
        "val_config.yaml",
        "data_manifest.csv",
        "commands.md",
        "output_contract.yaml",
    },
}
ALLOWED_VERIFIER_PATCH_KEYS: frozenset[str] = frozenset({
    "required_outputs",
    "task_type",
    "forbidden_actions",
    "context_request",
    "reader_meta",
})

_CONTEXT_LEVEL_RANK: dict[str, int] = {
    "materialize": 0,
    "prepare_config": 1,
    "patch_low_risk": 2,
    "unknown_or_high_risk": 3,
}

REQUIRED_FORBIDDEN = {
    "modify_source_code",
    "execute_training",
    "change_dataset_split",
    "overwrite_checkpoint",
    "delete_files",
}
DENY_SUBSTRINGS = [
    "rm ",
    "rm -rf",
    "sudo",
    "chmod 777",
    "chown",
    "scp",
    "rsync",
    "curl | bash",
    "wget | bash",
    "pip install",
    "conda install",
    "mv data",
    "mv checkpoints",
    "kill -9",
    "pkill",
    "reboot",
    "shutdown",
    "nohup",
    "tmux",
    "screen",
    "setsid",
]


@dataclass
class CheckResult:
    name: str
    status: str = "pass"
    errors: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.status = "fail"
        self.errors.append(message)


def _is_under(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _safe_relative_path(value: str, allowed_root: str) -> bool:
    if not value or "\x00" in value:
        return False
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        return False
    root = Path(allowed_root)
    return _is_under(root, root / path)


def validate_work_order_payload(payload: dict[str, Any]) -> CheckResult:
    result = CheckResult("validate_work_order")
    if payload.get("status") == "ambiguous":
        for key in ("missing_fields", "reason", "need_human_or_codex"):
            if key not in payload:
                result.fail(f"ambiguous work order missing {key}")
        return result
    required = [
        "task_id",
        "task_type",
        "source_task_path",
        "jobs",
        "required_outputs",
        "input_files",
        "allowed_output_root",
        "forbidden_actions",
        "validation_steps",
        "retry_policy",
        "escalation_policy",
    ]
    for key in required:
        if key not in payload:
            result.fail(f"missing required key: {key}")
    if payload.get("task_type") not in TASK_TYPES:
        result.fail(f"invalid task_type: {payload.get('task_type')}")
    if not payload.get("required_outputs"):
        result.fail("required_outputs must be non-empty")
    allowed_root = str(payload.get("allowed_output_root") or "")
    if not allowed_root.startswith("runs/") or not _safe_relative_path(".", allowed_root):
        result.fail(f"unsafe allowed_output_root: {allowed_root}")
    forbidden = set(payload.get("forbidden_actions") or [])
    missing_forbidden = sorted(REQUIRED_FORBIDDEN - forbidden)
    if missing_forbidden:
        result.fail(f"forbidden_actions missing: {', '.join(missing_forbidden)}")
    return result


def load_work_order(path: Path) -> tuple[dict[str, Any], CheckResult]:
    result = CheckResult("validate_work_order")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        result.fail(f"JSON parse failed: {exc}")
        return {}, result
    if not isinstance(payload, dict):
        result.fail("work_order must be a JSON object")
        return {}, result
    return payload, validate_work_order_payload(payload)


def _walk_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_walk_strings(item))
        return out
    if isinstance(value, dict):
        out = []
        for item in value.values():
            out.extend(_walk_strings(item))
        return out
    return []


def validate_yaml_file(path: Path, allowed_output_root: str, required_keys: list[str] | None = None) -> CheckResult:
    result = CheckResult(f"validate_yaml:{path.name}")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        result.fail(f"YAML parse failed: {exc}")
        return result
    if not isinstance(payload, dict):
        result.fail("YAML root must be a mapping")
        return result
    for key in required_keys or []:
        if key not in payload:
            result.fail(f"missing top-level key: {key}")
    for value in _walk_strings(payload):
        if "../" in value.replace("\\", "/") or value.startswith("/"):
            result.fail(f"unsafe path-like value: {value}")
    return result


def validate_csv_file(path: Path, required_columns: list[str], allow_empty: bool = False) -> CheckResult:
    result = CheckResult(f"validate_csv:{path.name}")
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
    except Exception as exc:
        result.fail(f"CSV parse failed: {exc}")
        return result
    columns = set(reader.fieldnames or [])
    missing = [item for item in required_columns if item not in columns]
    if missing:
        result.fail(f"missing columns: {', '.join(missing)}")
    if not rows and not allow_empty:
        result.fail("CSV must contain at least one row")
    for row in rows:
        for value in row.values():
            text = str(value or "").replace("\\", "/")
            if "../" in text or text.startswith("/"):
                result.fail(f"unsafe path value: {value}")
    return result


def _command_lines(text: str) -> list[str]:
    lines: list[str] = []
    in_fence = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if not line or line.startswith("#"):
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        if in_fence or line:
            lines.append(line)
    return lines


def _allowed_command(line: str) -> bool:
    try:
        parts = shlex.split(line, posix=True)
    except ValueError:
        return False
    if not parts:
        return True
    if parts[0] == "python":
        return True
    if parts[0] == "bash" and len(parts) >= 2:
        script = parts[1].replace("\\", "/")
        return script.startswith("scripts/run_") and script.endswith(".sh") and ".." not in Path(script).parts
    if parts[0] in {"cat", "tail", "grep", "ls", "find"}:
        return True
    return parts[:2] == ["git", "diff"]


def validate_commands_file(path: Path) -> CheckResult:
    result = CheckResult("validate_commands")
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    for line in _command_lines(text):
        lowered = line.lower()
        for denied in DENY_SUBSTRINGS:
            if denied in lowered:
                result.fail(f"denied command substring `{denied}` in: {line}")
        if line.endswith("&") or " & " in line:
            result.fail(f"background execution is forbidden: {line}")
        if not _allowed_command(line):
            result.fail(f"command is not allowlisted: {line}")
    return result


def validate_output_contract(path: Path, allowed_output_root: str) -> CheckResult:
    result = CheckResult("validate_output_contract")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        result.fail(f"output contract YAML parse failed: {exc}")
        return result
    required_files = payload.get("required_files")
    if not isinstance(required_files, list) or not required_files:
        result.fail("output_contract.yaml must define non-empty required_files")
    for item in required_files or []:
        text = str(item)
        if not _safe_relative_path(text, allowed_output_root):
            result.fail(f"required file escapes allowed root: {text}")
    return result


def validate_generated_bundle(generated_dir: Path, work_order: dict[str, Any]) -> list[CheckResult]:
    allowed_root = str(work_order.get("allowed_output_root") or ".")
    results: list[CheckResult] = []
    task_type = work_order.get("task_type")
    required_outputs = [str(item) for item in work_order.get("required_outputs") or []]
    reader_meta = work_order.get("reader_meta") if isinstance(work_order.get("reader_meta"), dict) else {}
    closure_policy = direct_closure_policy()
    patch_policy = load_materializer_policy().patch
    explicit_materializer_outputs = (
        reader_meta.get("materializer_required_outputs_source") == closure_policy.required_outputs_source
    )
    direct_closure_artifacts = reader_meta.get("materializer_output_mode") == closure_policy.mode_name
    for name in required_outputs:
        if not (generated_dir / name).exists():
            if name == patch_policy.required_patch_filename and _is_missing_patch_deferred(generated_dir):
                continue
            missing = CheckResult(f"exists:{name}", "fail", [f"missing required output: {name}"])
            results.append(missing)
    if (
        direct_closure_artifacts
        and not closure_policy.materialization_failed_allowed
        and (generated_dir / closure_policy.materialization_failed_filename).exists()
    ):
        results.append(CheckResult(
            "direct_closure_no_materialization_failed_substitute",
            "fail",
            [
                "direct_closure_artifacts must produce the requested closure files; "
                "materialization_failed.json is not a substitute",
            ],
        ))
    for path in generated_dir.glob("*.yaml"):
        if path.name == "execution_spec.yaml":
            # execution_spec.yaml is validated by execution_port after the
            # materializer phase. It legitimately contains absolute remote
            # workspace/output paths, so the generic materializer path scrubber
            # would produce false positives here.
            result = CheckResult("validate_execution_spec_artifact")
            payload: dict[str, Any] | None = None
            try:
                loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except Exception as exc:
                result.fail(f"YAML parse failed: {exc}")
            else:
                if not isinstance(loaded, dict):
                    result.fail("execution_spec.yaml must be a YAML object")
                else:
                    payload = loaded
                if isinstance(payload, dict) and not payload.get("task_id"):
                    result.fail("execution_spec.yaml missing task_id")
                if direct_closure_artifacts and isinstance(payload, dict):
                    command = payload.get("command") if isinstance(payload.get("command"), dict) else {}
                    script_id = str(command.get("script_id") or "")
                    if script_id and script_id not in closure_policy.allowed_execution_scripts:
                        result.fail(
                            "direct_closure_artifacts execution_spec.yaml may only use "
                            f"{sorted(closure_policy.allowed_execution_scripts)}, got {script_id!r}"
                        )
            results.append(result)
            continue
        required_keys = ["task_id"] if path.name.endswith("_config.yaml") else []
        results.append(validate_yaml_file(path, allowed_root, required_keys=required_keys))
    for path in generated_dir.glob("*.csv"):
        required_columns = ["path"] if path.name in {"data_manifest.csv", "sample_manifest.csv"} else []
        if path.name == "probe_axes.csv":
            required_columns = ["axis"]
        results.append(validate_csv_file(path, required_columns=required_columns))
    commands = generated_dir / "commands.md"
    if commands.exists():
        results.append(validate_commands_file(commands))
    contract = generated_dir / "output_contract.yaml"
    if contract.exists():
        results.append(validate_output_contract(contract, allowed_root))
    action_requests = generated_dir / "action_requests.json"
    if action_requests.exists():
        results.append(validate_action_requests_file(action_requests))
    decision_summary = generated_dir / "decision_summary.json"
    if decision_summary.exists():
        results.append(validate_decision_summary_file(decision_summary))
    patch_filename = patch_policy.required_patch_filename
    patch_intent = generated_dir / patch_policy.intent_filename
    if patch_intent.exists():
        results.append(validate_patch_intent_file(patch_intent))
    if patch_filename in required_outputs:
        if (generated_dir / patch_filename).exists() or not _is_missing_patch_deferred(generated_dir):
            results.append(validate_patch_syntax(generated_dir / patch_filename))
    materialized_execution_bundle = "execution_spec.yaml" in required_outputs
    skip_task_type_defaults = materialized_execution_bundle or explicit_materializer_outputs
    if task_type == "probe_inference" and not skip_task_type_defaults and not (generated_dir / "expected_tables.md").exists():
        results.append(CheckResult("expected_tables", "fail", ["probe_inference requires expected_tables.md"]))
    if not skip_task_type_defaults:
        results.append(validate_task_output_alignment(generated_dir, task_type or ""))
    return results


def validate_verifier_patches(patches: dict[str, Any], original_work_order: dict[str, Any]) -> CheckResult:
    result = CheckResult("validate_verifier_patches")
    if not isinstance(patches, dict):
        result.fail("patches must be a dict")
        return result
    for key in patches:
        if key not in ALLOWED_VERIFIER_PATCH_KEYS:
            result.fail(f"patch targets disallowed key: {key!r}")
    if "forbidden_actions" in patches:
        new_forbidden = set(patches.get("forbidden_actions") or [])
        missing = sorted(REQUIRED_FORBIDDEN - new_forbidden)
        if missing:
            result.fail(f"patch removes required forbidden_actions: {', '.join(missing)}")
    if "task_type" in patches and patches["task_type"] not in TASK_TYPES:
        result.fail(f"patch sets invalid task_type: {patches['task_type']!r}")
    if "context_request" in patches:
        original_level = str((original_work_order.get("context_request") or {}).get("context_level") or "materialize")
        new_level = str((patches.get("context_request") or {}).get("context_level") or "materialize")
        if _CONTEXT_LEVEL_RANK.get(new_level, 0) > _CONTEXT_LEVEL_RANK.get(original_level, 0):
            result.fail(f"patch escalates context_level from {original_level!r} to {new_level!r}")
    return result


def validate_action_requests_file(path: Path) -> CheckResult:
    result = CheckResult("validate_action_requests")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        result.fail(f"action_requests.json parse failed: {exc}")
        return result
    requests = _extract_action_request_items(payload)
    if requests is None:
        result.fail("action_requests.json must be a list or contain an action_requests/actions_required/requests list")
        return result
    for index, item in enumerate(requests):
        if not isinstance(item, dict):
            result.fail(f"action request #{index} must be an object")
            continue
        _validate_action_request_item(result, item, index)
    return result


def validate_decision_summary_file(path: Path) -> CheckResult:
    result = CheckResult("validate_decision_summary")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        result.fail(f"decision_summary.json parse failed: {exc}")
        return result
    if not isinstance(payload, dict):
        result.fail("decision_summary.json must be an object")
        return result
    evidence = extract_model_execution_evidence(payload)
    if evidence is None and ("requested_model" in payload or "executed_model" in payload):
        evidence = payload
    if evidence is not None:
        for error in validate_model_execution_evidence(evidence):
            result.fail(error)
        claim_fields = {
            key: payload.get(key)
            for key in ("summary", "conclusion", "claim", "claim_allowed", "claim_scope", "decision", "status")
            if key in payload
        }
        for error in validate_proxy_claim_text(evidence, claim_fields):
            result.fail(error)
    return result


def _extract_action_request_items(payload: Any) -> list[Any] | None:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("action_requests", "actions_required", "requests"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return None


def _validate_action_request_item(result: CheckResult, item: dict[str, Any], index: int) -> None:
    policy = load_materializer_policy().action_request
    aliases = {
        "action": "missing_or_needed",
        "description": "missing_or_needed",
        "rationale": "why_needed",
        "reason": "why_needed",
    }
    normalized = dict(item)
    for source, target in aliases.items():
        if target not in normalized and source in normalized:
            normalized[target] = normalized[source]
    if "blocks" not in normalized:
        if normalized.get("blocks_execution") is True:
            normalized["blocks"] = "execution"
        elif normalized.get("blocks_claim_not_execution") is True:
            normalized["blocks"] = "claim"
    missing = [field for field in policy.required_fields if field not in normalized]
    if missing:
        result.fail(f"action request #{index} missing fields: {', '.join(missing)}")
    kind = str(normalized.get("kind") or "").strip()
    if kind == "evidence_probe":
        kind = "bounded_exploratory_probe"
        normalized["kind"] = kind
    if policy.allowed_kinds and kind and kind not in policy.allowed_kinds:
        result.fail(f"action request #{index} invalid kind: {kind}")
    next_owner = str(normalized.get("next_owner") or "").strip()
    if next_owner == "materialized_execution":
        next_owner = "materializer"
        normalized["next_owner"] = next_owner
    if next_owner == "experiment_runner":
        next_owner = "scheduler"
        normalized["next_owner"] = next_owner
    if next_owner == "execution_port":
        next_owner = "scheduler"
        normalized["next_owner"] = next_owner
    if policy.allowed_next_owners and next_owner and next_owner not in policy.allowed_next_owners:
        result.fail(f"action request #{index} invalid next_owner: {next_owner}")
    blocks = str(normalized.get("blocks") or "").strip()
    if policy.allowed_blocks and blocks and blocks not in policy.allowed_blocks:
        if normalized.get("blocks_execution") is True:
            blocks = "execution"
            normalized["blocks"] = blocks
        elif normalized.get("blocks_claim_not_execution") is True:
            blocks = "claim"
            normalized["blocks"] = blocks
    if policy.allowed_blocks and blocks and blocks not in policy.allowed_blocks:
        result.fail(f"action request #{index} invalid blocks: {blocks}")
    why_needed = str(normalized.get("why_needed") or "").strip()
    if not why_needed:
        return
    lowered_why = why_needed.lower()
    if any(lowered_why == phrase for phrase in policy.vague_phrases):
        result.fail(f"action request #{index} why_needed is too vague")
    if kind == "request_approval":
        needed = str(normalized.get("missing_or_needed") or "")
        if policy.approval_flag_prefix and policy.approval_flag_prefix not in needed:
            result.fail(f"action request #{index} request_approval must name an {policy.approval_flag_prefix}* flag")
    if normalized.get("blocks_execution") is True and not blocks:
        result.fail(f"action request #{index} blocks_execution=true must declare blocks")
    if kind in {"request_artifact", "request_context"} and not str(normalized.get("missing_or_needed") or "").strip():
        result.fail(f"action request #{index} must name the missing artifact/context")


def validate_task_output_alignment(generated_dir: Path, task_type: str) -> CheckResult:
    result = CheckResult("validate_task_output_alignment")
    required = REQUIRED_OUTPUTS_BY_TASK_TYPE.get(task_type)
    if required is None:
        return result
    generated = {path.name for path in generated_dir.glob("*") if path.is_file()}
    missing = sorted(required - generated)
    if missing:
        result.fail(f"task_type={task_type} missing required outputs: {', '.join(missing)}")
    return result


def validate_context_request_payload(payload: dict[str, Any]) -> CheckResult:
    result = CheckResult("validate_context_request")
    for key in ("task_id", "context_level", "needed_files", "reason"):
        if key not in payload:
            result.fail(f"missing required key: {key}")
    context_level = str(payload.get("context_level") or "")
    if context_level not in CONTEXT_LEVELS:
        result.fail(f"invalid context_level: {context_level}")
    needed = payload.get("needed_files")
    if not isinstance(needed, list):
        result.fail("needed_files must be a list")
        return result
    if context_level == "materialize" and needed:
        result.fail("materialize context must not request source files")
    for item in needed:
        if isinstance(item, dict):
            path_str = str(item.get("path") or "").strip()
            if not path_str:
                result.fail(f"needed_files item missing path key: {item!r}")
                continue
        else:
            path_str = str(item)
        decision = decide_path(context_level, path_str)
        if not decision.allowed:
            result.fail(f"requested file rejected: {decision.path} ({decision.reason})")
    return result


def validate_context_pack(pack_dir: Path, context_request: dict[str, Any]) -> CheckResult:
    result = CheckResult("validate_context_pack")
    required = [
        "task.md",
        "work_order.json",
        "context_request.json",
        "behavior_policy.md",
        "allowed_files.yaml",
        "file_index_excerpt.md",
        "relevant_code.md",
        "recent_changes.md",
        "failing_tests.md",
        "expected_outputs.md",
        "memory.md",
        "context_manifest.json",
    ]
    for name in required:
        if not (pack_dir / name).exists():
            result.fail(f"context_pack missing {name}")
    context_level = str(context_request.get("context_level") or "")
    relevant = pack_dir / "relevant_code.md"
    if context_level == "materialize" and relevant.exists() and relevant.read_text(encoding="utf-8").strip():
        result.fail("materialize context_pack must not include source code")
    manifest = pack_dir / "context_manifest.json"
    if manifest.exists():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception as exc:
            result.fail(f"context_manifest parse failed: {exc}")
            return result
        for item in payload.get("included_files") or []:
            decision = decide_path(context_level, str(item))
            if not decision.allowed:
                result.fail(f"included file is not allowed: {item}")
    return result


DIFF_FILE_RE = re.compile(r"^(?:\+\+\+|---) [ab]/(.+)$")
GIT_DIFF_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def changed_files_from_patch(patch_text: str) -> list[str]:
    from ..tools.patch_tools import inspect_patch_files

    return inspect_patch_files(patch_text)


def validate_patch_allowed_files(patch_path: Path, context_level: str = "patch_low_risk") -> CheckResult:
    result = CheckResult("validate_patch_allowed_files")
    if not patch_path.exists():
        result.fail("patch.diff does not exist")
        return result
    patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
    changed = changed_files_from_patch(patch_text)
    if not changed:
        result.fail("patch.diff does not contain changed files")
    for path in changed:
        decision = decide_path(context_level, path)
        if not decision.allowed:
            result.fail(f"patch touches forbidden or unallowed file: {path} ({decision.reason})")
    return result


def validate_patch_syntax(patch_path: Path) -> CheckResult:
    result = CheckResult("validate_patch_syntax")
    if not patch_path.exists():
        result.fail("patch.diff does not exist")
        return result
    text = patch_path.read_text(encoding="utf-8", errors="replace")
    if _is_deferred_patch_with_closure(patch_path, text):
        return result
    from ..tools.patch_tools import check_patch_applies

    check = check_patch_applies(Path.cwd(), text)
    for error in check.errors:
        result.fail(error)
    return result


def validate_patch_intent_file(path: Path) -> CheckResult:
    result = CheckResult("validate_patch_intent")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        result.fail(f"patch_intent.json parse failed: {exc}")
        return result
    if not isinstance(payload, dict):
        result.fail("patch_intent.json must be an object")
        return result
    policy = load_materializer_policy().patch
    tier = str(payload.get("tier") or "")
    if policy.allowed_intent_tiers and tier not in policy.allowed_intent_tiers:
        result.fail(f"unsupported patch intent tier: {tier}")
    operation = str(payload.get("operation") or "")
    if policy.allowed_intent_operations and operation not in policy.allowed_intent_operations:
        result.fail(f"unsupported patch intent operation: {operation}")
    required = ["schema_version", "intent_id", "target_file", "anchor", "reason", "expected_validation", "risk"]
    for key in required:
        if key not in payload:
            result.fail(f"patch_intent missing required key: {key}")
    risk = payload.get("risk") if isinstance(payload.get("risk"), dict) else {}
    if risk.get("source_code_behavior_change") is True or risk.get("multi_file") is True or risk.get("requires_codex") is True:
        result.fail("patch_intent requests source/multifile/Codex work; OpenCoder may only propose")
    anchor = payload.get("anchor") if isinstance(payload.get("anchor"), dict) else {}
    if not str(anchor.get("text") or "").strip():
        result.fail("patch_intent anchor.text must be non-empty")
    return result


def _is_deferred_patch_with_closure(patch_path: Path, text: str) -> bool:
    policy = load_materializer_policy().patch
    if not policy.allow_deferred_with_closure:
        return False
    if not any(marker in text for marker in policy.deferred_markers):
        return False
    base = patch_path.parent
    for name in policy.deferred_required_closure_files:
        path = base / name
        if not path.exists() or not path.read_text(encoding="utf-8", errors="replace").strip():
            return False
    return True


def _is_missing_patch_deferred(generated_dir: Path) -> bool:
    policy = load_materializer_policy().patch
    if not policy.allow_deferred_with_closure:
        return False
    for name in policy.deferred_required_closure_files:
        path = generated_dir / name
        if not path.exists() or not path.read_text(encoding="utf-8", errors="replace").strip():
            return False
    return True


def summarize(results: list[CheckResult]) -> dict[str, Any]:
    failed = [item for item in results if item.status != "pass"]
    return {
        "status": "fail" if failed else "pass",
        "validators": [item.__dict__ for item in results],
        "failed_validators": [item.name for item in failed],
    }
