"""Harness 0.6 two-ended identity and activation gate."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "0.6"


class ProtocolMismatch(RuntimeError):
    code = "HARNESS_PROTOCOL_MISMATCH"


@dataclass(frozen=True)
class EndpointIdentity:
    protocol_version: str
    release_id: str
    payload_sha256: str
    mcp_schema_sha256: str
    workplace_id: bytes | None = None

    def compact(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "release_id": self.release_id,
            "payload_sha256": self.payload_sha256,
            "mcp_schema_sha256": self.mcp_schema_sha256,
        }


@dataclass(frozen=True)
class Activation:
    generation: int
    local: EndpointIdentity
    remote: EndpointIdentity


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def identity_from_manifest(manifest: dict[str, Any], *, payload_sha256: str | None = None,
                           workplace_id: bytes | None = None) -> EndpointIdentity:
    protocol = str(manifest.get("protocol_version") or "")
    release = str(manifest.get("release_id") or "")
    payload = payload_sha256 or str(manifest.get("payload_sha256") or manifest.get("source_root_sha256") or "")
    schema = str(manifest.get("mcp_schema_sha256") or manifest.get("tool_contract_sha256") or "")
    if not protocol or not release or not payload or not schema:
        raise ProtocolMismatch("manifest is missing protocol, release, payload, or tool contract identity")
    return EndpointIdentity(protocol, release, payload, schema, workplace_id)


def load_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ProtocolMismatch(f"manifest is not an object: {path}")
    return value


def verify_pair(local: EndpointIdentity, remote: EndpointIdentity) -> None:
    if local.workplace_id is not None and remote.workplace_id is not None and local.workplace_id != remote.workplace_id:
        raise ProtocolMismatch("workplace.id does not match")
    for field in ("protocol_version", "release_id", "payload_sha256", "mcp_schema_sha256"):
        if getattr(local, field) != getattr(remote, field):
            raise ProtocolMismatch(f"endpoint {field} mismatch")
    if local.protocol_version != PROTOCOL_VERSION:
        raise ProtocolMismatch(f"unsupported protocol version: {local.protocol_version}")


class ActivationGate:
    def __init__(self) -> None:
        self._activation: Activation | None = None

    @property
    def active(self) -> bool:
        return self._activation is not None

    def activate(self, local: EndpointIdentity, remote: EndpointIdentity, generation: int) -> Activation:
        verify_pair(local, remote)
        self._activation = Activation(generation, local, remote)
        return self._activation

    def invalidate(self) -> None:
        self._activation = None

    def require(self, generation: int | None = None) -> Activation:
        if self._activation is None:
            raise ProtocolMismatch("Harness activation gate is closed")
        if generation is not None and generation != self._activation.generation:
            raise ProtocolMismatch("stale activation generation")
        return self._activation
