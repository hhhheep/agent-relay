"""Real API adapters for small_task_worker using OpenAICompatibleChatClient."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from solution_agent.remote_materializer.model_clients import (
    OpenAICompatibleChatClient,
    OpenAICompatibleConfig,
)
from .scope_planner import ProposedScope
from .task_contract import TaskContract

_PROMPTS = Path(__file__).parent / "prompts"


def _load_prompt(name: str) -> str:
    return (_PROMPTS / name).read_text(encoding="utf-8", errors="replace").strip()


def _build_scope_user_prompt(task: TaskContract, context: str) -> str:
    lines = [
        f"## Task: {task.task_id}",
        f"**Title:** {task.title}",
        f"**Type:** {task.task_type}",
        "",
        "### Goal",
        task.goal or "(not specified)",
        "",
        "### Current Problem",
        task.current_problem or "(not specified)",
        "",
        "### Allowed Modify",
        "\n".join(f"- {p}" for p in task.allowed_modify) or "(none)",
        "",
        "### Allowed Commands",
        "\n".join(f"- {c}" for c in task.allowed_commands) or "(none)",
        "",
        "### Escalation Rules",
        "\n".join(f"- {r}" for r in task.escalation_rules) or "(none)",
    ]
    if context.strip():
        lines += ["", "### Context", context[:4000]]
    return "\n".join(lines)


def _parse_scope_json(text: str) -> ProposedScope:
    # Strip markdown fences if model wrapped it
    cleaned = re.sub(r"^```[a-z]*\n?|\n?```$", "", text.strip(), flags=re.MULTILINE)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return ProposedScope(
            risk="high",
            inspect_paths=[],
            modify_paths=[],
            commands_to_run=[],
            reason=f"scope JSON parse error: {exc}",
            escalation_required=True,
            escalation_reason=f"model returned unparseable scope JSON: {text[:200]}",
        )
    return ProposedScope(
        risk=str(data.get("risk", "high")),
        inspect_paths=list(data.get("inspect_paths", [])),
        modify_paths=list(data.get("modify_paths", [])),
        commands_to_run=list(data.get("commands_to_run", [])),
        reason=str(data.get("reason", "")),
        escalation_required=bool(data.get("escalation_required", False)),
        escalation_reason=str(data.get("escalation_reason", "")),
    )


def _build_patch_user_prompt(
    task: TaskContract,
    scope: ProposedScope,
    context: str,
    feedback: str,
) -> str:
    lines = [
        f"## Task: {task.task_id}",
        f"**Goal:** {task.goal or '(not specified)'}",
        "",
        "### Scope",
        f"- Modify: {', '.join(scope.modify_paths) or '(none)'}",
        f"- Commands after patch: {', '.join(scope.commands_to_run) or '(none)'}",
        "",
        "### Current Problem",
        task.current_problem or "(not specified)",
        "",
        "### Expected Behavior",
        task.expected_behavior or "(not specified)",
    ]
    if context.strip():
        lines += ["", "### Relevant Context", context[:6000]]
    if feedback.strip():
        lines += ["", "---", feedback]
    return "\n".join(lines)


def _normalize_diff(diff: str) -> str:
    """Fix common model formatting issues in unified diffs."""
    lines = []
    for line in diff.splitlines():
        # Split "--- a/foo +++ b/foo" onto two lines if merged
        merged = re.match(r"^(---\s+\S.*?)\s+(\+\+\+\s+\S.*)$", line)
        if merged:
            lines.append(merged.group(1))
            lines.append(merged.group(2))
            continue
        lines.append(line)
    return "\n".join(lines)


def _extract_diff(text: str) -> str:
    """Extract unified diff from model response — strip prose and fences."""
    # Try to find a fenced diff block
    fence_match = re.search(r"```(?:diff|patch)?\n(.*?)```", text, re.DOTALL)
    if fence_match:
        return _normalize_diff(fence_match.group(1).strip())
    # If response starts with diff markers, return as-is
    stripped = text.strip()
    if stripped.startswith("---") or stripped.startswith("diff --git"):
        return _normalize_diff(stripped)
    # Try to find diff block anywhere
    diff_match = re.search(r"((?:^---\s.+\n\+\+\+\s.+\n@@.+?(?=\Z|^diff |\Z))+)",
                           text, re.MULTILINE | re.DOTALL)
    if diff_match:
        return _normalize_diff(diff_match.group(1).strip())
    return ""


@dataclass
class DeepSeekScopeAdapter:
    """Calls deepseek-v4-flash for scope planning (JSON output)."""
    client: OpenAICompatibleChatClient

    def propose_scope(self, task: TaskContract, context: str) -> ProposedScope:
        system = _load_prompt("scope_system.md")
        user = _build_scope_user_prompt(task, context)
        response = self.client.complete([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])
        return _parse_scope_json(response)


@dataclass
class DeepSeekPatchAdapter:
    """Calls deepseek-v4-flash (or qwen3-coder-next) for patch generation."""
    client: OpenAICompatibleChatClient

    def generate_patch(
        self,
        task: TaskContract,
        scope: ProposedScope,
        context: str,
        feedback: str,
    ) -> str:
        system = _load_prompt("patch_system.md")
        user = _build_patch_user_prompt(task, scope, context, feedback)
        return self.client.complete([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])


def _make_client(
    base_url: str,
    api_key_env: str,
    model: str,
    temperature: float = 0.0,
    max_tokens: int = 4096,
    response_format: Optional[str] = None,
    timeout_seconds: int = 120,
) -> OpenAICompatibleChatClient:
    cfg = OpenAICompatibleConfig(
        provider="api",
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        response_format=response_format,
    )
    return OpenAICompatibleChatClient(cfg)


def build_deepseek_adapters(
    base_url: str = "https://api.deepseek.com",
    api_key_env: str = "DEEPSEEK_API_KEY",
    scope_model: str = "deepseek-chat",
    patch_model: str = "deepseek-chat",
) -> tuple[DeepSeekScopeAdapter, DeepSeekPatchAdapter]:
    """Factory: returns (scope_adapter, patch_adapter) using deepseek."""
    scope_client = _make_client(
        base_url=base_url,
        api_key_env=api_key_env,
        model=scope_model,
        temperature=0.0,
        max_tokens=1024,
        response_format="json_object",
    )
    patch_client = _make_client(
        base_url=base_url,
        api_key_env=api_key_env,
        model=patch_model,
        temperature=0.1,
        max_tokens=4096,
    )
    return DeepSeekScopeAdapter(scope_client), DeepSeekPatchAdapter(patch_client)


def build_bailian_adapters(
    base_url: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    api_key_env: str = "DASHSCOPE_API_KEY",
    scope_model: str = "qwen-flash",
    patch_model: str = "qwen3-coder-next",
) -> tuple[DeepSeekScopeAdapter, DeepSeekPatchAdapter]:
    """Factory: returns (scope_adapter, patch_adapter) using Bailian/DashScope."""
    scope_client = _make_client(
        base_url=base_url,
        api_key_env=api_key_env,
        model=scope_model,
        temperature=0.0,
        max_tokens=1024,
        response_format="json_object",
    )
    patch_client = _make_client(
        base_url=base_url,
        api_key_env=api_key_env,
        model=patch_model,
        temperature=0.1,
        max_tokens=8192,
    )
    return DeepSeekScopeAdapter(scope_client), DeepSeekPatchAdapter(patch_client)
