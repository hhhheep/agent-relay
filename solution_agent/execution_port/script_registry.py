"""Load script_registry.yaml and build argv from typed args."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import yaml

_DEFAULT_REGISTRY = Path(__file__).parent / "script_registry.yaml"


def load_registry(path: Path = _DEFAULT_REGISTRY) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def get_script_entry(registry: dict, script_id: str) -> dict:
    scripts = registry.get("scripts", {})
    entry = scripts.get(script_id, {}) if isinstance(scripts, dict) else {}
    return entry if isinstance(entry, dict) else {}


def get_expected_metrics(registry: dict, script_id: str) -> dict:
    entry = get_script_entry(registry, script_id)
    metrics = entry.get("expected_metrics", {})
    return metrics if isinstance(metrics, dict) else {}


def get_expected_outputs(registry: dict, script_id: str) -> dict:
    entry = get_script_entry(registry, script_id)
    outputs = entry.get("expected_outputs", {})
    return outputs if isinstance(outputs, dict) else {}


def get_closure_contract(registry: dict, script_id: str) -> dict:
    contract = registry.get("closure_contract", {})
    if not isinstance(contract, dict):
        contract = {}
    entry = get_script_entry(registry, script_id)
    script_contract = entry.get("closure_contract", {}) if isinstance(entry, dict) else {}
    merged = dict(contract)
    if isinstance(script_contract, dict):
        merged.update(script_contract)
    return merged


def get_numeric_policy(registry: dict, script_id: str) -> dict:
    entry = get_script_entry(registry, script_id)
    policy = entry.get("numeric_policy", {})
    return policy if isinstance(policy, dict) else {}


def get_required_approvals(registry: dict, script_id: str) -> list[str]:
    entry = get_script_entry(registry, script_id)
    approvals = entry.get("required_approvals", [])
    if not isinstance(approvals, list):
        return []
    return [str(item) for item in approvals if str(item).strip()]


def get_warning_rules(registry: dict, script_id: str) -> dict:
    entry = get_script_entry(registry, script_id)
    rules = entry.get("warning_rules", {})
    return rules if isinstance(rules, dict) else {}


def get_artifact_family(registry: dict, family: str) -> dict:
    families = registry.get("artifact_families", {})
    if not isinstance(families, dict):
        return {}
    entry = families.get(family, {})
    return entry if isinstance(entry, dict) else {}


def get_output_family_allowlist(registry: dict) -> dict[str, list[str]]:
    scripts = registry.get("scripts", {})
    if not isinstance(scripts, dict):
        return {}
    allowlist: dict[str, list[str]] = {}
    for script_id, entry in scripts.items():
        if not isinstance(entry, dict):
            continue
        families = entry.get("produces_artifact_families", [])
        if not isinstance(families, list):
            continue
        for item in families:
            if not isinstance(item, dict):
                continue
            prefix = str(item.get("prefix") or "").strip()
            if not prefix:
                continue
            scripts_for_prefix = allowlist.setdefault(prefix, [])
            if str(script_id) not in scripts_for_prefix:
                scripts_for_prefix.append(str(script_id))
    return allowlist


def build_argv(script_id: str, args: Dict[str, Any], registry: dict) -> List[str]:
    """
    Build the full argv list from script_id + args dict using registry type info.

    Rendering rules:
      str / int / float  → [--flag, value]
      bool True          → [--flag]
      bool False / None  → omit
      list               → [--flag, val1, --flag, val2, ...]
    """
    scripts = registry.get("scripts", {})
    if script_id not in scripts:
        raise KeyError(f"script_id {script_id!r} not in registry")

    reg = scripts[script_id]
    argv: List[str] = list(reg["entrypoint"])
    arg_defs: dict = reg.get("args", {})

    for key, value in args.items():
        if key not in arg_defs:
            raise KeyError(f"arg {key!r} not in allowed args for {script_id!r}")
        flag: str = arg_defs[key]["flag"]
        arg_type: str = arg_defs[key].get("type", "str")

        if value is None:
            continue

        if arg_type == "bool":
            if value is True or str(value).lower() in ("true", "1", "yes"):
                argv.append(flag)
            # False → omit
        elif isinstance(value, list):
            for item in value:
                argv.extend([flag, str(item)])
        else:
            argv.extend([flag, str(value)])

    return argv
