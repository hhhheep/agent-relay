from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Protocol

import yaml

from .api_metrics import write_api_call_metric
from .provider_adapter import ProviderAdapter, ProviderRequest, ProviderResponse


class ReaderClient(Protocol):
    def create_work_order(self, task_markdown: str, schema_text: str, prompt_text: str) -> str:
        """Return raw model output for task.md -> work_order.json."""


class MaterializerClient(Protocol):
    def materialize(self, work_order_json: str, prompt_text: str, context_pack_text: str, schemas_text: str, feedback_text: str = "") -> str:
        """Return raw model output containing fenced generated artifacts."""


class ReaderVerifierClient(Protocol):
    def verify_work_order(self, task_markdown: str, work_order_json: str, prompt_text: str) -> str:
        """Return raw JSON reviewing work_order for completeness and safety."""


@dataclass
class StaticReaderClient:
    """Test helper for deterministic controller smoke tests."""

    output: str

    def create_work_order(self, task_markdown: str, schema_text: str, prompt_text: str) -> str:
        return self.output


@dataclass
class StaticMaterializerClient:
    """Test helper for deterministic controller smoke tests."""

    outputs: list[str]
    calls: int = 0

    def materialize(self, work_order_json: str, prompt_text: str, context_pack_text: str, schemas_text: str, feedback_text: str = "") -> str:
        index = min(self.calls, len(self.outputs) - 1)
        self.calls += 1
        return self.outputs[index]


@dataclass
class StaticVerifierClient:
    """Test helper that returns a fixed verifier response."""

    output: str
    calls: int = 0

    def verify_work_order(self, task_markdown: str, work_order_json: str, prompt_text: str) -> str:
        self.calls += 1
        return self.output


@dataclass
class OpenAICompatibleConfig:
    provider: str
    model: str
    base_url: str
    api_key_env: str
    role: str = "unknown"
    temperature: float = 0.1
    max_tokens: int = 4096
    timeout_seconds: int = 120
    response_format: str | None = None

    @property
    def chat_completions_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


class OpenAICompatibleChatClient:
    """Small dependency-free client for OpenAI-compatible chat endpoints.

    The first target is Alibaba Cloud Model Studio/Bailian compatible mode.
    Keeping this tiny avoids pulling heavy SDKs onto a 1GB remote controller.
    """

    def __init__(self, config: OpenAICompatibleConfig):
        self.config = config

    def complete(self, messages: list[dict[str, str]]) -> str:
        t0 = time.monotonic()
        api_key = os.environ.get(self.config.api_key_env, "").strip()
        if not api_key:
            write_api_call_metric(
                provider=self.config.provider,
                model=self.config.model,
                role=self.config.role,
                status="error",
                latency_ms=int((time.monotonic() - t0) * 1000),
                messages=messages,
                error_type="missing_api_key",
                error_message=f"missing API key env var: {self.config.api_key_env}",
                response_format=self.config.response_format,
                max_tokens=self.config.max_tokens,
                temperature=self.config.temperature,
            )
            raise RuntimeError(f"missing API key env var: {self.config.api_key_env}")
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        if self.config.response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.config.chat_completions_url,
            data=data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            write_api_call_metric(
                provider=self.config.provider,
                model=self.config.model,
                role=self.config.role,
                status="error",
                latency_ms=int((time.monotonic() - t0) * 1000),
                messages=messages,
                error_type=f"http_{exc.code}",
                error_message=detail,
                response_format=self.config.response_format,
                max_tokens=self.config.max_tokens,
                temperature=self.config.temperature,
            )
            raise RuntimeError(f"{self.config.provider} HTTP {exc.code}: {detail[:1000]}") from exc
        try:
            payload = json.loads(body)
            choices = payload.get("choices") or []
            if not choices:
                raise RuntimeError(f"{self.config.provider} response had no choices")
            content = ((choices[0].get("message") or {}).get("content") or "").strip()
            if not content:
                raise RuntimeError(f"{self.config.provider} response content was empty")
        except Exception as exc:
            write_api_call_metric(
                provider=self.config.provider,
                model=self.config.model,
                role=self.config.role,
                status="error",
                latency_ms=int((time.monotonic() - t0) * 1000),
                messages=messages,
                error_type=type(exc).__name__,
                error_message=str(exc),
                response_format=self.config.response_format,
                max_tokens=self.config.max_tokens,
                temperature=self.config.temperature,
            )
            raise
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        write_api_call_metric(
            provider=self.config.provider,
            model=self.config.model,
            role=self.config.role,
            status="success",
            latency_ms=int((time.monotonic() - t0) * 1000),
            messages=messages,
            response_text=content,
            usage=usage,
            response_format=self.config.response_format,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
        )
        return content

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        """ProviderAdapter-compatible wrapper around complete().

        Accepts a ProviderRequest and returns a ProviderResponse with
        latency_ms populated.  The client's existing config supplies
        provider name, model, and auth; request supplies message content.
        """
        messages = [
            {"role": "system", "content": request.system_prompt},
            {"role": "user", "content": request.user_prompt},
        ]
        t0 = time.monotonic()
        text = self.complete(messages)
        return ProviderResponse(
            text=text,
            provider_name=self.config.provider,
            model=self.config.model,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )


