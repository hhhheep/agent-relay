#!/usr/bin/env python3
"""Validate the canonical Solution Agent Harness field-feedback format."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


ISSUE_RE = re.compile(
    r"^- \[(HF-\d{3})\]\[(defect|environment|safety-stop|decision)\]"
    r"\[(open|resolved)\]\[(\d{4}-\d{2}-\d{2})\] (.+)$"
)
INDEX_RE = re.compile(r"^- (HF-\d{3}) - (.+)$")
COMMENT_RE = re.compile(
    r"^  - \[(HF-\d{3}-C\d{3})\]"
    r"\[(progress|reply|resolved|reopened)\]"
    r"\[(\d{4}-\d{2}-\d{2})\]"
    r"\[reply-to:(HF-\d{3}(?:-C\d{3})?)\] (.+)$"
)
FORBIDDEN_RESOLVED = re.compile(
    r"\bpending\b|\bremains open\b|\bstill open\b|\bincomplete\b|尚未|仍未|待處理",
    re.IGNORECASE,
)


def lint(path: Path) -> dict:
    lines = path.read_text(encoding="utf-8").splitlines()
    errors: list[str] = []
    try:
        open_heading = lines.index("## Open issues")
        issues_heading = lines.index("## Issues")
    except ValueError as exc:
        return {"ok": False, "errors": [f"missing required heading: {exc}"], "path": str(path)}
    if open_heading >= issues_heading:
        errors.append("Open issues must appear before Issues")

    index: list[str] = []
    for line_no, line in enumerate(lines[open_heading + 1 : issues_heading], open_heading + 2):
        if not line.strip():
            continue
        match = INDEX_RE.match(line)
        if not match:
            errors.append(f"line {line_no}: invalid open-index entry")
            continue
        index.append(match.group(1))

    issues: dict[str, dict] = {}
    comment_ids: set[str] = set()
    current_id: str | None = None
    for line_no, line in enumerate(lines[issues_heading + 1 :], issues_heading + 2):
        if not line.strip():
            continue
        issue = ISSUE_RE.match(line)
        if issue:
            issue_id, issue_class, state, opened, text = issue.groups()
            if issue_id in issues:
                errors.append(f"line {line_no}: duplicate issue id {issue_id}")
            if "Evidence:" not in text or "Impact:" not in text or "Next:" not in text:
                errors.append(f"line {line_no}: issue must contain Evidence, Impact, and Next")
            issues[issue_id] = {
                "class": issue_class,
                "state": state,
                "opened": opened,
                "comments": [],
                "derived_state": "open",
                "line": line_no,
            }
            current_id = issue_id
            continue
        comment = COMMENT_RE.match(line)
        if comment:
            if current_id is None:
                errors.append(f"line {line_no}: comment without a preceding issue")
                continue
            comment_id, kind, date, parent, text = comment.groups()
            data = issues[current_id]
            expected_number = len(data["comments"]) + 1
            expected_id = f"{current_id}-C{expected_number:03d}"
            if comment_id in comment_ids:
                errors.append(f"line {line_no}: duplicate comment id {comment_id}")
            if comment_id != expected_id:
                errors.append(f"line {line_no}: expected comment id {expected_id}, got {comment_id}")
            valid_parents = {current_id, *(item["id"] for item in data["comments"])}
            if parent not in valid_parents:
                errors.append(f"line {line_no}: parent {parent} is not an earlier item in {current_id}")
            if date < data["opened"] or (data["comments"] and date < data["comments"][-1]["date"]):
                errors.append(f"line {line_no}: comment date is out of chronological order")
            if kind == "resolved" and FORBIDDEN_RESOLVED.search(text):
                errors.append(f"line {line_no}: resolved comment still describes open work")
            if kind == "progress" and data["derived_state"] != "open":
                errors.append(f"line {line_no}: progress requires an open issue")
            if kind == "resolved":
                if data["derived_state"] != "open":
                    errors.append(f"line {line_no}: resolved transition requires an open issue")
                data["derived_state"] = "resolved"
            elif kind == "reopened":
                if data["derived_state"] != "resolved":
                    errors.append(f"line {line_no}: reopened transition requires a resolved issue")
                data["derived_state"] = "open"
            data["comments"].append(
                {"id": comment_id, "kind": kind, "date": date, "parent": parent, "line": line_no}
            )
            comment_ids.add(comment_id)
            continue
        errors.append(f"line {line_no}: content outside canonical issue/comment format")

    open_ids = [issue_id for issue_id, data in issues.items() if data["state"] == "open"]
    if len(index) != len(set(index)):
        errors.append("open index contains duplicate ids")
    if set(index) != set(open_ids):
        errors.append(f"open index mismatch: index={index}, issues={open_ids}")

    numeric_ids = [int(issue_id.split("-")[1]) for issue_id in issues]
    if numeric_ids != sorted(numeric_ids):
        errors.append("issue ids must appear in numeric order")

    for issue_id, data in issues.items():
        if data["state"] != data["derived_state"]:
            errors.append(
                f"{issue_id}: canonical state {data['state']} does not match "
                f"comment-derived state {data['derived_state']}"
            )

    return {
        "ok": not errors,
        "path": str(path),
        "issue_count": len(issues),
        "comment_count": sum(len(data["comments"]) for data in issues.values()),
        "reopened_count": sum(
            item["kind"] == "reopened"
            for data in issues.values()
            for item in data["comments"]
        ),
        "open_ids": open_ids,
        "resolved_count": len(issues) - len(open_ids),
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("feedback_file", type=Path)
    args = parser.parse_args()
    result = lint(args.feedback_file)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()

