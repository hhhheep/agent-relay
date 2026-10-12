from __future__ import annotations

from dataclasses import dataclass


V1_BASELINE_VERSION = "v1.0"
CONTRACT_SCHEMA_VERSION = 1
REMOTE_RUNTIME_VERSION = "materialized-runtime-v1"

ORCHESTRATOR_RESULT_STATUSES = frozenset({"success", "partial", "failed"})
EXECUTION_PORT_TERMINAL_STATUSES = frozenset(
    {
        "succeeded",
        "failed_launch",
        "failed_exit_code",
        "artifact_missing",
        "metrics_missing",
        "validation_failed",
        "failed",
    }
)


@dataclass(frozen=True)
class ContractInfo:
    name: str
    schema_version: int = CONTRACT_SCHEMA_VERSION
    baseline_version: str = V1_BASELINE_VERSION


CORE_CONTRACTS = {
    "task_front_matter": ContractInfo("task_front_matter"),
    "context_pack_manifest": ContractInfo("context_pack_manifest"),
    "work_order": ContractInfo("work_order"),
    "result_package": ContractInfo("result_package"),
    "execution_spec": ContractInfo("execution_spec"),
    "script_registry": ContractInfo("script_registry"),
    "execution_final_result": ContractInfo("execution_final_result"),
    "worker_result": ContractInfo("worker_result"),
    "registry": ContractInfo("registry"),
    "workplace_profile": ContractInfo("workplace_profile"),
    "remote_execution_env": ContractInfo("remote_execution_env"),
}


def contract_schema_version(name: str) -> int:
    info = CORE_CONTRACTS.get(name)
    return info.schema_version if info else CONTRACT_SCHEMA_VERSION


def is_orchestrator_result_status(value: object) -> bool:
    return str(value or "") in ORCHESTRATOR_RESULT_STATUSES


def is_execution_port_terminal_status(value: object) -> bool:
    return str(value or "") in EXECUTION_PORT_TERMINAL_STATUSES
