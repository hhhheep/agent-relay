from __future__ import annotations

from solution_agent.contracts import (
    CONTRACT_SCHEMA_VERSION,
    CORE_CONTRACTS,
    REMOTE_RUNTIME_VERSION,
    contract_schema_version,
    is_execution_port_terminal_status,
    is_orchestrator_result_status,
)


def test_v1_contract_versions_are_centralized():
    assert CONTRACT_SCHEMA_VERSION == 1
    assert REMOTE_RUNTIME_VERSION == "materialized-runtime-v1"
    assert contract_schema_version("worker_result") == 1
    assert "execution_spec" in CORE_CONTRACTS


def test_status_domains_remain_separate():
    assert is_orchestrator_result_status("success")
    assert is_orchestrator_result_status("partial")
    assert is_orchestrator_result_status("failed")
    assert not is_orchestrator_result_status("succeeded")

    assert is_execution_port_terminal_status("succeeded")
    assert is_execution_port_terminal_status("validation_failed")
