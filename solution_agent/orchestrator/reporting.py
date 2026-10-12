from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List

from .schema import TaskBook, WorkerResult


def _readable_item(item: Any) -> str:
    if isinstance(item, dict):
        return "; ".join(f"{key}: {value}" for key, value in item.items())
    return str(item)


def _bullets(items: Iterable[Any]) -> str:
    values = [_readable_item(item) for item in items if item]
    if not values:
        return "- None recorded"
    return "\n".join(f"- {item}" for item in values)


def _metrics_summary(metrics_path: Path) -> List[str]:
    if not metrics_path.exists():
        return []
    try:
        payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return [f"metrics.json exists but is not valid JSON: {metrics_path}"]
    if isinstance(payload, dict):
        return [f"{key}: {value}" for key, value in payload.items()]
    return [str(payload)]


def _load_metrics(metrics_path: Path) -> Any:
    if not metrics_path.exists():
        return None
    try:
        return json.loads(metrics_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _markdown_table(headers: List[str], rows: List[List[Any]]) -> str:
    if not rows:
        return "- None recorded"
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(_fmt(value).replace("|", "/") for value in row) + " |")
    return "\n".join(lines)


def _planned_variants(task: TaskBook) -> List[str]:
    values: List[str] = []
    for match in re.findall(r"`([A-Za-z0-9]+(?:_[A-Za-z0-9]+){2,})`", task.body):
        if match not in values and any(token in match for token in ("HF", "R8", "Delta", "S4", "P2")):
            values.append(match)
    return values[:12]


