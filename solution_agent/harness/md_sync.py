from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml
from pydantic import Field, model_validator

from .context_budget import ContextItem, build_bounded_context, conservative_token_estimate
from .contracts import StrictModel


Owner = Literal["local", "remote", "shared_reviewed", "derived"]
ReviewLevel = Literal["A0", "A1", "A2", "A3"]
PlanAction = Literal[
    "unchanged",
    "initial_alignment_required",
    "reader_update_proposal",
    "codex_review_required",
    "conflict",
]

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_EXPLICIT_MARKER_RE = re.compile(r"^\s*<!--\s*SYNC:([A-Za-z0-9_.-]+):(START|END)\s*-->\s*$")
_COMMENT_RE = re.compile(r"^\s*<!--\s*([^<>]+?)\s*-->\s*$")
_FRONT_KEY_RE = re.compile(r"^([A-Za-z0-9_.-]+):(?:\s|$)")
_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\(([^)\s]+)(?:\s+[^)]*)?\)")
_WIKI_LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _normalise_block_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").rstrip() + "\n"


def _slug(value: str) -> str:
    value = re.sub(r"[`*~]", "", value).strip().lower()
    value = re.sub(r"[^\w\u3400-\u9fff.-]+", "-", value, flags=re.UNICODE)
    return value.strip("-") or "section"


def safe_relative_path(value: str) -> str:
    raw = str(value).replace("\\", "/").strip()
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts or re.match(r"^[A-Za-z]:", raw):
        raise ValueError(f"unsafe relative path: {value!r}")
    return path.as_posix()


def resolve_under_root(root: Path, relative: str) -> Path:
    safe = safe_relative_path(relative)
    resolved_root = root.resolve()
    resolved = (resolved_root / Path(*PurePosixPath(safe).parts)).resolve()
    resolved.relative_to(resolved_root)
    return resolved


class EndpointConfig(StrictModel):
    path: str

    @model_validator(mode="after")
    def validate_path(self) -> "EndpointConfig":
        self.path = safe_relative_path(self.path)
        return self


class SectionMapping(StrictModel):
    section_id: str
    local_block: str
    remote_block: str
    owner: Owner
    review_level: ReviewLevel = "A1"
    required: bool = True
    priority: int = 0
    mode: Literal["semantic", "copy"] = "semantic"


class EntityMapping(StrictModel):
    entity_id: str
    local: EndpointConfig
    remote: EndpointConfig
    sections: list[SectionMapping]

    @model_validator(mode="after")
    def unique_sections(self) -> "EntityMapping":
        ids = [item.section_id for item in self.sections]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate section_id in {self.entity_id}")
        return self


class SyncMapping(StrictModel):
    schema_version: Literal["research-md-sync/v1"]
    entities: list[EntityMapping]

    @model_validator(mode="after")
    def unique_entities(self) -> "SyncMapping":
        ids = [item.entity_id for item in self.entities]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate entity_id")
        return self

    def entity(self, entity_id: str) -> EntityMapping:
        for item in self.entities:
            if item.entity_id == entity_id:
                return item
        raise KeyError(f"unknown entity_id: {entity_id}")


class MarkdownBlock(StrictModel):
    block_id: str
    kind: Literal["frontmatter", "heading", "managed"]
    title: str
    heading_path: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    sha256: str
    bytes: int = Field(ge=0)
    text: str


class MarkdownLink(StrictModel):
    kind: Literal["markdown", "wikilink"]
    target: str
    label: str = ""
    line: int = Field(ge=1)


class MarkdownSnapshot(StrictModel):
    schema_version: Literal["research-md-snapshot/v1"] = "research-md-snapshot/v1"
    entity_id: str
    endpoint: Literal["local", "remote"]
    source_path: str
    source_bytes: int = Field(ge=0)
    source_sha256: str
    blocks: list[MarkdownBlock]
    links: list[MarkdownLink]
    snapshot_fingerprint: str

    def block(self, block_id: str) -> MarkdownBlock:
        matches = [item for item in self.blocks if item.block_id == block_id or block_id in item.aliases]
        if len(matches) != 1:
            raise KeyError(f"block selector {block_id!r} resolved to {len(matches)} blocks")
        return matches[0]


