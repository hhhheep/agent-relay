"""Bounded, evidence-preserving harness primitives for solution-agent."""

from .ab_eval import HarnessObservation, SavingsEvaluation, evaluate_paired_savings
from .budget import evaluate_provider_budget, evaluate_structural_budget
from .context_budget import ContextItem, build_bounded_context
from .contracts import BudgetDecision, ProviderUsage, TokenBudget
from .failure_fingerprint import failure_fingerprint
from .receipt_adapter import verify_token_guard_receipt
from .reader_ab import ReaderManagerABObservation, decision_outcome_fingerprint, evaluate_reader_manager_savings
from .usage import normalize_provider_usage
from .manifests import build_tool_manifest, canonical_json_bytes
from .model_dispatch_guard import (
    ModelDispatchDenied,
    ModelDispatchPermit,
    admit_model_dispatch,
    complete_model_dispatch,
    model_request_fingerprint,
)
from .telemetry_contracts import (
    ContextManifest,
    ContextManifestItem,
    PhaseBudget,
    PortfolioBudget,
    QuotaWindowObservation,
    StructuralBudgetDecision,
    StructuralUsage,
    ToolManifest,
    UsageEvent,
)
from .usage_ledger import (
    UsageLedger,
    UsageLedgerError,
    provider_usage_event,
    summarize_usage_events,
    token_guard_receipt_event,
)

__all__ = [
    "HarnessObservation",
    "SavingsEvaluation",
    "evaluate_paired_savings",
    "BudgetDecision",
    "ContextItem",
    "ProviderUsage",
    "ReaderManagerABObservation",
    "TokenBudget",
    "build_bounded_context",
    "evaluate_provider_budget",
    "evaluate_structural_budget",
    "evaluate_reader_manager_savings",
    "decision_outcome_fingerprint",
    "failure_fingerprint",
    "normalize_provider_usage",
    "verify_token_guard_receipt",
    "build_tool_manifest",
    "canonical_json_bytes",
    "ModelDispatchDenied",
    "ModelDispatchPermit",
    "admit_model_dispatch",
    "complete_model_dispatch",
    "model_request_fingerprint",
    "ContextManifest",
    "ContextManifestItem",
    "PhaseBudget",
    "PortfolioBudget",
    "QuotaWindowObservation",
    "StructuralBudgetDecision",
    "StructuralUsage",
    "ToolManifest",
    "UsageEvent",
    "UsageLedger",
    "UsageLedgerError",
    "provider_usage_event",
    "summarize_usage_events",
    "token_guard_receipt_event",
]
