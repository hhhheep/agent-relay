from __future__ import annotations

import ast
import csv
import io
import json
from pathlib import Path
from typing import Any

import yaml


def expand_contract_artifact_request(generated_dir: Path, required: set[str]) -> None:
    request_path = generated_dir / "contract_artifact_request.yaml"
    if not request_path.exists():
        return
    try:
        request = yaml.safe_load(request_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return
    outputs = request.get("outputs")
    if not isinstance(outputs, list):
        return
    for output in outputs:
        if not isinstance(output, dict):
            continue
        name = safe_contract_output_name(str(output.get("path") or ""))
        if not name or name not in required:
            continue
        target = generated_dir / name
        if target.exists():
            continue
        target.write_text(render_contract_artifact_output(output), encoding="utf-8")


def repair_csv_artifact_tables(generated_dir: Path, required: set[str]) -> None:
    for name in required:
        if not str(name).endswith(".csv"):
            continue
        path = generated_dir / name
        if not path.exists():
            continue
        repaired = _repair_csv_text(path.read_text(encoding="utf-8", errors="replace"))
        if repaired is not None:
            path.write_text(repaired, encoding="utf-8")


def _repair_csv_text(text: str) -> str | None:
    stripped = text.strip()
    if not stripped or not stripped.startswith(("{", "[")):
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        try:
            payload = ast.literal_eval(stripped)
        except (SyntaxError, ValueError):
            return None
    if isinstance(payload, list):
        if len(payload) == 1 and isinstance(payload[0], dict):
            payload = payload[0]
        elif len(payload) == 2 and all(isinstance(item, dict) for item in payload):
            first, second = payload
            if any(key in first for key in ("headers", "header", "column_names")) and "rows" in second:
                payload = {**first, "rows": second.get("rows")}
    if not isinstance(payload, dict):
        return None
    headers = payload.get("headers", payload.get("header", payload.get("column_names")))
    rows = payload.get("rows")
    if not isinstance(headers, list) or not all(str(item).strip() for item in headers):
        return None
    fieldnames = [str(item).strip() for item in headers]
    normalized_rows = _normalize_csv_rows(rows, fieldnames)
    if normalized_rows is None:
        return None
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(normalized_rows)
    return output.getvalue()


def _normalize_csv_rows(rows: Any, fieldnames: list[str]) -> list[dict[str, Any]] | None:
    if not isinstance(rows, list):
        return None
    if not rows:
        return []
    if all(isinstance(row, dict) for row in rows):
        return [{name: row.get(name, "") for name in fieldnames} for row in rows]
    if all(not isinstance(row, (dict, list, tuple)) for row in rows):
        if len(rows) % len(fieldnames) != 0:
            return None
        normalized = []
        for index in range(0, len(rows), len(fieldnames)):
            values = rows[index:index + len(fieldnames)]
            normalized.append(dict(zip(fieldnames, values)))
        return normalized
    if all(isinstance(row, (list, tuple)) for row in rows):
        normalized = []
        for row in rows:
            values = list(row)
            if len(values) != len(fieldnames):
                return None
            normalized.append(dict(zip(fieldnames, values)))
        return normalized
    return None


def contract_artifact_request_from_task(task: Any, outputs: list[str]) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "outputs": [contract_artifact_output(task, name) for name in outputs],
    }


def contract_artifact_request_is_valid(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return False
    return isinstance(payload, dict) and isinstance(payload.get("outputs"), list)


def safe_contract_output_name(name: str) -> str:
    cleaned = name.strip().replace("\\", "/")
    if not cleaned or cleaned.startswith("/") or "/" in cleaned or ".." in Path(cleaned).parts:
        return ""
    return cleaned


def render_contract_artifact_output(output: dict[str, Any]) -> str:
    kind = str(output.get("kind") or "").strip().lower()
    content = output.get("content")
    if kind == "json" or str(output.get("path") or "").endswith(".json"):
        return json.dumps(content if content is not None else {}, ensure_ascii=False, indent=2) + "\n"
    if kind in {"yaml", "yml"} or str(output.get("path") or "").endswith((".yaml", ".yml")):
        return yaml.safe_dump(content if content is not None else {}, sort_keys=False, allow_unicode=True)
    if kind == "csv" or str(output.get("path") or "").endswith(".csv"):
        return str(content or "")
    return render_markdown_contract_output(output)


def render_markdown_contract_output(output: dict[str, Any]) -> str:
    title = str(output.get("title") or Path(str(output.get("path") or "artifact.md")).stem.replace("_", " ").title())
    lines = [f"# {title}", ""]
    sections = output.get("sections")
    if isinstance(sections, list):
        for section in sections:
            if not isinstance(section, dict):
                continue
            heading = str(section.get("heading") or "").strip()
            if heading:
                lines.extend([f"## {heading}", ""])
            body = section.get("body")
            if body:
                lines.extend([str(body), ""])
            bullets = section.get("bullets")
            if isinstance(bullets, list):
                lines.extend(f"- {item}" for item in bullets)
                lines.append("")
    elif content := output.get("content"):
        lines.extend([str(content), ""])
    return "\n".join(lines).rstrip() + "\n"


def contract_artifact_output(task: Any, name: str) -> dict[str, Any]:
    suffix = Path(name).suffix.lower()
    if suffix == ".json":
        return {
            "path": name,
            "kind": "json",
            "content": {
                "task_id": task.task_id,
                "status": "pending_evidence_review",
                "normalizer_note": "Generated by bounded contract_artifact_writer fallback.",
                "claim_policy": "do not claim success beyond available evidence",
                "action_requests": [],
            },
        }
    if suffix == ".csv":
        return {
            "path": name,
            "kind": "csv",
            "content": "item,status,notes\nnormalizer_fallback,pending_review,bounded artifact placeholder generated for downstream review\n",
        }
    if suffix in {".yaml", ".yml"}:
        return {
            "path": name,
            "kind": "yaml",
            "content": {
                "task_id": task.task_id,
                "status": "pending_evidence_review",
                "normalizer_note": "Generated by bounded contract_artifact_writer fallback.",
            },
        }
    return {
        "path": name,
        "kind": "markdown",
        "title": Path(name).stem.replace("_", " ").title(),
        "sections": [
            {
                "heading": "Summary",
                "body": (
                    "This artifact was produced by the normalizer's bounded contract writer fallback "
                    "because the task book expressed the desired observation but did not include an explicit execution block."
                ),
            },
            {
                "heading": "Claim Policy",
                "bullets": [
                    "Missing evidence limits claims, not bounded execution.",
                    "Treat this artifact as an observation aid until downstream evidence is reviewed.",
                ],
            },
        ],
    }
