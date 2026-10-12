from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Dict, Optional

from .schema import TaskBook


DEFAULT_OLLAMA_MODEL = "qwen2.5-3b-local:latest"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"


@dataclass
class SupervisionDecision:
    action: str
    confidence: float
    reason_code: str
    summary: str
    evidence: list[str]
    concerns: list[str]
    next_check_minutes: int
    source: str

    def as_dict(self) -> Dict[str, object]:
        return {
            "action": self.action,
            "confidence": self.confidence,
            "reason_code": self.reason_code,
            "summary": self.summary,
            "evidence": self.evidence,
            "concerns": self.concerns,
            "next_check_minutes": self.next_check_minutes,
            "source": self.source,
        }


def _strip_code_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _parse_json_object(text: str) -> Optional[Dict[str, object]]:
    cleaned = _strip_code_fences(text)
    try:
        payload = json.loads(cleaned)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            return None


def _compact_task(task: TaskBook) -> str:
    raw = task.raw_front_matter
    keys = [
        "task_id",
        "title",
        "resource_hint",
        "runtime_budget_hours",
        "expected_runtime_hours",
        "nvidia_smi_ids_allowed",
        "gpu_ids_allowed",
        "min_free_gpu_mem_gb",
        "allow_code_changes",
        "code_change_scope",
        "allow_delete",
        "allow_overwrite_outputs",
        "allow_network",
        "depends_on",
        "requires_artifacts_from",
    ]
    header = {key: raw.get(key) for key in keys if key in raw}
    body_excerpt = task.body[:1800]
    return json.dumps({"front_matter": header, "objective": task.objective, "body_excerpt": body_excerpt}, ensure_ascii=False, indent=2)


def build_supervision_prompt(task: TaskBook, remote_status_text: str, extra_evidence: str = "") -> str:
    return f"""You are a local LLM supervisor for a remote Codex experiment.

Your job is to judge whether the remote worker is following the task book and whether the orchestrator should continue waiting, collect results, warn the human, or recommend stopping.

Allowed actions:
- continue: worker is active and aligned with the task.
- wait: worker is active but there is not enough new evidence; check again later.
- wait_gpu: worker stopped or cannot proceed because GPU memory/resources are unavailable; wait and retry later.
- codex_confirm: worker stopped unexpectedly or status is ambiguous; ask Codex to write/confirm result state before collecting.
- collect: worker appears finished and result files should be pulled back.
- warn: worker is alive but there is a concerning issue that needs human attention.
- stop_recommended: worker appears clearly unsafe, off-task, destructive, or violating task constraints. Do not issue stop commands yourself.

Return strict JSON only:
{{
  "action": "continue|wait|wait_gpu|codex_confirm|collect|warn|stop_recommended",
  "confidence": 0.0,
  "reason_code": "worker_active_progressing",
  "summary": "one or two sentences",
  "evidence": ["specific observed evidence"],
  "concerns": ["specific concern, or empty"],
  "next_check_minutes": 15
}}

Important rules:
- Do not treat long runtime as failure when the task explicitly allows long GPU training.
- Prefer continue when pid is alive, logs are changing, generated configs match the task variants, and outputs are in the expected directory.
- Recommend stop only for clear violations: deletion/reset/clean, writing outside allowed roots, installing packages, network access, using target labels for training/selection, or running an unrelated experiment.
- Do not flag a safety warning for negated audit sentences such as "No delete commands were run"; only flag actual risky commands or affirmative evidence.
- If the worker stopped because of CUDA out-of-memory, unavailable GPU, or temporary resource pressure, use wait_gpu.
- If the worker stopped but result.yaml is missing or stale, use codex_confirm.
- If the worker is finished, action should be collect.

Useful reason_code values:
- worker_active_progressing
- worker_active_no_recent_log
- gpu_oom_detected
- gpu_memory_pressure
- pid_dead_result_missing
- pid_dead_result_valid
- runtime_budget_exceeded
- result_contract_invalid
- possible_off_task
- possible_destructive_command
- target_label_leak_suspected

Task book summary:
{_compact_task(task)}

Remote status and log tail:
{remote_status_text[-6000:]}

Extra evidence:
{extra_evidence[-2000:]}
"""


