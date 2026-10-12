from __future__ import annotations

import hashlib
import json
import socket
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List

from solution_agent.context_index import query_context_index

SCHEMA_VERSION = 1


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_hex(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _read_markdown_docs(docs_dir: Path) -> List[Dict[str, str]]:
    docs: List[Dict[str, str]] = []
    for path in sorted(docs_dir.rglob("*.md")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        docs.append(
            {
                "name": path.name,
                "relative_path": str(path.relative_to(docs_dir)).replace("\\", "/"),
                "text": text,
            }
        )
    return docs


def _has_any(text: str, markers: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in markers)


def _doc_ref(name: str, *, authority: str) -> Dict[str, str]:
    return {"ref": name, "authority": authority}


def _authority_for_doc(name: str) -> str:
    lowered = name.lower()
    if "chronology" in lowered or "current_compressed_kb" in lowered or lowered == "readme.md":
        return "current_kb_or_chronology"
    if "task" in lowered or "task_book" in lowered:
        return "historical_taskbook"
    if "report" in lowered or "summary" in lowered or "status" in lowered or "l66" in lowered or "self_check" in lowered:
        return "experiment_report"
    if "handoff" in lowered:
        return "handoff_mixed_authority"
    return "project_doc"


def _build_evidence_refs(docs: List[Dict[str, str]]) -> List[Dict[str, str]]:
    refs: List[Dict[str, str]] = []
    for doc in docs:
        name = doc["name"]
        if name.lower() in {"readme.md"} or _has_any(
            name,
            ["chronology", "current_compressed_kb", "integration_kb", "l66", "self_check", "v2h2c"],
        ):
            refs.append(_doc_ref(name, authority=_authority_for_doc(name)))
    return refs[:12]


def _infer_project_id(docs_dir: Path, docs: List[Dict[str, str]], fallback: str | None) -> str:
    if fallback:
        return fallback
    joined = "\n".join(doc["text"][:400] for doc in docs)
    if "B14" in joined or "Spatial-GRACE" in joined or "DDL-X" in joined:
        return "B14_spatial_grace_graph_utility"
    return docs_dir.name or "unknown_project"


def _infer_mainline(joined: str) -> str:
    if _has_any(joined, ["mask/evidence -> calibrated bbox -> gated P2 fallback", "calibrated bbox", "gated P2 fallback"]):
        return "mask/evidence -> calibrated bbox -> gated P2 fallback"
    if _has_any(joined, ["same-image graph", "residual evidence"]):
        return "same-image graph / residual evidence -> bounded next-step validation"
    return "Unknown; no current mainline marker found in docs."


def _infer_state_summary(joined: str, mainline: str) -> str:
    if "decoder p0" in joined.lower() or "decode_mask_to_bbox" in joined:
        return "Project is in a bounded decoder/interface validation stage, not a final promotion stage."
    if "L6.3-V1" in joined and "completed" in joined.lower():
        return "Project has moved past the initial proposal-audit stage and is resolving current bbox calibration."
    if mainline.startswith("Unknown"):
        return "Project state could not be determined from available docs."
    return f"Project is following the current mainline: {mainline}."


def _infer_superseded(joined: str, docs: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    if "Run L6.3-V1 first" in joined and _has_any(joined, ["V1 audit was completed", "V1 proposal result", "L6.3-V1 completed"]):
        output.append(
            {
                "item": "Run L6.3-V1 first",
                "status": "superseded",
                "superseded_by": ["later V1/V2/L64/L66 evidence"],
                "current_handling": "historical_context_only",
            }
        )
    if _has_any(joined, ["V2/V2b/V2c", "local refiner"]) and _has_any(joined, ["Do not scale", "not a working local refiner", "real-negative"]):
        output.append(
            {
                "item": "V2/V2b/V2c local refiner promotion",
                "status": "failed_or_rejected_path",
                "reason": "real-negative detail leakage / not working local refiner",
            }
        )
    if _has_any(joined, ["P2-only"]) and _has_any(joined, ["not final", "Do not use P2-only", "proposal baseline"]):
        output.append(
            {
                "item": "P2-only final bbox method",
                "status": "rejected_as_final_method",
                "current_handling": "proposal_baseline_or_gated_fallback_only",
            }
        )
    return output


def _infer_bottlenecks(joined: str) -> List[Dict[str, Any]]:
    bottlenecks: List[Dict[str, Any]] = []
    if _has_any(joined, ["real-negative", "false-detail", "false detail"]):
        bottlenecks.append(
            {
                "issue": "real-negative false-detail evidence",
                "why_blocking": "Current candidates can improve recall while producing too many real boxes or leaking real-negative detail.",
                "supported_by": ["V2/V2h2c diagnostics", "L66 calibration/self-check"],
            }
        )
    if _has_any(joined, ["tiny", "small"]) and _has_any(joined, ["not solved", "insufficient", "not final"]):
        bottlenecks.append(
            {
                "issue": "tiny/small bbox quality is not solved",
                "why_blocking": "Tiny/small proposal coverage does not by itself establish final localization quality.",
                "supported_by": ["tiny branch KB", "proposal queue audit/status reports"],
            }
        )
    if not bottlenecks:
        bottlenecks.append(
            {
                "issue": "No explicit bottleneck extracted",
                "why_blocking": "The docs need structured open_questions or claim_limits for reliable bottleneck detection.",
                "supported_by": [],
            }
        )
    return bottlenecks


def _infer_claim_limits(joined: str) -> List[Dict[str, str]]:
    limits: List[Dict[str, str]] = []
    if _has_any(joined, ["P2-only", "P2 only"]):
        limits.append({"limit": "P2-only is a proposal baseline / gated fallback candidate, not the final bbox method."})
    if _has_any(joined, ["decoder p0", "decode_mask_to_bbox", "smoke"]):
        limits.append({"limit": "Decoder P0 or smoke success is interface/pathway evidence only, not formal bbox quality."})
    if _has_any(joined, ["tiny", "not solved", "top1 localization"]):
        limits.append({"limit": "Do not claim tiny localization is solved."})
    if _has_any(joined, ["same-image reference", "g_src", "source classifier"]):
        limits.append({"limit": "Do not harden graph/source evidence into a universal source classifier."})
    return limits


def _infer_next_safe_action(joined: str, mainline: str) -> Dict[str, str]:
    if _has_any(joined, ["decoder p0", "decode_mask_to_bbox", "bounded decoder"]):
        return {
            "action": "Run bounded decoder adapter/audit against existing calibration artifacts.",
            "why_safe": "This validates the current interface using existing artifacts and should stop on missing fields or weak evidence.",
        }
    if "calibrated bbox" in mainline:
        return {
            "action": "Build a bounded mask/evidence-to-bbox calibration diagnostic.",
            "why_safe": "It advances the current mainline without launching full training or promoting P2-only output.",
        }
    return {
        "action": "Build a structured current-state and bottleneck audit before proposing execution.",
        "why_safe": "The project state is not yet sufficiently grounded for execution.",
    }


def _safe_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            item = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _artifact_tokens(entry: Dict[str, Any]) -> set[str]:
    text = json.dumps(
        {
            "task_id": entry.get("task_id"),
            "run_id": entry.get("run_id"),
            "kind": entry.get("kind"),
            "status": entry.get("status"),
            "worker": entry.get("worker"),
            "script_id": entry.get("script_id"),
            "summary": entry.get("summary"),
            "claim_scope": entry.get("claim_scope"),
            "decision": entry.get("decision"),
            "produced_artifacts": entry.get("produced_artifacts"),
            "important_paths": entry.get("important_paths"),
            "tags": entry.get("tags"),
        },
        ensure_ascii=False,
        sort_keys=True,
    ).lower()
    return {token for token in text.replace("/", " ").replace("_", " ").replace("-", " ").split() if token}


def _select_artifact_entries(rows: List[Dict[str, Any]], query: str, limit: int) -> List[Dict[str, Any]]:
    query_tokens = {token.lower() for token in str(query or "").replace("/", " ").replace("_", " ").replace("-", " ").split() if token.strip()}
    scored: List[tuple[int, int, Dict[str, Any]]] = []
    for index, row in enumerate(rows):
        tokens = _artifact_tokens(row)
        score = len(query_tokens & tokens) if query_tokens else 0
        if score > 0 or row.get("kind") in {"collected_run", "workplace_snapshot"}:
            scored.append((score, index, row))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [row for _, _, row in scored[: max(1, int(limit))]]


def build_project_knowledge_from_docs_dry_run(
    docs_dir: Path | str,
    *,
    project_id: str | None = None,
    workplace_profile: str | None = None,
    knowledge_root_ref: str | None = None,
) -> Dict[str, Any]:
    """Build a project_knowledge bundle from local markdown docs.

    v0 is deterministic and local-only. It does not SSH, call APIs, dispatch
    work, mutate registries, or edit canonical knowledge.
    """

    docs_path = Path(docs_dir)
    docs = _read_markdown_docs(docs_path)
    joined = "\n\n".join(doc["text"] for doc in docs)
    resolved_project_id = _infer_project_id(docs_path, docs, project_id)
    current_mainline = _infer_mainline(joined)
    state_summary = _infer_state_summary(joined, current_mainline)
    superseded = _infer_superseded(joined, docs)
    bottlenecks = _infer_bottlenecks(joined)
    claim_limits = _infer_claim_limits(joined)
    next_safe_action = _infer_next_safe_action(joined, current_mainline)
    evidence_refs = _build_evidence_refs(docs)
    doc_index = [
        {
            "name": doc["name"],
            "relative_path": doc["relative_path"],
            "authority": _authority_for_doc(doc["name"]),
            "hash": _sha256_hex(doc["text"])[:16],
        }
        for doc in docs
    ]
    bundle = {
        "schema_version": SCHEMA_VERSION,
        "project_id": resolved_project_id,
        "project_knowledge_binding": {
            "kind": "local_project_knowledge_bundle" if not workplace_profile else "remote_project_kb_binding",
            "project_id": resolved_project_id,
            "workplace_profile": _normalize(workplace_profile),
            "knowledge_root_ref": _normalize(knowledge_root_ref or str(docs_path)),
            "visibility": "local_only",
        },
        "current_state": {
            "summary": state_summary,
            "mainline": current_mainline,
            "stage": "bounded_current_state_inferred_from_docs",
        },
        "active_workstreams": [
            {
                "name": "current-mainline bounded validation",
                "status": "next_safe",
                "mainline": current_mainline,
            }
        ],
        "completed_or_superseded": superseded,
        "superseded_tasks": superseded,
        "bottlenecks": bottlenecks,
        "claim_limits": claim_limits,
        "next_safe_action": next_safe_action,
        "evidence_refs": evidence_refs,
        "kb_patches_proposed": [
            {"patch": "chronology/current_state/claim_limits review recommended"} if evidence_refs else {"patch": "needs_evidence_refs"}
        ],
        "document_index": doc_index,
        "ingestion": {
            "mode": "local_markdown_docs_dry_run",
            "doc_count": len(docs),
            "side_effects": {
                "ssh": False,
                "network": False,
                "scheduler_dispatch": False,
                "registry_mutation": False,
                "remote_execution": False,
                "canonical_kb_write": False,
            },
        },
        "idempotency_key": f"project_knowledge_{_sha256_hex({'docs': doc_index, 'project_id': resolved_project_id})[:24]}",
    }
    return bundle


def build_project_knowledge_from_context_index_dry_run(
    repo_root: Path | str,
    query: str,
    *,
    project_id: str | None = None,
    workplace_profile: str | None = None,
    knowledge_root_ref: str | None = None,
    limit: int = 8,
) -> Dict[str, Any]:
    """Build a project_knowledge bundle from the local SQLite context index.

    This is the index-first path for Project Dialogue. It does not scan a docs
    directory; it only queries the existing local retrieval index and applies
    the same deterministic current-state extraction used by docs ingestion.
    """

    root = Path(repo_root)
    result = query_context_index(root, query, limit=limit)
    docs: List[Dict[str, str]] = []
    for item in result.get("results") or []:
        if not isinstance(item, dict):
            continue
        docs.append(
            {
                "name": str(item.get("title") or item.get("doc_id") or "context_result"),
                "relative_path": str((item.get("paths") or ["context_index"])[0]),
                "text": json.dumps(
                    {
                        "doc_id": item.get("doc_id"),
                        "source_type": item.get("source_type"),
                        "title": item.get("title"),
                        "text": item.get("text"),
                        "tags": item.get("tags"),
                        "paths": item.get("paths"),
                        "trust_level": item.get("trust_level"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            }
        )
    joined = "\n\n".join(doc["text"] for doc in docs)
    resolved_project_id = project_id or ("B14_spatial_grace_graph_utility" if "B14" in joined or "Spatial-GRACE" in joined else "context_index_project")
    current_mainline = _infer_mainline(joined)
    state_summary = _infer_state_summary(joined, current_mainline)
    superseded = _infer_superseded(joined, docs)
    bottlenecks = _infer_bottlenecks(joined)
    claim_limits = _infer_claim_limits(joined)
    next_safe_action = _infer_next_safe_action(joined, current_mainline)
    evidence_refs = [
        {
            "ref": str(item.get("doc_id") or item.get("title") or "context_result"),
            "authority": str(item.get("trust_level") or item.get("source_type") or "context_index"),
        }
        for item in (result.get("results") or [])[:12]
        if isinstance(item, dict)
    ]
    bundle = {
        "schema_version": SCHEMA_VERSION,
        "project_id": resolved_project_id,
        "project_knowledge_binding": {
            "kind": "context_index_project_knowledge_bundle" if not workplace_profile else "remote_project_kb_binding",
            "project_id": resolved_project_id,
            "workplace_profile": _normalize(workplace_profile),
            "knowledge_root_ref": _normalize(knowledge_root_ref or str(result.get("path") or root)),
            "visibility": "local_only",
        },
        "current_state": {
            "summary": state_summary,
            "mainline": current_mainline,
            "stage": "bounded_current_state_inferred_from_context_index",
        },
        "active_workstreams": [
            {
                "name": "current-mainline bounded validation",
                "status": "next_safe",
                "mainline": current_mainline,
            }
        ],
        "completed_or_superseded": superseded,
        "superseded_tasks": superseded,
        "bottlenecks": bottlenecks,
        "claim_limits": claim_limits,
        "next_safe_action": next_safe_action,
        "evidence_refs": evidence_refs,
        "kb_patches_proposed": [
            {"patch": "context-index-backed chronology/current_state/claim_limits review recommended"}
            if evidence_refs
            else {"patch": "needs_context_index_evidence_refs"}
        ],
        "document_index": [
            {
                "name": doc["name"],
                "relative_path": doc["relative_path"],
                "authority": "context_index_result",
                "hash": _sha256_hex(doc["text"])[:16],
            }
            for doc in docs
        ],
        "ingestion": {
            "mode": "context_index_dry_run",
            "query": query,
            "context_index_ok": bool(result.get("ok")),
            "context_index_mode": result.get("mode"),
            "doc_count": len(docs),
            "side_effects": {
                "docs_dir_scan": False,
                "ssh": False,
                "network": False,
                "scheduler_dispatch": False,
                "registry_mutation": False,
                "remote_execution": False,
                "canonical_kb_write": False,
            },
        },
        "idempotency_key": f"project_knowledge_index_{_sha256_hex({'query': query, 'docs': [doc['relative_path'] for doc in docs], 'project_id': resolved_project_id})[:24]}",
    }
    return bundle


def build_project_knowledge_from_artifact_index_dry_run(
    repo_root: Path | str,
    query: str,
    *,
    project_id: str | None = None,
    workplace_profile: str | None = None,
    knowledge_root_ref: str | None = None,
    limit: int = 8,
) -> Dict[str, Any]:
    """Build a project_knowledge bundle from the local artifact index only.

    This avoids docs rescans and context-index queries. It reads the existing
    `.solution/context/artifact_index.jsonl` file, selects bounded matching
    entries, and derives the same conservative project state fields used by the
    other project knowledge adapters.
    """

    root = Path(repo_root)
    artifact_index_path = root / ".solution" / "context" / "artifact_index.jsonl"
    rows = _safe_jsonl(artifact_index_path)
    selected = _select_artifact_entries(rows, query, limit)
    docs: List[Dict[str, str]] = []
    for item in selected:
        task_or_run = str(item.get("task_id") or item.get("run_id") or "artifact_index_entry")
        docs.append(
            {
                "name": task_or_run,
                "relative_path": str(item.get("result_path") or artifact_index_path.as_posix()),
                "text": json.dumps(
                    {
                        "task_id": item.get("task_id"),
                        "run_id": item.get("run_id"),
                        "kind": item.get("kind"),
                        "status": item.get("status"),
                        "worker": item.get("worker"),
                        "script_id": item.get("script_id"),
                        "summary": item.get("summary"),
                        "claim_scope": item.get("claim_scope"),
                        "decision": item.get("decision"),
                        "produced_artifacts": item.get("produced_artifacts"),
                        "important_paths": item.get("important_paths"),
                        "tags": item.get("tags"),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            }
        )
    joined = "\n\n".join(doc["text"] for doc in docs)
    resolved_project_id = project_id or ("B14_spatial_grace_graph_utility" if "B14" in joined or "Spatial-GRACE" in joined else "artifact_index_project")
    current_mainline = _infer_mainline(joined)
    state_summary = _infer_state_summary(joined, current_mainline)
    superseded = _infer_superseded(joined, docs)
    bottlenecks = _infer_bottlenecks(joined)
    claim_limits = _infer_claim_limits(joined)
    next_safe_action = _infer_next_safe_action(joined, current_mainline)
    evidence_refs = [
        {
            "ref": str(item.get("task_id") or item.get("run_id") or "artifact_index_entry"),
            "authority": "artifact_index",
        }
        for item in selected[:12]
    ]
    bundle = {
        "schema_version": SCHEMA_VERSION,
        "project_id": resolved_project_id,
        "project_knowledge_binding": {
            "kind": "artifact_index_project_knowledge_bundle" if not workplace_profile else "remote_project_kb_binding",
            "project_id": resolved_project_id,
            "workplace_profile": _normalize(workplace_profile),
            "knowledge_root_ref": _normalize(knowledge_root_ref or str(artifact_index_path)),
            "visibility": "local_only",
        },
        "current_state": {
            "summary": state_summary,
            "mainline": current_mainline,
            "stage": "bounded_current_state_inferred_from_artifact_index",
        },
        "active_workstreams": [
            {
                "name": "current-mainline bounded validation",
                "status": "next_safe",
                "mainline": current_mainline,
            }
        ],
        "completed_or_superseded": superseded,
        "superseded_tasks": superseded,
        "bottlenecks": bottlenecks,
        "claim_limits": claim_limits,
        "next_safe_action": next_safe_action,
        "evidence_refs": evidence_refs,
        "kb_patches_proposed": [
            {"patch": "artifact-index-backed chronology/current_state/claim_limits review recommended"}
            if evidence_refs
            else {"patch": "needs_artifact_index_evidence_refs"}
        ],
        "document_index": [
            {
                "name": doc["name"],
                "relative_path": doc["relative_path"],
                "authority": "artifact_index_result",
                "hash": _sha256_hex(doc["text"])[:16],
            }
            for doc in docs
        ],
        "ingestion": {
            "mode": "artifact_index_dry_run",
            "query": query,
            "artifact_index_path": str(artifact_index_path),
            "artifact_index_ok": artifact_index_path.exists(),
            "doc_count": len(docs),
            "side_effects": {
                "docs_dir_scan": False,
                "context_index_query": False,
                "ssh": False,
                "network": False,
                "scheduler_dispatch": False,
                "registry_mutation": False,
                "remote_execution": False,
                "canonical_kb_write": False,
            },
        },
        "idempotency_key": f"project_knowledge_artifact_index_{_sha256_hex({'query': query, 'docs': [doc['relative_path'] for doc in docs], 'project_id': resolved_project_id})[:24]}",
    }
    return bundle


def write_project_knowledge_bundle_dry_run(bundle: Dict[str, Any], output_path: Path | str) -> Dict[str, Any]:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if target.exists():
        existing = target.read_text(encoding="utf-8")
        if existing == payload:
            return {"status": "ready", "created": False, "replayed": True, "path": str(target)}
        return {"status": "blocked", "created": False, "replayed": False, "path": str(target), "reasons": ["idempotency_collision"]}
    target.write_text(payload, encoding="utf-8")
    return {"status": "ready", "created": True, "replayed": False, "path": str(target)}


__all__ = [
    "build_project_knowledge_from_artifact_index_dry_run",
    "build_project_knowledge_from_context_index_dry_run",
    "build_project_knowledge_from_docs_dry_run",
    "write_project_knowledge_bundle_dry_run",
]