def _section(text: str, heading: str, limit: int = 1600) -> str:
    pattern = re.compile(rf"^## {re.escape(heading)}\s*$", flags=re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return ""
    start = match.end()
    next_match = re.search(r"^##\s+", text[start:], flags=re.MULTILINE)
    end = start + next_match.start() if next_match else len(text)
    content = text[start:end].strip()
    return content[:limit] + ("\n...[truncated]" if len(content) > limit else "")


def _completion_rows(task: TaskBook, result: WorkerResult, metrics: Any) -> List[List[Any]]:
    rows = [
        ["Worker result", result.status, result.summary_short or ""],
        ["Expected outputs", "checked", "See Evidence section for collected files."],
    ]
    if isinstance(metrics, dict):
        checkpoints = (metrics.get("evaluation") or {}).get("checkpoints") or []
        rows.append(["Evaluated checkpoints", len(checkpoints), ", ".join(str(item) for item in checkpoints)])
        planned = _planned_variants(task)
        roles = {item.get("variant"): item.get("role_label") for item in metrics.get("candidate_roles", []) if isinstance(item, dict)}
        trained = set((metrics.get("train_summaries") or {}).keys())
        evaluated = set(str(item) for item in checkpoints)
        for variant in planned:
            if variant in trained or variant in evaluated:
                state = "done"
            else:
                state = "not run"
            rows.append([variant, state, roles.get(variant, "")])
    return rows


def _variant_rows(metrics: Any) -> List[List[Any]]:
    if not isinstance(metrics, dict):
        return []
    roles = {item.get("variant"): item for item in metrics.get("candidate_roles", []) if isinstance(item, dict)}
    rows: List[List[Any]] = []
    for item in metrics.get("single_checkpoint_metrics", []) or []:
        if not isinstance(item, dict) or item.get("variant") == "S4":
            continue
        role = roles.get(item.get("variant"), {})
        rows.append(
            [
                item.get("variant"),
                role.get("role_label", ""),
                item.get("DF40_AUC"),
                item.get("NTIRE_AUC"),
                item.get("NTIRE_TP"),
                item.get("NTIRE_FP"),
                "; ".join(str(reason) for reason in role.get("reasons", []) or []),
            ]
        )
    return rows


def _baseline_row(metrics: Any) -> str:
    if not isinstance(metrics, dict):
        return ""
    for item in metrics.get("single_checkpoint_metrics", []) or []:
        if isinstance(item, dict) and item.get("variant") == "S4":
            return (
                f"- S4 baseline: DF40 AUC {_fmt(item.get('DF40_AUC'))}, "
                f"NTIRE AUC {_fmt(item.get('NTIRE_AUC'))}, "
                f"NTIRE TP/FP {item.get('NTIRE_TP')}/{item.get('NTIRE_FP')}"
            )
    return ""


def _rescue_rows(metrics: Any) -> List[List[Any]]:
    if not isinstance(metrics, dict):
        return []
    rows: List[List[Any]] = []
    for item in metrics.get("pairwise_rescue_harm_vs_S4", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("group") not in {"all", "DF40", "NTIRE_fake", "NTIRE_real"}:
            continue
        rows.append(
            [
                item.get("variant"),
                item.get("group"),
                item.get("rescue"),
                item.get("harm"),
                item.get("net"),
                item.get("fake_fn_decrease"),
                item.get("real_fp_increase"),
            ]
        )
    return rows


def _metrics_files(metrics: Any) -> List[str]:
    if not isinstance(metrics, dict):
        return []
    raw_files = metrics.get("raw_files") or {}
    if not isinstance(raw_files, dict):
        return []
    return [f"{key}: {value}" for key, value in raw_files.items()]


def excerpt(path: Path, limit: int = 3000) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"


def write_task_report(
    report_path: Path,
    task: TaskBook,
    result: WorkerResult,
    result_path: Path,
    diff_stat: str = "",
) -> Path:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    metrics_payload = _load_metrics(result_path / "metrics.json")
    metrics = _metrics_summary(result_path / "metrics.json")
    stdout = excerpt(result_path / "stdout.log", 1200)
    stderr = excerpt(result_path / "stderr.log", 1200)
    worker_notes = excerpt(result_path / "worker_notes.md", 5000)
    worker_findings = _section(worker_notes, "Key findings") or "No worker findings were provided."
    worker_changes = _section(worker_notes, "What I changed")
    lines = [
        f"# {task.task_id} - {task.title}",
        "",
        "## Decision Summary",
        f"- Status: {result.status}",
        f"- Summary: {result.summary_short or 'No summary was provided by the worker.'}",
        _baseline_row(metrics_payload),
        "",
        "## Objective",
        task.objective or "Not specified.",
        "",
        "## Completion Check",
        _markdown_table(["Item", "State", "Notes"], _completion_rows(task, result, metrics_payload)),
        "",
        "## Key findings",
        worker_findings,
        "",
        "## Variant Results",
        _markdown_table(["Variant", "Role", "DF40 AUC", "NTIRE AUC", "NTIRE TP", "NTIRE FP", "Notes"], _variant_rows(metrics_payload)),
        "",
        "## Rescue / Harm vs S4",
        _markdown_table(["Variant", "Group", "Rescue", "Harm", "Net", "Fake FN decrease", "Real FP increase"], _rescue_rows(metrics_payload)),
        "",
        "## What Changed",
        worker_changes or _bullets(result.changed_files),
        "",
        "## Changed files",
        _bullets(result.changed_files),
        "",
        "## Evidence",
        _bullets([str(result_path / name) for name in ("result.yaml", "worker_notes.md", "metrics.json", "patch.diff", "stdout.log", "stderr.log") if (result_path / name).exists()]),
        "",
        "## Risks",
        _bullets(result.risks),
        "",
        "## Next actions",
        _bullets(result.next_actions),
        "",
        "## Raw Metric Files",
        _bullets(_metrics_files(metrics_payload) or metrics),
        "",
        "## Diff stat",
        "```text",
        diff_stat or "No source changes declared by worker.",
        "```",
        "",
        "## Appendix: Log Excerpts",
        "These excerpts are diagnostic only and may include stale messages from previous attempts; prefer `result.yaml` and `metrics.json` for final status.",
        "",
        "### stdout excerpt",
        "```text",
        stdout or "",
        "```",
        "",
        "### stderr excerpt",
        "```text",
        stderr or "",
        "```",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def write_index(report_path: Path, registry: Dict[str, Any]) -> Path:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    tasks = registry.get("tasks", {})
    lines = ["# Experiment Index", ""]
    if not tasks:
        lines.append("No tasks registered.")
    else:
        lines.append("| Task | Status | Worker | Summary | Report |")
        lines.append("| --- | --- | --- | --- | --- |")
        for task_id, record in sorted(tasks.items()):
            report = record.get("report_path") or ""
            lines.append(
                f"| {task_id} | {record.get('status', '')} | {record.get('worker', '')} | "
                f"{str(record.get('summary_short', '')).replace('|', '/')} | {report} |"
            )
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def write_latest_summary(report_path: Path, registry: Dict[str, Any]) -> Path:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    tasks = list(registry.get("tasks", {}).values())
    tasks.sort(key=lambda item: item.get("finished_at") or item.get("started_at") or item.get("created_at") or "", reverse=True)
    latest = tasks[:5]
    lines = ["# Latest Summary", ""]
    if not latest:
        lines.append("No recent tasks.")
    for record in latest:
        lines.extend(
            [
                f"## {record.get('task_id')} - {record.get('title', '')}",
                "",
                f"- Status: {record.get('status', '')}",
                f"- Worker: {record.get('worker', '')}",
                f"- Summary: {record.get('summary_short', '')}",
                f"- Report: {record.get('report_path', '')}",
                "",
            ]
        )
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path