class SectionBase(StrictModel):
    section_id: str
    local_sha256: str
    remote_sha256: str


class SyncBase(StrictModel):
    schema_version: Literal["research-md-sync-base/v1"] = "research-md-sync-base/v1"
    entity_id: str
    mapping_sha256: str
    sections: list[SectionBase]


class SectionPlan(StrictModel):
    section_id: str
    owner: Owner
    review_level: ReviewLevel
    action: PlanAction
    local_changed: bool | None
    remote_changed: bool | None
    source_endpoint: Literal["local", "remote"] | None = None
    destination_endpoint: Literal["local", "remote"] | None = None
    reason: str
    local_block: str
    remote_block: str
    local_sha256: str
    remote_sha256: str


class SyncPlan(StrictModel):
    schema_version: Literal["research-md-sync-plan/v1"] = "research-md-sync-plan/v1"
    entity_id: str
    write_enabled: Literal[False] = False
    mapping_sha256: str
    local_snapshot_fingerprint: str
    remote_snapshot_fingerprint: str
    sections: list[SectionPlan]
    conflicts: list[str]
    operations: list[dict[str, Any]] = Field(default_factory=list)


def load_mapping(path: Path) -> tuple[SyncMapping, str]:
    raw = path.read_bytes()
    payload = yaml.safe_load(raw.decode("utf-8-sig")) or {}
    return SyncMapping.model_validate(payload), _sha256_bytes(raw)


def _frontmatter_blocks(lines: list[str]) -> tuple[list[MarkdownBlock], int]:
    if not lines or lines[0].strip() != "---":
        return [], 0
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        raise ValueError("unterminated YAML frontmatter")
    blocks: list[MarkdownBlock] = []
    starts: list[tuple[str, int]] = []
    for index in range(1, end):
        match = _FRONT_KEY_RE.match(lines[index])
        if match and not lines[index].startswith((" ", "\t")):
            starts.append((match.group(1), index))
    for pos, (key, start) in enumerate(starts):
        stop = starts[pos + 1][1] if pos + 1 < len(starts) else end
        text = _normalise_block_text("\n".join(lines[start:stop]))
        blocks.append(
            MarkdownBlock(
                block_id=f"frontmatter:{key}",
                kind="frontmatter",
                title=key,
                start_line=start + 1,
                end_line=stop,
                sha256=_sha256_text(text),
                bytes=len(text.encode("utf-8")),
                text=text,
            )
        )
    return blocks, end + 1