@dataclass
class OpenAICompatibleReaderClient:
    chat: OpenAICompatibleChatClient

    def create_work_order(self, task_markdown: str, schema_text: str, prompt_text: str) -> str:
        return self.chat.complete(
            [
                {"role": "system", "content": prompt_text},
                {
                    "role": "user",
                    "content": (
                        "Convert this task.md to work_order.json. Use the schema below.\n\n"
                        "## work_order.schema.json\n\n"
                        f"{schema_text}\n\n"
                        "## task.md\n\n"
                        f"{task_markdown}"
                    ),
                },
            ]
        )


@dataclass
class OpenAICompatibleMaterializerClient:
    chat: OpenAICompatibleChatClient

    def materialize(self, work_order_json: str, prompt_text: str, context_pack_text: str, schemas_text: str, feedback_text: str = "") -> str:
        user_content = (
            "Generate only the requested artifacts from this context pack.\n\n"
            "## work_order.json\n\n"
            f"{work_order_json}\n\n"
            "## context_pack\n\n"
            f"{context_pack_text}\n\n"
            "## schemas\n\n"
            f"{schemas_text}"
        )
        if feedback_text:
            user_content += f"\n\n## feedback_from_previous_attempt\n\n{feedback_text}"
        return self.chat.complete(
            [
                {"role": "system", "content": prompt_text},
                {"role": "user", "content": user_content},
            ]
        )


@dataclass
class OpenAICompatibleVerifierClient:
    chat: OpenAICompatibleChatClient

    def verify_work_order(self, task_markdown: str, work_order_json: str, prompt_text: str) -> str:
        return self.chat.complete(
            [
                {"role": "system", "content": prompt_text},
                {
                    "role": "user",
                    "content": (
                        "Review this work_order.json for completeness and safety.\n\n"
                        "## task.md\n\n"
                        f"{task_markdown}\n\n"
                        "## work_order.json\n\n"
                        f"{work_order_json}"
                    ),
                },
            ]
        )


@dataclass
class PolicyClients:
    reader: ReaderClient
    materializer: MaterializerClient
    backup_materializer: MaterializerClient | None = None
    reader_verifier: ReaderVerifierClient | None = None


def load_env_file(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(path)
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _role_config(payload: dict[str, Any], role: str) -> OpenAICompatibleConfig:
    item = payload[role]
    return OpenAICompatibleConfig(
        provider=str(item.get("provider") or "openai_compatible"),
        model=str(item["model"]),
        base_url=str(item["base_url"]),
        api_key_env=str(item.get("api_key_env") or "DASHSCOPE_API_KEY"),
        role=role,
        temperature=float(item.get("temperature", 0.1)),
        max_tokens=int(item.get("max_tokens", 4096)),
        timeout_seconds=int(item.get("timeout_seconds", 120)),
        response_format=item.get("response_format"),
    )


def clients_from_provider_policy(path: Path) -> tuple[ReaderClient, MaterializerClient]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    reader = OpenAICompatibleReaderClient(OpenAICompatibleChatClient(_role_config(payload, "primary_reader")))
    materializer = OpenAICompatibleMaterializerClient(OpenAICompatibleChatClient(_role_config(payload, "primary_materializer")))
    return reader, materializer


def load_policy_clients(path: Path) -> PolicyClients:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    reader = OpenAICompatibleReaderClient(OpenAICompatibleChatClient(_role_config(payload, "primary_reader")))
    materializer = OpenAICompatibleMaterializerClient(OpenAICompatibleChatClient(_role_config(payload, "primary_materializer")))

    backup: MaterializerClient | None = None
    if "backup_materializer" in payload:
        backup = OpenAICompatibleMaterializerClient(OpenAICompatibleChatClient(_role_config(payload, "backup_materializer")))
    else:
        pm = payload.get("primary_materializer") or {}
        if pm.get("backup_model"):
            backup_cfg = OpenAICompatibleConfig(
                provider=str(pm.get("provider") or "openai_compatible"),
                model=str(pm["backup_model"]),
                base_url=str(pm["base_url"]),
                api_key_env=str(pm.get("api_key_env") or "DASHSCOPE_API_KEY"),
                role="backup_materializer",
                temperature=float(pm.get("temperature", 0.1)),
                max_tokens=int(pm.get("max_tokens", 4096)),
                timeout_seconds=int(pm.get("timeout_seconds", 120)),
            )
            backup = OpenAICompatibleMaterializerClient(OpenAICompatibleChatClient(backup_cfg))

    verifier: ReaderVerifierClient | None = None
    if "reader_verifier" in payload:
        verifier = OpenAICompatibleVerifierClient(OpenAICompatibleChatClient(_role_config(payload, "reader_verifier")))

    return PolicyClients(reader=reader, materializer=materializer, backup_materializer=backup, reader_verifier=verifier)