def call_ollama(prompt: str, model: str = DEFAULT_OLLAMA_MODEL, timeout_seconds: int = 120) -> str:
    payload = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.0},
        }
    ).encode("utf-8")
    request = urllib.request.Request(OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        body = json.loads(response.read().decode("utf-8"))
    return str(body.get("response", ""))


def fallback_decision(remote_status_text: str) -> SupervisionDecision:
    normalized = remote_status_text.replace("\r\n", "\n")
    normalized_lower = normalized.lower()
    running = "running:\nyes" in normalized
    heartbeat_age_match = re.search(r"heartbeat_age_seconds:\n\s*(\d+)", normalized)
    heartbeat_age_seconds = int(heartbeat_age_match.group(1)) if heartbeat_age_match else None
    has_result = "result.yaml" in normalized
    has_finished = "run_state:\nfinished" in normalized or '"state": "finished"' in normalized or '"state":"finished"' in normalized
    has_exit_code = re.search(r"exit_code:\n\s*\d+", normalized) is not None
    oom_markers = ["cuda out of memory", "outofmemoryerror", "cublas_status_alloc_failed", "no a100 gpu", "gpu unavailable"]
    oom_hits = [marker for marker in oom_markers if marker in normalized_lower]
    unsafe_patterns = {
        "git reset": r"\bgit\s+reset\b",
        "git clean": r"\bgit\s+clean\b",
        "pip install": r"\bpip(?:3)?\s+install\b",
        "conda install": r"\bconda\s+install\b",
        "rm -rf": r"\brm\s+-[^\n]*r[^\n]*f\b|\brm\s+-[^\n]*f[^\n]*r\b",
        "unlink/delete command": r"\b(unlink|delete-file|Remove-Item)\b",
    }
    hits = [name for name, pattern in unsafe_patterns.items() if re.search(pattern, normalized, flags=re.IGNORECASE)]
    if hits:
        return SupervisionDecision(
            action="warn",
            confidence=0.65,
            reason_code="possible_destructive_command",
            summary="Rule-based supervisor saw potentially risky command text in the log tail.",
            evidence=hits,
            concerns=["Inspect the remote log before allowing more work."],
            next_check_minutes=5,
            source="fallback",
        )
    if oom_hits:
        return SupervisionDecision(
            action="wait_gpu",
            confidence=0.75,
            reason_code="gpu_oom_detected",
            summary="Rule-based supervisor saw GPU memory or availability pressure.",
            evidence=oom_hits,
            concerns=["Wait for GPU resources or lower batch/runtime before retrying."],
            next_check_minutes=30,
            source="fallback",
        )
    if running and heartbeat_age_seconds is not None and heartbeat_age_seconds > 20 * 60:
        return SupervisionDecision(
            action="warn",
            confidence=0.65,
            reason_code="heartbeat_stale",
            summary="Remote runner still has a PID, but heartbeat is stale.",
            evidence=[f"heartbeat_age_seconds={heartbeat_age_seconds}"],
            concerns=["Check whether Codex is stuck or the heartbeat process died."],
            next_check_minutes=5,
            source="fallback",
        )
    if running:
        return SupervisionDecision(
            action="continue",
            confidence=0.55,
            reason_code="worker_active_progressing",
            summary="Remote runner is alive, so continue monitoring.",
            evidence=["running: yes"],
            concerns=[],
            next_check_minutes=15,
            source="fallback",
        )
    if has_result and has_finished and has_exit_code:
        return SupervisionDecision(
            action="collect",
            confidence=0.6,
            reason_code="pid_dead_result_valid",
            summary="Remote runner has finished and required finish signals are present.",
            evidence=["result.yaml present", "finished state detected", "exit_code present"],
            concerns=[],
            next_check_minutes=0,
            source="fallback",
        )
    if has_result and has_finished:
        return SupervisionDecision(
            action="collect",
            confidence=0.55,
            reason_code="pid_dead_result_valid_missing_exit_code",
            summary="Remote runner has finished and result.yaml exists; collect and let the result validator decide final status.",
            evidence=["result.yaml present", "finished state detected", "exit_code missing or empty"],
            concerns=["exit_code.txt is missing or empty, so the collector must validate result.yaml and artifacts strictly."],
            next_check_minutes=0,
            source="fallback",
        )
    if has_result or has_finished or has_exit_code:
        return SupervisionDecision(
            action="codex_confirm",
            confidence=0.5,
            reason_code="pid_dead_result_incomplete",
            summary="Remote runner has some finish signals, but not enough deterministic evidence to collect.",
            evidence=[
                f"result.yaml present: {has_result}",
                f"finished state detected: {has_finished}",
                f"exit_code present: {has_exit_code}",
            ],
            concerns=["Require result.yaml, finished state, and exit_code before collecting."],
            next_check_minutes=5,
            source="fallback",
        )
    return SupervisionDecision(
        action="codex_confirm",
        confidence=0.45,
        reason_code="pid_dead_result_missing",
        summary="No clear finish signal or active process was detected.",
        evidence=[],
        concerns=["Ask Codex to confirm current state or write a result package."],
        next_check_minutes=5,
        source="fallback",
    )


def supervise_with_llm(
    task: TaskBook,
    remote_status_text: str,
    extra_evidence: str = "",
    model: str = DEFAULT_OLLAMA_MODEL,
) -> SupervisionDecision:
    prompt = build_supervision_prompt(task, remote_status_text, extra_evidence=extra_evidence)
    try:
        response = call_ollama(prompt, model=model)
        payload = _parse_json_object(response)
        if not payload:
            fallback = fallback_decision(remote_status_text)
            fallback.source = f"fallback_after_unparseable_ollama:{model}"
            fallback.concerns.append(f"Ollama response was not valid JSON: {response[:300]}")
            return fallback
        action = str(payload.get("action", "wait"))
        if action not in {"continue", "wait", "wait_gpu", "codex_confirm", "collect", "warn", "stop_recommended"}:
            action = "wait"
        return SupervisionDecision(
            action=action,
            confidence=float(payload.get("confidence", 0.5) or 0.5),
            reason_code=str(payload.get("reason_code", "llm_advisory")),
            summary=str(payload.get("summary", "")),
            evidence=[str(item) for item in payload.get("evidence", []) or []],
            concerns=[str(item) for item in payload.get("concerns", []) or []],
            next_check_minutes=int(payload.get("next_check_minutes", 15) or 15),
            source=f"ollama:{model}",
        )
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        fallback = fallback_decision(remote_status_text)
        fallback.source = f"fallback_after_ollama_error:{model}"
        fallback.concerns.append(f"Ollama unavailable or failed: {exc}")
        return fallback
