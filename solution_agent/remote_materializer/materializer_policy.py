from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


POLICY_PATH = Path(__file__).with_name("materializer_policy.yaml")


@dataclass(frozen=True)
class DirectClosurePolicy:
    mode_name: str
    required_outputs_source: str
    exact_names: frozenset[str]
    suffixes: tuple[str, ...]
    allowed_execution_scripts: frozenset[str]
    materialization_failed_filename: str
    materialization_failed_allowed: bool
    reader_meta: dict[str, str]

    def matches_output_name(self, output_name: str) -> bool:
        name = output_name.strip().lower()
        if name in self.exact_names:
            return True
        return name.endswith(self.suffixes)


@dataclass(frozen=True)
class PatchPolicy:
    required_patch_filename: str
    intent_filename: str
    quality_report_filename: str
    allowed_intent_tiers: frozenset[str]
    allowed_intent_operations: frozenset[str]
    require_real_unified_diff: bool
    reject_placeholder_hunks: bool
    allow_deferred_with_closure: bool
    deferred_markers: tuple[str, ...]
    deferred_required_closure_files: tuple[str, ...]


@dataclass(frozen=True)
class ActionRequestPolicy:
    required_fields: tuple[str, ...]
    allowed_kinds: frozenset[str]
    allowed_next_owners: frozenset[str]
    allowed_blocks: frozenset[str]
    vague_phrases: tuple[str, ...]
    approval_flag_prefix: str


@dataclass(frozen=True)
class MaterializerPolicy:
    direct_closure: DirectClosurePolicy
    patch: PatchPolicy
    action_request: ActionRequestPolicy


def _load_raw_policy(path: Path = POLICY_PATH) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        loaded = {}
    return loaded if isinstance(loaded, dict) else {}


@lru_cache(maxsize=1)
def load_materializer_policy() -> MaterializerPolicy:
    raw = _load_raw_policy()
    closure = raw.get("direct_closure_artifacts") if isinstance(raw.get("direct_closure_artifacts"), dict) else {}
    substitute = (
        closure.get("materialization_failed_substitute")
        if isinstance(closure.get("materialization_failed_substitute"), dict)
        else {}
    )
    reader_meta = closure.get("reader_meta") if isinstance(closure.get("reader_meta"), dict) else {}
    patch = raw.get("patch_policy") if isinstance(raw.get("patch_policy"), dict) else {}
    action_request = raw.get("action_request_policy") if isinstance(raw.get("action_request_policy"), dict) else {}
    return MaterializerPolicy(
        direct_closure=DirectClosurePolicy(
            mode_name=str(closure.get("mode_name") or "direct_closure_artifacts"),
            required_outputs_source=str(closure.get("required_outputs_source") or "task_explicit_materializer_outputs"),
            exact_names=frozenset(str(item).lower() for item in closure.get("exact_names") or []),
            suffixes=tuple(str(item).lower() for item in closure.get("suffixes") or []),
            allowed_execution_scripts=frozenset(str(item) for item in closure.get("allowed_execution_scripts") or []),
            materialization_failed_filename=str(substitute.get("filename") or "materialization_failed.json"),
            materialization_failed_allowed=bool(substitute.get("allowed", False)),
            reader_meta={str(key): str(value) for key, value in reader_meta.items()},
        ),
        patch=PatchPolicy(
            required_patch_filename=str(patch.get("required_patch_filename") or "patch.diff"),
            intent_filename=str(patch.get("intent_filename") or "patch_intent.json"),
            quality_report_filename=str(patch.get("quality_report_filename") or "patch_quality_report.json"),
            allowed_intent_tiers=frozenset(str(item) for item in patch.get("allowed_intent_tiers") or []),
            allowed_intent_operations=frozenset(str(item) for item in patch.get("allowed_intent_operations") or []),
            require_real_unified_diff=bool(patch.get("require_real_unified_diff", True)),
            reject_placeholder_hunks=bool(patch.get("reject_placeholder_hunks", True)),
            allow_deferred_with_closure=bool(patch.get("allow_deferred_with_closure", False)),
            deferred_markers=tuple(str(item) for item in patch.get("deferred_markers") or []),
            deferred_required_closure_files=tuple(str(item) for item in patch.get("deferred_required_closure_files") or []),
        ),
        action_request=ActionRequestPolicy(
            required_fields=tuple(str(item) for item in action_request.get("required_fields") or []),
            allowed_kinds=frozenset(str(item) for item in action_request.get("allowed_kinds") or []),
            allowed_next_owners=frozenset(str(item) for item in action_request.get("allowed_next_owners") or []),
            allowed_blocks=frozenset(str(item) for item in action_request.get("allowed_blocks") or []),
            vague_phrases=tuple(str(item).lower() for item in action_request.get("vague_phrases") or []),
            approval_flag_prefix=str(action_request.get("approval_flag_prefix") or "allow_"),
        ),
    )


def direct_closure_policy() -> DirectClosurePolicy:
    return load_materializer_policy().direct_closure
