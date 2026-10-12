from __future__ import annotations

import re
import json
from dataclasses import dataclass
from pathlib import Path

import yaml


FENCE_RE = re.compile(
    r"```(?P<lang>[A-Za-z0-9_-]+)?\s+file=\"(?P<name>[^\"]+)\"\s*>?\s*\n(?P<body>.*?)\n```",
    re.DOTALL,
)
ANY_FENCE_RE = re.compile(
    r"```(?P<lang>[A-Za-z0-9_-]+)?[^\n]*\n(?P<body>.*?)\n```",
    re.DOTALL,
)


@dataclass
class ParsedArtifact:
    name: str
    text: str


def _safe_name(name: str) -> str:
    cleaned = name.strip().replace("\\", "/")
    if not cleaned or cleaned.startswith("/") or ".." in Path(cleaned).parts:
        return ""
    return cleaned


def _loads_mapping(lang: str, body: str) -> dict | None:
    try:
        if lang.lower() == "json":
            data = json.loads(body)
        elif lang.lower() in {"yaml", "yml"}:
            data = yaml.safe_load(body)
        else:
            stripped = body.lstrip()
            if stripped.startswith("{"):
                data = json.loads(body)
            else:
                data = yaml.safe_load(body)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _infer_artifact_name(lang: str, body: str) -> str:
    data = _loads_mapping(lang or "", body)
    if not data:
        return ""
    keys = set(data.keys())
    if {"read_paths", "write_paths", "data_access", "execution_strategy"}.issubset(keys):
        return "execution_plan.json"
    if {"task_id", "workspace", "command"}.issubset(keys):
        return "execution_spec.yaml"
    return ""


def _repair_json_artifact_text(name: str, text: str) -> str:
    if not name.endswith(".json"):
        return text
    candidate = text.rstrip()
    try:
        json.loads(candidate)
        return candidate + "\n"
    except json.JSONDecodeError:
        pass
    missing_closing = candidate.count("{") - candidate.count("}")
    if missing_closing <= 0 or missing_closing > 3:
        return text
    repaired = candidate + ("\n" + ("}" * missing_closing))
    try:
        json.loads(repaired)
    except json.JSONDecodeError:
        return text
    return repaired + "\n"


def parse_fenced_artifacts(raw_text: str) -> list[ParsedArtifact]:
    artifacts: list[ParsedArtifact] = []
    named_spans: set[tuple[int, int]] = set()
    for match in FENCE_RE.finditer(raw_text):
        named_spans.add(match.span())
        name = _safe_name(match.group("name"))
        if not name:
            continue
        text = match.group("body").rstrip() + "\n"
        artifacts.append(ParsedArtifact(name=name, text=_repair_json_artifact_text(name, text)))
    existing_names = {artifact.name for artifact in artifacts}
    for match in ANY_FENCE_RE.finditer(raw_text):
        if match.span() in named_spans:
            continue
        body = match.group("body").rstrip()
        name = _infer_artifact_name(match.group("lang") or "", body)
        if name and name not in existing_names:
            artifacts.append(ParsedArtifact(name=name, text=body + "\n"))
            existing_names.add(name)
    return artifacts


def write_artifacts(artifacts: list[ParsedArtifact], output_dir: Path, required_outputs: list[str]) -> list[Path]:
    required = {item.replace("\\", "/") for item in required_outputs}
    written: list[Path] = []
    for artifact in artifacts:
        if artifact.name not in required:
            continue
        target = output_dir / artifact.name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(artifact.text)
        written.append(target)
    return written