def parse_markdown_snapshot(
    source: Path,
    *,
    entity_id: str,
    endpoint: Literal["local", "remote"],
    source_path: str,
    max_input_bytes: int = 524_288,
) -> MarkdownSnapshot:
    raw = source.read_bytes()
    if len(raw) > max_input_bytes:
        raise ValueError(f"Markdown source exceeds max_input_bytes: {len(raw)}>{max_input_bytes}")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Markdown source must be UTF-8") from exc
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalised.splitlines()
    blocks, body_start = _frontmatter_blocks(lines)

    headings: list[dict[str, Any]] = []
    managed: list[MarkdownBlock] = []
    pending_markers: list[str] = []
    open_marker: tuple[str, int] | None = None
    stack: list[str] = []
    in_fence = False
    fence_token = ""
    for index in range(body_start, len(lines)):
        line = lines[index]
        fence = _FENCE_RE.match(line)
        if fence:
            token = fence.group(1)
            if not in_fence:
                in_fence, fence_token = True, token
            elif token == fence_token:
                in_fence, fence_token = False, ""
            continue
        if in_fence:
            continue
        marker = _EXPLICIT_MARKER_RE.match(line)
        if marker:
            name, action = marker.groups()
            if action == "START":
                if open_marker is not None:
                    raise ValueError("nested SYNC managed blocks are not allowed")
                open_marker = (name, index)
            else:
                if open_marker is None or open_marker[0] != name:
                    raise ValueError(f"unmatched SYNC END marker: {name}")
                start = open_marker[1]
                content = _normalise_block_text("\n".join(lines[start + 1 : index]))
                managed.append(
                    MarkdownBlock(
                        block_id=f"managed:{name}",
                        kind="managed",
                        title=name,
                        start_line=start + 1,
                        end_line=index + 1,
                        sha256=_sha256_text(content),
                        bytes=len(content.encode("utf-8")),
                        text=content,
                    )
                )
                open_marker = None
            continue
        comment = _COMMENT_RE.match(line)
        if comment:
            pending_markers.append("marker:" + _slug(comment.group(1)))
            continue
        heading = _HEADING_RE.match(line)
        if not heading:
            continue
        level, title = len(heading.group(1)), heading.group(2).strip()
        stack = stack[: level - 1]
        stack.append(title)
        headings.append(
            {
                "index": index,
                "level": level,
                "title": title,
                "path": list(stack),
                "aliases": pending_markers,
            }
        )
        pending_markers = []
    if open_marker is not None:
        raise ValueError(f"unterminated SYNC managed block: {open_marker[0]}")

    seen: dict[str, int] = {}
    for pos, item in enumerate(headings):
        start = int(item["index"])
        stop = int(headings[pos + 1]["index"]) if pos + 1 < len(headings) else len(lines)
        base_id = "heading:" + "/".join(_slug(part) for part in item["path"])
        seen[base_id] = seen.get(base_id, 0) + 1
        block_id = base_id if seen[base_id] == 1 else f"{base_id}~{seen[base_id]}"
        content = _normalise_block_text("\n".join(lines[start:stop]))
        blocks.append(
            MarkdownBlock(
                block_id=block_id,
                kind="heading",
                title=item["title"],
                heading_path=item["path"],
                aliases=item["aliases"],
                start_line=start + 1,
                end_line=stop,
                sha256=_sha256_text(content),
                bytes=len(content.encode("utf-8")),
                text=content,
            )
        )
    blocks.extend(managed)
    blocks.sort(key=lambda item: (item.start_line, item.kind, item.block_id))

    links: list[MarkdownLink] = []
    in_fence = False
    fence_token = ""
    for index, line in enumerate(lines):
        fence = _FENCE_RE.match(line)
        if fence:
            token = fence.group(1)
            if not in_fence:
                in_fence, fence_token = True, token
            elif token == fence_token:
                in_fence, fence_token = False, ""
            continue
        if in_fence:
            continue
        for match in _MD_LINK_RE.finditer(line):
            links.append(MarkdownLink(kind="markdown", label=match.group(1), target=match.group(2), line=index + 1))
        for match in _WIKI_LINK_RE.finditer(line):
            links.append(MarkdownLink(kind="wikilink", label=match.group(2) or "", target=match.group(1), line=index + 1))

    fingerprint_payload = {
        "entity_id": entity_id,
        "endpoint": endpoint,
        "source_path": safe_relative_path(source_path),
        "source_sha256": _sha256_bytes(raw),
        "blocks": [(item.block_id, item.sha256) for item in blocks],
        "links": [(item.kind, item.target, item.label, item.line) for item in links],
    }
    fingerprint = _sha256_text(json.dumps(fingerprint_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return MarkdownSnapshot(
        entity_id=entity_id,
        endpoint=endpoint,
        source_path=safe_relative_path(source_path),
        source_bytes=len(raw),
        source_sha256=_sha256_bytes(raw),
        blocks=blocks,
        links=links,
        snapshot_fingerprint=fingerprint,
    )


def _plan_section(
    mapping: SectionMapping,
    local: MarkdownBlock,
    remote: MarkdownBlock,
    base: SectionBase | None,
) -> SectionPlan:
    common = dict(
        section_id=mapping.section_id,
        owner=mapping.owner,
        review_level=mapping.review_level,
        local_block=local.block_id,
        remote_block=remote.block_id,
        local_sha256=local.sha256,
        remote_sha256=remote.sha256,
    )
    if base is None:
        return SectionPlan(
            **common,
            action="initial_alignment_required",
            local_changed=None,
            remote_changed=None,
            reason="No accepted three-way base exists; Phase 0 refuses to infer authority.",
        )
    local_changed = local.sha256 != base.local_sha256
    remote_changed = remote.sha256 != base.remote_sha256
    if not local_changed and not remote_changed:
        return SectionPlan(**common, action="unchanged", local_changed=False, remote_changed=False, reason="Both endpoints match the accepted base.")
    if local_changed and remote_changed:
        return SectionPlan(**common, action="conflict", local_changed=True, remote_changed=True, reason="Both endpoints changed since the accepted base.")

    changed: Literal["local", "remote"] = "local" if local_changed else "remote"
    other: Literal["local", "remote"] = "remote" if changed == "local" else "local"
    if mapping.owner in {"local", "remote"}:
        if changed != mapping.owner:
            return SectionPlan(
                **common,
                action="conflict",
                local_changed=local_changed,
                remote_changed=remote_changed,
                source_endpoint=changed,
                destination_endpoint=mapping.owner,
                reason=f"Non-owner endpoint {changed} changed while {mapping.owner} owns this section.",
            )
        action: PlanAction = "reader_update_proposal" if mapping.review_level in {"A0", "A1"} else "codex_review_required"
        return SectionPlan(
            **common,
            action=action,
            local_changed=local_changed,
            remote_changed=remote_changed,
            source_endpoint=changed,
            destination_endpoint=other,
            reason=f"Authoritative {changed} block changed; produce a semantic proposal only.",
        )
    if mapping.owner == "shared_reviewed":
        return SectionPlan(
            **common,
            action="codex_review_required",
            local_changed=local_changed,
            remote_changed=remote_changed,
            source_endpoint=changed,
            destination_endpoint=other,
            reason="A shared-reviewed section changed on one endpoint and needs a reviewed semantic proposal.",
        )
    return SectionPlan(
        **common,
        action="reader_update_proposal",
        local_changed=local_changed,
        remote_changed=remote_changed,
        source_endpoint=changed,
        destination_endpoint=other,
        reason="Derived section may be regenerated as a proposal; no authoritative write is allowed.",
    )


def build_sync_artifacts(
    mapping: SyncMapping,
    mapping_sha256: str,
    entity_id: str,
    local_snapshot: MarkdownSnapshot,
    remote_snapshot: MarkdownSnapshot,
    *,
    base: SyncBase | None = None,
    max_reader_tokens: int = 4096,
) -> tuple[SyncPlan, dict[str, Any], dict[str, Any], SyncBase, dict[str, Any]]:
    entity = mapping.entity(entity_id)
    if local_snapshot.entity_id != entity_id or remote_snapshot.entity_id != entity_id:
        raise ValueError("snapshot entity_id mismatch")
    if local_snapshot.endpoint != "local" or remote_snapshot.endpoint != "remote":
        raise ValueError("snapshot endpoint mismatch")
    if local_snapshot.source_path != entity.local.path or remote_snapshot.source_path != entity.remote.path:
        raise ValueError("snapshot source_path does not match the mapped endpoint")
    if base is not None and (base.entity_id != entity_id or base.mapping_sha256 != mapping_sha256):
        raise ValueError("accepted base does not match entity or mapping")
    base_by_id = {item.section_id: item for item in (base.sections if base else [])}
    plans: list[SectionPlan] = []
    resolved: dict[str, tuple[MarkdownBlock, MarkdownBlock, SectionMapping]] = {}
    for section in entity.sections:
        try:
            local = local_snapshot.block(section.local_block)
            remote = remote_snapshot.block(section.remote_block)
        except KeyError:
            if section.required:
                raise
            continue
        resolved[section.section_id] = (local, remote, section)
        plans.append(_plan_section(section, local, remote, base_by_id.get(section.section_id)))
    conflicts = [item.section_id for item in plans if item.action == "conflict"]
    plan = SyncPlan(
        entity_id=entity_id,
        mapping_sha256=mapping_sha256,
        local_snapshot_fingerprint=local_snapshot.snapshot_fingerprint,
        remote_snapshot_fingerprint=remote_snapshot.snapshot_fingerprint,
        sections=plans,
        conflicts=conflicts,
    )

    reader_items: list[ContextItem] = []
    reader_tasks: list[dict[str, Any]] = []
    for item in plans:
        if item.action not in {"reader_update_proposal", "codex_review_required"}:
            continue
        local, remote, section = resolved[item.section_id]
        payload = {
            "section_id": item.section_id,
            "owner": item.owner,
            "review_level": item.review_level,
            "mode": section.mode,
            "source_endpoint": item.source_endpoint,
            "destination_endpoint": item.destination_endpoint,
            "source": (local if item.source_endpoint == "local" else remote).model_dump(mode="json"),
            "destination": (remote if item.destination_endpoint == "remote" else local).model_dump(mode="json"),
            "instruction": "Draft a semantic proposal only. Preserve uncertainty, evidence references, and destination conventions. Do not write files.",
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        reader_items.append(ContextItem(name=item.section_id, text=encoded, priority=section.priority, required=section.required))
        reader_tasks.append({key: payload[key] for key in ("section_id", "owner", "review_level", "mode", "source_endpoint", "destination_endpoint")})
    bounded = build_bounded_context(reader_items, max_estimated_tokens=max_reader_tokens)
    reader_packet = {
        "schema_version": "research-md-reader-packet/v1",
        "entity_id": entity_id,
        "write_enabled": False,
        "tasks": reader_tasks,
        "included": [item.__dict__ for item in bounded.included],
        "omitted": list(bounded.omitted),
        "estimated_tokens": bounded.estimated_tokens,
        "max_estimated_tokens": bounded.max_estimated_tokens,
        "estimator": bounded.estimator,
        "usage_note": "Preflight estimate only; not provider-reported usage.",
    }
    authorizations = sorted({item.review_level for item in plans if item.action in {"codex_review_required", "conflict"}})
    initial_sections = sorted(item.section_id for item in plans if item.action == "initial_alignment_required")
    if initial_sections:
        authorizations.append("initial_base_acceptance")
    if conflicts:
        next_action = "resolve_conflicts"
    elif initial_sections:
        next_action = "approve_initial_alignment"
    elif reader_tasks:
        next_action = "run_reader_manager"
    else:
        next_action = "none"
    evidence_ids = sorted(
        [
            local_snapshot.snapshot_fingerprint,
            remote_snapshot.snapshot_fingerprint,
            mapping_sha256,
        ]
    )
    critical = {
        "entity_id": entity_id,
        "execution_status": "plan_succeeded",
        "scientific_status": "unchanged",
        "conflicts": conflicts,
        "authorizations_required": authorizations,
        "initial_alignment_sections": initial_sections,
        "reader_tasks_pending": sorted(item["section_id"] for item in reader_tasks),
        "next_action": next_action,
        "evidence_ids": evidence_ids,
        "section_outcomes": sorted((item.section_id, item.action, item.reason) for item in plans),
    }
    decision_packet = {
        "schema_version": "research-md-codex-decision/v1",
        **critical,
        "requires_codex": bool(conflicts or authorizations),
        "evidence_refs": {
            "local_snapshot": local_snapshot.snapshot_fingerprint,
            "remote_snapshot": remote_snapshot.snapshot_fingerprint,
            "mapping": mapping_sha256,
        },
        "decision_fingerprint": _sha256_text(json.dumps(critical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
    }
    base_candidate = SyncBase(
        entity_id=entity_id,
        mapping_sha256=mapping_sha256,
        sections=[
            SectionBase(section_id=item.section_id, local_sha256=item.local_sha256, remote_sha256=item.remote_sha256)
            for item in plans
        ],
    )
    baseline_bytes = local_snapshot.source_bytes + remote_snapshot.source_bytes
    candidate_text = json.dumps(decision_packet, ensure_ascii=False, sort_keys=True)
    metrics = {
        "execution_status": "succeeded",
        "write_enabled": False,
        "section_count": len(plans),
        "conflict_count": len(conflicts),
        "reader_task_count": len(reader_tasks),
        "baseline_source_bytes": baseline_bytes,
        "candidate_decision_bytes": len(candidate_text.encode("utf-8")),
        "candidate_estimated_tokens": conservative_token_estimate(candidate_text),
        "provider_usage_measured": False,
        "token_savings_claim_allowed": False,
        "usage_note": "Byte and estimated-token fields are preflight only. A claim requires provider-reported paired A/B usage and equivalent decision fingerprints.",
    }
    return plan, reader_packet, decision_packet, base_candidate, metrics
