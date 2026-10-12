from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List

import yaml

from .paths import (
    continuation_remote_execution_work_orders_dir,
    solution_continuation_dir,
)
from .manual_dispatch import assess_manual_dispatch_admission


FORBIDDEN_PATH_PATTERNS = [
    re.compile(r"^/ssd[^/]*", re.IGNORECASE),
    re.compile(r"^/mnt[^/]*", re.IGNORECASE),
    re.compile(r"^/raid[^/]*", re.IGNORECASE),
    re.compile(r"^[A-Za-z]:\\\\", re.IGNORECASE),
    re.compile(r"^\\\\\\\\", re.IGNORECASE),
]

ALLOWED_RESOURCE_HINTS = {"cpu", "gpu", "any"}


def _policy_root() -> Path:
    configured = solution_continuation_dir()
    if configured.exists():
        return configured
    fallback = Path(__file__).resolve().parents[2] / ".solution" / "continuation"
    if fallback.exists():
        return fallback
    return configured


def _load_yaml(path: Path) -> Dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return payload


def _load_policy() -> Dict[str, Any]:
    return _load_yaml(_policy_root() / "remote_execution_policy.yaml")


def _load_checklist() -> Dict[str, Any]:
    return _load_yaml(_policy_root() / "remote_execution_preflight_checklist.yaml")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256_hex(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _to_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return None
    normalized = _normalize(value)
    if not normalized:
        return None
    try:
        return float(normalized)
    except ValueError:
        return None


def _normalize_scope(raw: Dict[str, Any] | None) -> Dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(key).strip(): _normalize(value) for key, value in raw.items()}


def _load_remote_execution_profiles() -> Dict[str, Any]:
    repo_root = solution_continuation_dir().parent.parent
    profile_path = repo_root / "config" / "remote_execution_profiles.yaml"
    if not profile_path.exists():
        return {}
    payload = _load_yaml(profile_path)
    profiles = payload.get("profiles")
    if isinstance(profiles, dict):
        return {str(name): value for name, value in profiles.items() if isinstance(value, dict)}
    return {}


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _add_check(checks: List[Dict[str, Any]], check_id: str, status: str, **payload: Any) -> Dict[str, Any]:
    entry = {"id": check_id, "status": status}
    entry.update(payload)
    checks.append(entry)
    return entry


def _load_json(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("dispatch request JSON must contain an object")
    return payload


def _parse_front_matter(text: str) -> Dict[str, Any]:
    if not text.startswith("---\n"):
        return {}
    parts = text.split("---", 2)
    if len(parts) != 3:
        return {}
    try:
        payload = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _append_action_request(action_requests: List[Dict[str, Any]], action: str, **payload: Any) -> None:
    action_item = {"type": action}
    action_item.update(payload)
    action_requests.append(action_item)


def _present_true_value(value: Any) -> bool:
    if isinstance(value, dict):
        present = value.get("present")
        if present is None:
            return True
        return bool(present)
    return bool(value)


def _extract_requirement_items(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    items: List[Dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            items.append({"id": item, "required": True, "present": True})
        elif isinstance(item, dict):
            item_id = _normalize(item.get("id") or item.get("name") or item.get("artifact") or item.get("capability"))
            if not item_id:
                item_id = "unknown"
            required = bool(item.get("required", True))
            present = _present_true_value(item)
            items.append({"id": item_id, "required": required, "present": present})
    return items


def _collect_resource_guard(raw: Dict[str, Any]) -> Dict[str, Any]:
    resource_hint = _normalize(raw.get("resource_hint") or raw.get("resource_lane")).lower()
    if not resource_hint:
        resource_hint = _normalize(raw.get("task_metadata", {}).get("resource_hint"))
    if not resource_hint:
        resource_hint = _normalize(raw.get("task_metadata", {}).get("resource_lane"))
    guard = {
        "resource_hint": resource_hint,
        "resource_lane": _normalize(raw.get("resource_lane") or raw.get("task_metadata", {}).get("resource_lane")),
        "required": raw.get("required_gpu_mem_gb") if raw.get("required_gpu_mem_gb") is not None else raw.get("task_metadata", {}).get("required_gpu_mem_gb"),
        "available": raw.get("available_gpu_mem_gb") if raw.get("available_gpu_mem_gb") is not None else raw.get("task_metadata", {}).get("available_gpu_mem_gb"),
    }
    if isinstance(guard["required"], str):
        try:
            guard["required"] = float(guard["required"])
        except ValueError:
            guard["required"] = None
    if isinstance(guard["available"], str):
        try:
            guard["available"] = float(guard["available"])
        except ValueError:
            guard["available"] = None
    return guard


def _load_profile_by_name(profile_name: str, explicit: Dict[str, Any] | None = None) -> Dict[str, Any]:
    if not profile_name:
        return {}

    merged: Dict[str, Any] = {}
    if isinstance(explicit, dict):
        merged = dict(explicit)

    if _normalize(merged.get("profile")) != profile_name:
        merged["profile"] = profile_name

    loaded_profiles = _load_remote_execution_profiles()
    loaded = loaded_profiles.get(profile_name, {})
    if isinstance(loaded, dict):
        merged.setdefault("host", _normalize(loaded.get("host")))
        merged.setdefault("remote_workdir", _normalize(loaded.get("remote_workdir")))
        merged.setdefault("remote_root", _normalize(loaded.get("remote_root") or loaded.get("remote_output_root")))

    return {key: _normalize(value) for key, value in merged.items()}


def _path_within_path(child: str, parent: str) -> bool:
    child_norm = _normalize(child).rstrip("/")
    parent_norm = _normalize(parent).rstrip("/")
    if not child_norm or not parent_norm:
        return False
    if child_norm == parent_norm:
        return True
    return child_norm.startswith(parent_norm + "/")


def _guessed_path_is_profile_owned(path: str, profile_roots: List[str]) -> bool:
    normalized = _normalize(path)
    if not normalized:
        return False
    for root in profile_roots:
        if _path_within_path(normalized, root):
            return True
    return False


def _is_guessed_remote_path(path: str) -> bool:
    normalized = str(path or "")
    return any(expr.match(normalized) for expr in FORBIDDEN_PATH_PATTERNS)



def _assess_remote_execution_preflight_stub_v2(
    dispatch_request_path: Path,
    *,
    workplace_profile: dict | None = None,
    actor: str,
) -> dict:
    path = Path(dispatch_request_path)
    policy = _load_policy()
    checklist = _load_checklist()
    checks = []
    action_requests = []
    hard_failures = []
    hold_reasons = []
    manual_reasons = []
    reason_codes = []

    side_effects = {
        'ssh': False,
        'remote_write': False,
        'scheduler_dispatch': False,
        'registry_mutation': False,
        'running_state': False,
        'subprocess': False,
    }

    normalized_actor = _normalize(actor)
    _add_check(checks, 'actor_present', 'passed' if normalized_actor else 'failed', actor=normalized_actor)
    if not normalized_actor:
        hard_failures.append('actor_missing')
        reason_codes.append('actor_missing')

    if not path.exists():
        _add_check(checks, 'dispatch_request_exists', 'failed', path=str(path), reason='dispatch_request_missing')
        hard_failures.append('dispatch_request_missing')
        reason_codes.append('dispatch_request_missing')
        dispatch_request_hash = _sha256_text(str(path))
        return {
            'request_id': _normalize(path.stem),
            'dispatch_request_path': str(path),
            'dispatch_request_hash': dispatch_request_hash,
            'task_id': '',
            'actor': normalized_actor,
            'status': 'blocked',
            'reasons': sorted(set(reason_codes)),
            'task_metadata': {
                'declared_worker': '',
                'required_worker': '',
                'line': '',
                'batch': '',
                'dag_root': '',
                'prefix': '',
                'resource_hint': '',
                'resource_lane': '',
            },
            'workplace_metadata': {
                'profile': '',
                'host': '',
                'remote_workdir': '',
                'remote_root': '',
            },
            'checks': checks,
            'action_requests': action_requests,
            'side_effects': side_effects,
            'checklist': checklist.get('name', 'continuation_remote_execution_preflight_p3'),
            'policy_version': str(policy.get('policy_version', '0.1.0')),
        }

    _add_check(checks, 'dispatch_request_exists', 'passed', path=str(path))
    try:
        request = _load_json(path)
    except Exception:
        _add_check(checks, 'dispatch_request_parseable', 'failed', path=str(path), reason='dispatch_request_parse_error')
        return {
            'request_id': _normalize(path.stem),
            'dispatch_request_path': str(path),
            'dispatch_request_hash': _sha256_text(str(path)),
            'task_id': '',
            'actor': normalized_actor,
            'status': 'blocked',
            'reasons': ['dispatch_request_parse_error'],
            'task_metadata': {
                'declared_worker': '',
                'required_worker': '',
                'line': '',
                'batch': '',
                'dag_root': '',
                'prefix': '',
                'resource_hint': '',
                'resource_lane': '',
            },
            'workplace_metadata': {
                'profile': '',
                'host': '',
                'remote_workdir': '',
                'remote_root': '',
            },
            'checks': checks,
            'action_requests': action_requests,
            'side_effects': side_effects,
            'checklist': checklist.get('name', 'continuation_remote_execution_preflight_p3'),
            'policy_version': str(policy.get('policy_version', '0.1.0')),
        }

    dispatch_request_hash = _sha256_text(_canonical_json(request))
    request_id = _normalize(request.get('request_id'))
    _add_check(checks, 'dispatch_request_id_present', 'passed' if request_id else 'failed', reason='request_id_missing' if not request_id else None)
    if not request_id:
        hard_failures.append('request_id_missing')
        reason_codes.append('request_id_missing')

    task_id = _normalize(request.get('task_id'))
    _add_check(checks, 'task_id_present', 'passed' if task_id else 'failed', reason='task_id_missing' if not task_id else None)
    if not task_id:
        hard_failures.append('task_id_missing')
        reason_codes.append('task_id_missing')

    active_inbox_path = _normalize(request.get('active_inbox_path') or request.get('task_path'))
    if not active_inbox_path:
        _add_check(checks, 'active_inbox_task_exists', 'failed', reason='active_inbox_path_missing', path='')
        hard_failures.append('active_inbox_path_missing')
        reason_codes.append('active_inbox_path_missing')
    else:
        _add_check(checks, 'active_inbox_task_exists', 'passed', active_inbox_path=active_inbox_path)

    request_status = _normalize(request.get('status'))
    _add_check(
        checks,
        'dispatch_request_status_valid',
        'passed' if request_status == 'request_created' else 'failed',
        request_status=request_status,
        allowed=['request_created'],
    )
    if request_status != 'request_created':
        hard_failures.append('dispatch_request_status_invalid')
        reason_codes.append('dispatch_request_status_invalid')

    request_hash = _normalize(request.get('admission_report_hash'))
    _add_check(checks, 'admission_hash_present', 'passed' if request_hash else 'failed', reason='admission_report_hash_missing' if not request_hash else None)
    if not request_hash:
        hard_failures.append('admission_report_hash_missing')
        reason_codes.append('admission_report_hash_missing')

    task_front = {}
    task_path = Path(active_inbox_path) if active_inbox_path else Path()
    if active_inbox_path and task_path.exists():
        task_content = task_path.read_text(encoding='utf-8')
        task_front = _parse_front_matter(task_content)
        _add_check(checks, 'active_inbox_task_front_matter', 'passed' if task_front else 'failed')
        if not task_front:
            hard_failures.append('active_inbox_task_front_matter_missing')
            reason_codes.append('active_inbox_task_front_matter_missing')
    else:
        if active_inbox_path:
            _add_check(checks, 'active_inbox_task_front_matter', 'failed', path=active_inbox_path)
            hard_failures.append('active_task_missing')
            reason_codes.append('active_task_missing')

    task_metadata = request.get('task_metadata')
    if not isinstance(task_metadata, dict):
        task_metadata = {}
    for field in ('declared_worker', 'required_worker', 'line', 'batch', 'dag_root', 'prefix', 'resource_hint', 'resource_lane'):
        task_metadata.setdefault(field, _normalize(task_front.get(field)))

    if not task_metadata.get('declared_worker'):
        task_metadata['declared_worker'] = _normalize(task_front.get('worker'))
    if not task_metadata.get('required_worker'):
        task_metadata['required_worker'] = task_metadata.get('declared_worker')

    declared_worker = _normalize(task_metadata.get('declared_worker'))
    required_worker = _normalize(task_metadata.get('required_worker'))
    requested_worker = _normalize(
        request.get('requested_worker') if isinstance(request.get('requested_worker'), str) else (request.get('scope') or {}).get('worker')
    )

    if declared_worker and required_worker and declared_worker == required_worker:
        _add_check(checks, 'declared_worker_matches_required_worker', 'passed', declared_worker=declared_worker, required_worker=required_worker)
    else:
        hard_failures.append('worker_mismatch')
        reason_codes.append('worker_mismatch')
        _add_check(checks, 'declared_worker_matches_required_worker', 'failed', declared_worker=declared_worker, required_worker=required_worker)

    if requested_worker and requested_worker not in {declared_worker, required_worker}:
        hard_failures.append('worker_mismatch')
        reason_codes.append('worker_mismatch')
        _add_check(checks, 'requested_worker_filter', 'failed', requested_worker=requested_worker, declared_worker=declared_worker, required_worker=required_worker)
    else:
        _add_check(checks, 'requested_worker_filter', 'passed', requested_worker=requested_worker, declared_worker=declared_worker, required_worker=required_worker)

    requested_scope_raw = request.get('scope')
    requested_scope = _normalize_scope(requested_scope_raw) if isinstance(requested_scope_raw, dict) else {}
    scope_fields = ('line', 'batch', 'dag_root', 'prefix')
    scope_violations = []
    for field in scope_fields:
        requested_value = _normalize(requested_scope.get(field))
        if not requested_value:
            requested_value = _normalize(task_metadata.get(field))
        actual_value = _normalize(task_front.get(field))
        if requested_value and actual_value and requested_value != actual_value:
            scope_violations.append(f'{field}:{actual_value}->{requested_value}')

    if not scope_violations:
        _add_check(checks, 'scope_match', 'passed')
    else:
        hard_failures.append('scope_mismatch')
        reason_codes.append('scope_mismatch')
        _add_check(checks, 'scope_match', 'failed', violations=scope_violations)

    request_profile = _normalize(request.get('workplace_metadata', {}).get('profile') if isinstance(request.get('workplace_metadata'), dict) else request.get('profile'))
    explicit_profile = _normalize(workplace_profile.get('profile') if isinstance(workplace_profile, dict) else '')
    profile_name = explicit_profile or request_profile
    if not profile_name:
        hard_failures.append('workplace_profile_missing')
        reason_codes.append('workplace_profile_missing')
        _append_action_request(action_requests, 'missing_workplace_profile')
        _add_check(checks, 'workplace_profile_present', 'failed', reason='workplace_profile_missing')
    else:
        _add_check(checks, 'workplace_profile_present', 'passed', profile=profile_name)

    profile_data = _load_profile_by_name(profile_name, workplace_profile)
    workplace_metadata = request.get('workplace_metadata')
    if not isinstance(workplace_metadata, dict):
        workplace_metadata = {}
    workplace_metadata.setdefault('profile', profile_name)
    for key in ('host', 'remote_workdir', 'remote_root'):
        if key not in workplace_metadata:
            workplace_metadata[key] = _normalize(request.get(key))
        if not workplace_metadata[key]:
            workplace_metadata[key] = _normalize(profile_data.get(key))
        workplace_metadata[key] = _normalize(workplace_metadata[key])

    host = workplace_metadata.get('host', '')
    remote_workdir = workplace_metadata.get('remote_workdir', '')
    remote_root = workplace_metadata.get('remote_root', '')

    if host:
        _add_check(checks, 'host_present', 'passed', host=host)
    else:
        hard_failures.append('missing_host')
        reason_codes.append('missing_host')
        _append_action_request(action_requests, 'missing_host')
        _add_check(checks, 'host_present', 'failed', missing='host')

    if remote_root:
        _add_check(checks, 'remote_root_present', 'passed', remote_root=remote_root)
    else:
        hard_failures.append('missing_remote_root')
        reason_codes.append('missing_remote_root')
        _append_action_request(action_requests, 'missing_remote_root')
        _add_check(checks, 'remote_root_present', 'failed', missing='remote_root')

    if remote_workdir:
        _add_check(checks, 'remote_workdir_present', 'passed', remote_workdir=remote_workdir)
    else:
        hard_failures.append('missing_remote_workdir')
        reason_codes.append('missing_remote_workdir')
        _append_action_request(action_requests, 'missing_remote_workdir')
        _add_check(checks, 'remote_workdir_present', 'failed', missing='remote_workdir')

    profile_roots = []
    for key in ('remote_root', 'remote_workdir', 'remote_output_root'):
        value = _normalize(profile_data.get(key))
        if value:
            profile_roots.append(value)

    profile_owns_remote_root = bool(profile_roots) and _guessed_path_is_profile_owned(remote_root, profile_roots)
    profile_owns_remote_workdir = bool(profile_roots) and _guessed_path_is_profile_owned(remote_workdir, profile_roots)
    cross_profile = False
    if remote_root and profile_roots and not profile_owns_remote_root:
        cross_profile = True

    if remote_root and remote_workdir:
        if _is_guessed_remote_path(remote_root) or _is_guessed_remote_path(remote_workdir):
            if not (profile_owns_remote_root or profile_owns_remote_workdir):
                hard_failures.append('remote_path_guess')
                reason_codes.append('remote_path_guess')
                _add_check(checks, 'remote_paths_not_remote_path_guessing', 'failed', remote_root=remote_root, remote_workdir=remote_workdir)
            else:
                _add_check(checks, 'remote_paths_not_remote_path_guessing', 'passed', remote_root=remote_root, remote_workdir=remote_workdir)
        else:
            _add_check(checks, 'remote_paths_not_remote_path_guessing', 'passed', remote_root=remote_root, remote_workdir=remote_workdir)

        if cross_profile:
            hard_failures.append('remote_paths_not_from_profile')
            reason_codes.append('remote_paths_not_from_profile')
            _add_check(checks, 'remote_paths_from_profile_only', 'failed', remote_root=remote_root, remote_workdir=remote_workdir)
        elif remote_workdir == remote_root or _path_within_path(remote_workdir, remote_root):
            _add_check(checks, 'remote_paths_from_profile_only', 'passed', remote_root=remote_root, remote_workdir=remote_workdir)
        else:
            hard_failures.append('remote_paths_not_from_profile')
            reason_codes.append('remote_paths_not_from_profile')
            _add_check(checks, 'remote_paths_from_profile_only', 'failed', remote_root=remote_root, remote_workdir=remote_workdir)
    else:
        _add_check(checks, 'remote_paths_not_remote_path_guessing', 'failed', reason='missing_remote_path')
        _add_check(checks, 'remote_paths_from_profile_only', 'failed', reason='missing_remote_path')

    missing_artifacts = [
        item
        for item in _extract_requirement_items(request.get('required_artifacts'))
        if item.get('required', True) and not bool(item.get('present'))
    ]
    if missing_artifacts:
        for item in missing_artifacts:
            _append_action_request(action_requests, 'missing_artifact', id=item.get('id'))
        hold_reasons.append('missing_artifact')
        reason_codes.append('missing_artifact')
        _add_check(
            checks,
            'required_artifacts_present_or_action_request',
            'failed',
            count=len(missing_artifacts),
            missing=[item.get('id') for item in missing_artifacts],
        )
    else:
        _add_check(checks, 'required_artifacts_present_or_action_request', 'passed')

    missing_capabilities = [
        item
        for item in _extract_requirement_items(request.get('required_capabilities'))
        if item.get('required', True) and not bool(item.get('present'))
    ]
    if missing_capabilities:
        for item in missing_capabilities:
            _append_action_request(action_requests, 'missing_capability', id=item.get('id'))
        manual_reasons.append('missing_capability')
        reason_codes.append('missing_capability')
        _add_check(
            checks,
            'required_capability_present_or_action_request',
            'failed',
            count=len(missing_capabilities),
            missing=[item.get('id') for item in missing_capabilities],
        )
    else:
        _add_check(checks, 'required_capability_present_or_action_request', 'passed')

    resource_guard = _collect_resource_guard(request)
    resource_hint = _normalize(resource_guard.get('resource_hint') or task_metadata.get('resource_hint') or 'cpu')

    if resource_hint not in ALLOWED_RESOURCE_HINTS:
        manual_reasons.append('unsupported_resource_hint')
        reason_codes.append('unsupported_resource_hint')
        _add_check(checks, 'resource_hint_supported', 'failed', resource_hint=resource_hint)
    else:
        _add_check(checks, 'resource_hint_supported', 'passed', resource_hint=resource_hint)

    if resource_hint == 'gpu':
        required_mem = _to_float(resource_guard.get('required'))
        available_mem = _to_float(resource_guard.get('available'))
        if required_mem is None:
            hold_reasons.append('gpu_memory_guard_declared_when_gpu_present')
            reason_codes.append('gpu_memory_guard_declared_when_gpu_present')
            _add_check(checks, 'gpu_memory_guard_declared_when_gpu_present', 'failed', reason='resource_guard_missing')
        elif available_mem is None:
            hold_reasons.append('resource_unavailable')
            reason_codes.append('resource_unavailable')
            _add_check(checks, 'gpu_memory_guard_declared_when_gpu_present', 'failed', reason='resource_availability_unknown')
        elif available_mem < required_mem:
            hold_reasons.append('resource_unavailable')
            reason_codes.append('resource_unavailable')
            _add_check(checks, 'gpu_memory_guard_declared_when_gpu_present', 'failed', required=required_mem, available=available_mem)
        else:
            _add_check(checks, 'gpu_memory_guard_declared_when_gpu_present', 'passed', required=required_mem, available=available_mem)
    else:
        _add_check(checks, 'gpu_memory_guard_declared_when_gpu_present', 'passed', resource_hint=resource_hint)

    if active_inbox_path and task_path.exists() and request_hash:
        admission_report = assess_manual_dispatch_admission(
            task_path,
            actor=normalized_actor,
            requested_worker=requested_worker,
            scope=requested_scope,
        )
        fresh_report_hash = _sha256_hex(admission_report)
        if fresh_report_hash != request_hash:
            hard_failures.append('admission_report_hash_mismatch')
            reason_codes.append('admission_report_hash_mismatch')
            _add_check(checks, 'admission_hash_fresh', 'failed', request_hash=request_hash, fresh_hash=fresh_report_hash)
        else:
            _add_check(checks, 'admission_hash_fresh', 'passed', request_hash=request_hash)
        if _normalize(admission_report.get('status')) != 'admissible':
            hard_failures.append('admission_stale')
            reason_codes.append('admission_stale')
            _add_check(checks, 'admission_hash_fresh', 'failed', reason='admission_stale')
    else:
        _add_check(checks, 'admission_hash_fresh', 'failed', reason='admission_report_hash_missing' if not request_hash else 'active_inbox_missing')

    _add_check(checks, 'side_effects_all_false', 'passed', **side_effects)

    if hard_failures:
        status = 'blocked'
    elif manual_reasons:
        status = 'manual_review'
    elif hold_reasons:
        status = 'hold'
    else:
        status = 'pass'

    return {
        'request_id': request_id or _normalize(path.stem),
        'dispatch_request_path': str(path),
        'dispatch_request_hash': dispatch_request_hash,
        'task_id': task_id,
        'actor': normalized_actor,
        'status': status,
        'reasons': sorted(set(reason_codes)),
        'task_metadata': {
            'declared_worker': _normalize(task_metadata.get('declared_worker')),
            'required_worker': _normalize(task_metadata.get('required_worker')),
            'line': _normalize(task_metadata.get('line')),
            'batch': _normalize(task_metadata.get('batch')),
            'dag_root': _normalize(task_metadata.get('dag_root')),
            'prefix': _normalize(task_metadata.get('prefix')),
            'resource_hint': _normalize(resource_hint),
            'resource_lane': _normalize(task_metadata.get('resource_lane')),
        },
        'workplace_metadata': {
            'profile': profile_name,
            'host': host,
            'remote_workdir': remote_workdir,
            'remote_root': remote_root,
        },
        'checks': checks,
        'action_requests': action_requests,
        'side_effects': side_effects,
        'checklist': checklist.get('name', 'continuation_remote_execution_preflight_p3'),
        'policy_version': str(policy.get('policy_version', '0.1.0')),
    }


def _load_preflight_report(preflight_report_path_or_dict) -> Dict[str, Any]:
    if isinstance(preflight_report_path_or_dict, dict):
        return dict(preflight_report_path_or_dict)

    path = Path(preflight_report_path_or_dict)
    if not path.exists():
        raise FileNotFoundError(f"preflight report not found: {path}")
    payload = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError(f"preflight report must be a JSON object: {path}")
    return payload


def _read_dispatch_request_payload(preflight: Dict[str, Any]) -> Dict[str, Any]:
    dispatch_request_path = Path(_normalize(preflight.get('dispatch_request_path')))
    if not dispatch_request_path.exists():
        raise FileNotFoundError(f"dispatch request path missing: {dispatch_request_path}")
    payload = _load_json(dispatch_request_path)
    if not isinstance(payload, dict):
        raise ValueError(f"dispatch request must be a JSON object: {dispatch_request_path}")
    return payload


def _required_items_for_work_order(raw: Any) -> List[Dict[str, Any]]:
    return [
        {
            "id": _normalize(item.get("id") or item.get("artifact") or item.get("capability")),
            "required": bool(item.get("required", True)),
            "present": bool(item.get("present", True)),
        }
        for item in _extract_requirement_items(raw)
    ]


def _expected_outputs_from_items(items: List[Dict[str, Any]]) -> List[str]:
    outputs: List[str] = []
    for item in items:
        item_id = _normalize(item.get("id"))
        if not item_id:
            continue
        required = bool(item.get("required", True))
        present = bool(item.get("present", True))
        if required or present:
            outputs.append(item_id)
    return outputs


def _resolve_materialized_execution_requirement(preflight: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    task_metadata = preflight.get("task_metadata", {})
    if not isinstance(task_metadata, dict):
        reasons.append("task_metadata_missing")
        return reasons
    if _normalize(task_metadata.get("declared_worker")) != "materialized_execution":
        reasons.append("materialized_execution_worker_required")
    return reasons


def _collect_remote_execution_checks(preflight: Dict[str, Any], dispatch_request: Dict[str, Any]) -> List[str]:
    reasons: List[str] = []
    action_requests = preflight.get("action_requests", [])
    if not isinstance(action_requests, list):
        reasons.append("action_requests_invalid")
        return reasons

    action_types = {_normalize(item.get("type")) for item in action_requests if isinstance(item, dict)}
    if action_types.intersection({"missing_artifact", "missing_capability", "missing_workplace_profile", "missing_host", "missing_remote_root", "missing_remote_workdir"}):
        reasons.append("preflight_blocked_action_request_present")

    preflight_reasons = {_normalize(item) for item in preflight.get("reasons", [])}
    if "remote_path_guess" in preflight_reasons:
        reasons.append("remote_path_guess")
    if "remote_paths_not_from_profile" in preflight_reasons:
        reasons.append("remote_path_not_from_profile")
    if "resource_unavailable" in preflight_reasons:
        reasons.append("resource_unavailable")
    if "unsupported_resource_hint" in preflight_reasons:
        reasons.append("unsupported_resource_hint")
    if "worker_mismatch" in preflight_reasons:
        reasons.append("worker_mismatch")
    if "scope_mismatch" in preflight_reasons:
        reasons.append("scope_mismatch")
    if "workplace_profile_missing" in preflight_reasons:
        reasons.append("workplace_profile_missing")

    status = _normalize(preflight.get("status"))
    if status not in {"pass"}:
        reasons.append("preflight_status_not_pass")
    return reasons


def _normalize_workplace_metadata(preflight: Dict[str, Any]) -> Dict[str, str]:
    workplace_metadata = preflight.get("workplace_metadata", {})
    if not isinstance(workplace_metadata, dict):
        workplace_metadata = {}
    return {
        "profile": _normalize(workplace_metadata.get("profile")),
        "host": _normalize(workplace_metadata.get("host")),
        "remote_workdir": _normalize(workplace_metadata.get("remote_workdir")),
        "remote_root": _normalize(workplace_metadata.get("remote_root")),
    }


def _normalize_work_order_scope(preflight: Dict[str, Any]) -> Dict[str, str]:
    task_metadata = preflight.get("task_metadata", {})
    if not isinstance(task_metadata, dict):
        task_metadata = {}
    return {
        "line": _normalize(task_metadata.get("line")),
        "batch": _normalize(task_metadata.get("batch")),
        "dag_root": _normalize(task_metadata.get("dag_root")),
        "prefix": _normalize(task_metadata.get("prefix")),
    }


def _compute_work_order_idempotency_key(
    *,
    dispatch_request_id: str,
    dispatch_request_hash: str,
    preflight_report_hash: str,
    actor: str,
    worker: str,
    resource_hint: str,
    resource_lane: str,
    workplace_profile: str,
    remote_root: str,
    remote_workdir: str,
    required_artifacts: List[Dict[str, Any]],
    capability_checks: List[Dict[str, Any]],
) -> str:
    payload = {
        "dispatch_request_id": dispatch_request_id,
        "dispatch_request_hash": dispatch_request_hash,
        "preflight_report_hash": preflight_report_hash,
        "actor": actor,
        "worker": worker,
        "resource_hint": resource_hint,
        "resource_lane": resource_lane,
        "workplace_profile": workplace_profile,
        "remote_root": remote_root,
        "remote_workdir": remote_workdir,
        "required_artifacts": required_artifacts,
        "capability_checks": capability_checks,
    }
    return f"wo_{_sha256_hex(payload)[:20]}"


def _failure_work_order(
    *,
    preflight: Dict[str, Any],
    work_order_path: Path,
    reasons: List[str],
    checks: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "work_order_id": _normalize(preflight.get("request_id") or preflight.get("dispatch_request_id")),
        "idempotency_key": _normalize(preflight.get("idempotency_key")),
        "dispatch_request_id": _normalize(preflight.get("request_id")),
        "preflight_report_hash": _sha256_hex(preflight),
        "admission_report_hash": _normalize(preflight.get("dispatch_request_hash")),
        "task_id": _normalize(preflight.get("task_id")),
        "actor": _normalize(preflight.get("actor")),
        "status": "blocked",
        "reasons": sorted(set(_normalize(item) for item in reasons)),
        "side_effects": {
            "ssh": False,
            "remote_write": False,
            "scheduler_dispatch": False,
            "registry_mutation": False,
            "running_state": False,
            "subprocess": False,
            "remote_execution": False,
        },
        "checks": checks or [],
        "action_requests": [],
        "work_order_path": str(work_order_path),
        "created": False,
        "replayed": False,
    }


def _build_work_order_payload(
    *,
    preflight: Dict[str, Any],
    preflight_report_path: Path | None,
    dispatch_request: Dict[str, Any],
    check_reasons: List[str],
) -> Dict[str, Any]:
    dispatch_request_id = _normalize(preflight.get("request_id") or dispatch_request.get("request_id"))
    actor = _normalize(preflight.get("actor"))
    task_id = _normalize(preflight.get("task_id") or dispatch_request.get("task_id"))
    workplace = _normalize_workplace_metadata(preflight)
    scope = _normalize_work_order_scope(preflight)
    task_metadata = preflight.get("task_metadata", {})
    if not isinstance(task_metadata, dict):
        task_metadata = {}

    resource_hint = _normalize(task_metadata.get("resource_hint") or dispatch_request.get("resource_hint", "cpu"))
    resource_lane = _normalize(task_metadata.get("resource_lane") or dispatch_request.get("resource_lane", ""))
    remote_root = workplace["remote_root"]
    remote_workdir = workplace["remote_workdir"]

    required_artifacts = _required_items_for_work_order(dispatch_request.get("required_artifacts"))
    capability_checks = _required_items_for_work_order(dispatch_request.get("required_capabilities"))
    expected_outputs = _expected_outputs_from_items(required_artifacts)
    preflight_report_hash = _sha256_hex(preflight)
    dispatch_request_hash = _normalize(preflight.get("dispatch_request_hash") or _sha256_hex(dispatch_request))
    admission_report_hash = _normalize(dispatch_request.get("admission_report_hash"))
    dispatch_request_path = _normalize(preflight.get("dispatch_request_path"))
    workplace_profile = workplace["profile"]
    declared_worker = _normalize(task_metadata.get("declared_worker"))
    required_worker = _normalize(task_metadata.get("required_worker"))
    line = scope["line"]
    batch = scope["batch"]
    dag_root = scope["dag_root"]
    prefix = scope["prefix"]
    active_inbox_path = _normalize(preflight.get("active_inbox_path") or dispatch_request.get("active_inbox_path"))

    idempotency_key = _compute_work_order_idempotency_key(
        dispatch_request_id=dispatch_request_id,
        dispatch_request_hash=dispatch_request_hash,
        preflight_report_hash=preflight_report_hash,
        actor=actor,
        worker=declared_worker,
        resource_hint=resource_hint,
        resource_lane=resource_lane,
        workplace_profile=workplace_profile,
        remote_root=remote_root,
        remote_workdir=remote_workdir,
        required_artifacts=required_artifacts,
        capability_checks=capability_checks,
    )

    side_effects = {
        "ssh": False,
        "remote_write": False,
        "scheduler_dispatch": False,
        "registry_mutation": False,
        "running_state": False,
        "subprocess": False,
        "remote_execution": False,
    }
    output = {
        "schema_version": 1,
        "work_order_id": f"rewo_{dispatch_request_id}",
        "idempotency_key": idempotency_key,
        "dispatch_request_id": dispatch_request_id,
        "dispatch_request_path": dispatch_request_path,
        "preflight_report_hash": preflight_report_hash,
        "admission_report_hash": admission_report_hash,
        "task_id": task_id,
        "active_inbox_path": active_inbox_path,
        "actor": actor,
        "declared_worker": declared_worker,
        "required_worker": required_worker,
        "worker": declared_worker,
        "expected_materialized_execution": declared_worker == "materialized_execution",
        "workplace_profile": workplace_profile,
        "host": workplace["host"],
        "remote_workdir": remote_workdir,
        "remote_root": remote_root,
        "line": line,
        "batch": batch,
        "dag_root": dag_root,
        "prefix": prefix,
        "resource_hint": resource_hint,
        "resource_lane": resource_lane,
        "resource_fields": {
            "resource_hint": resource_hint,
            "resource_lane": resource_lane,
            "required_gpu_mem_gb": _to_float(_collect_resource_guard(dispatch_request).get("required")),
            "available_gpu_mem_gb": _to_float(_collect_resource_guard(dispatch_request).get("available")),
            "min_free_gpu_mem_gb": _to_float(_collect_resource_guard(dispatch_request).get("available")),
        },
        "required_artifacts": required_artifacts,
        "capability_checks": capability_checks,
        "expected_outputs": expected_outputs,
        "execution_mode": "dry_run",
        "status": "ready_for_materialized_execution_handoff",
        "reasons": [],
        "action_requests": [],
        "checks": [],
        "side_effects": side_effects,
    }
    if preflight_report_path:
        output["preflight_report_path"] = str(preflight_report_path)
    return output


def create_remote_execution_work_order_dry_run(
    preflight_report: Dict[str, Any] | str | Path,
) -> Dict[str, Any]:
    """Convert a passed preflight report into a local materialized-execution dry-run work-order."""

    side_effects = {
        "ssh": False,
        "remote_write": False,
        "scheduler_dispatch": False,
        "registry_mutation": False,
        "running_state": False,
        "subprocess": False,
        "remote_execution": False,
    }
    work_order_path = continuation_remote_execution_work_orders_dir()
    reasons: List[str] = []
    checks: List[Dict[str, Any]] = []

    try:
        report = _load_preflight_report(preflight_report)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return _failure_work_order(
            preflight={
                "request_id": "",
                "dispatch_request_id": "",
                "task_id": "",
                "actor": "",
                "idempotency_key": "",
                "dispatch_request_hash": "",
            },
            work_order_path=work_order_path / "invalid.json",
            reasons=["preflight_load_failed"],
            checks=[
                {"id": "preflight_load", "status": "failed", "reason": str(exc)},
            ],
        )

    preflight_report_path = Path(_normalize(report.get("dispatch_request_path")))
    checks.append({"id": "preflight_load", "status": "passed", "path": str(preflight_report_path) if preflight_report_path else ""})

    try:
        dispatch_request = _read_dispatch_request_payload(report)
    except (OSError, json.JSONDecodeError, ValueError, FileNotFoundError) as exc:
        reasons.append("dispatch_request_unreadable")
        checks.append({"id": "dispatch_request_read", "status": "failed", "reason": str(exc)})
        dispatch_request = {}
    else:
        checks.append({"id": "dispatch_request_read", "status": "passed"})

    if not isinstance(dispatch_request, dict):
        dispatch_request = {}

    preflight_reasons = _collect_remote_execution_checks(report, dispatch_request)
    reasons.extend(preflight_reasons)

    workplace = _normalize_workplace_metadata(report)
    dispatch_request_hash = _normalize(report.get("dispatch_request_hash"))
    dispatch_request_hash_input = _sha256_hex(dispatch_request) if dispatch_request else ""
    if dispatch_request and not dispatch_request_hash:
        dispatch_request_hash = dispatch_request_hash_input
    if not dispatch_request_hash:
        reasons.append("dispatch_request_hash_missing")

    request_id = _normalize(report.get("request_id") or dispatch_request.get("request_id"))
    if not request_id:
        reasons.append("dispatch_request_id_missing")

    actor = _normalize(report.get("actor"))
    if not actor:
        reasons.append("actor_missing")

    task_id = _normalize(report.get("task_id") or dispatch_request.get("task_id"))
    if not task_id:
        reasons.append("task_id_missing")

    active_inbox_path = _normalize(report.get("active_inbox_path") or dispatch_request.get("active_inbox_path"))
    if not active_inbox_path:
        reasons.append("active_inbox_path_missing")

    task_metadata = report.get("task_metadata", {})
    if not isinstance(task_metadata, dict):
        task_metadata = {}
    declared_worker = _normalize(task_metadata.get("declared_worker"))
    required_worker = _normalize(task_metadata.get("required_worker"))
    if not declared_worker or not required_worker:
        reasons.append("worker_metadata_missing")
    elif declared_worker != required_worker:
        reasons.append("worker_mismatch")

    reasons.extend(_resolve_materialized_execution_requirement(report))

    scope = _normalize_work_order_scope(report)
    if not scope["line"] or not scope["batch"] or not scope["dag_root"] or not scope["prefix"]:
        reasons.append("scope_field_missing")

    if not workplace["host"]:
        reasons.append("host_missing")
    if not workplace["remote_root"]:
        reasons.append("remote_root_missing")
    if not workplace["remote_workdir"]:
        reasons.append("remote_workdir_missing")

    resource_hint = _normalize(task_metadata.get("resource_hint") or dispatch_request.get("resource_hint") or "cpu")
    if resource_hint not in ALLOWED_RESOURCE_HINTS:
        reasons.append("resource_hint_unsupported")

    if "required_artifacts" not in dispatch_request:
        reasons.append("required_artifacts_missing")
    if "required_capabilities" not in dispatch_request:
        reasons.append("required_capabilities_missing")

    if _is_guessed_remote_path(workplace["remote_root"]) or _is_guessed_remote_path(workplace["remote_workdir"]):
        profile_data = _load_profile_by_name(workplace["profile"], workplace)
        profile_roots = []
        for key in ("remote_root", "remote_workdir", "remote_output_root"):
            value = _normalize(profile_data.get(key))
            if value:
                profile_roots.append(value)
        if not any(_path_within_path(workplace["remote_root"], root) for root in profile_roots):
            reasons.append("remote_path_guess")
        if not any(_path_within_path(workplace["remote_workdir"], root) for root in profile_roots):
            reasons.append("remote_path_guess")

    checks.append({"id": "side_effects", "status": "passed", **side_effects})

    work_order_root = continuation_remote_execution_work_orders_dir()
    work_order_root.mkdir(parents=True, exist_ok=True)
    checks.append({"id": "work_order_dir", "status": "passed", "path": str(work_order_root)})

    if reasons:
        work_order = _failure_work_order(
            preflight=report,
            work_order_path=work_order_root / "invalid.json",
            reasons=sorted(set(reasons)),
            checks=checks,
        )
        work_order["side_effects"] = side_effects
        return work_order

    payload = _build_work_order_payload(
        preflight=report,
        preflight_report_path=Path(preflight_report) if isinstance(preflight_report, (str, Path)) else None,
        dispatch_request=dispatch_request,
        check_reasons=checks,
    )
    payload["checks"] = checks
    payload["side_effects"] = side_effects

    required_items = _required_items_for_work_order(dispatch_request.get("required_artifacts"))
    if any(item.get("required") and not item.get("present") for item in required_items):
        payload["status"] = "blocked"
        payload["reasons"] = ["missing_required_artifact"]
        payload["action_requests"] = [{"type": "missing_artifact"}]
        payload["created"] = False
        payload["replayed"] = False
        payload["work_order_path"] = str(work_order_root / f"{payload['work_order_id']}.json")
        return payload

    capability_items = _required_items_for_work_order(dispatch_request.get("required_capabilities"))
    if any(item.get("required") and not item.get("present") for item in capability_items):
        payload["status"] = "blocked"
        payload["reasons"] = ["missing_required_capability"]
        payload["action_requests"] = [{"type": "missing_capability"}]
        payload["created"] = False
        payload["replayed"] = False
        payload["work_order_path"] = str(work_order_root / f"{payload['work_order_id']}.json")
        return payload

    work_order_path = work_order_root / f"{payload['work_order_id']}.json"
    if work_order_path.exists():
        try:
            existing = json.loads(work_order_path.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            return {
                **payload,
                "status": "blocked",
                "reasons": ["work_order_payload_corrupt"],
                "created": False,
                "replayed": False,
                "work_order_path": str(work_order_path),
            }
        if existing == payload:
            checks.append({"id": "idempotency", "status": "passed", "path": str(work_order_path)})
            return {
                **payload,
                "created": False,
                "replayed": True,
                "work_order_path": str(work_order_path),
                "checks": checks,
                "reasons": [],
            }
        if existing.get("idempotency_key") == payload["idempotency_key"]:
            checks.append({"id": "idempotency", "status": "failed", "path": str(work_order_path), "reason": "idempotency_collision"})
            return {
                **payload,
                "status": "blocked",
                "reasons": ["idempotency_collision"],
                "action_requests": [{"type": "idempotency_collision"}],
                "created": False,
                "replayed": False,
                "work_order_path": str(work_order_path),
                "checks": checks,
            }

        checks.append({"id": "idempotency", "status": "failed", "path": str(work_order_path), "reason": "idempotency_key_mismatch"})
        return {
            **payload,
            "status": "blocked",
            "reasons": ["work_order_id_collision"],
            "action_requests": [{"type": "idempotency_collision"}],
            "created": False,
            "replayed": False,
            "work_order_path": str(work_order_path),
            "checks": checks,
        }

    work_order_path.write_text(_canonical_json(payload) + "\n", encoding='utf-8')
    checks.append({"id": "idempotent_write", "status": "passed", "path": str(work_order_path)})
    return {
        **payload,
        "created": True,
        "replayed": False,
        "reasons": [],
        "action_requests": [],
        "work_order_path": str(work_order_path),
        "checks": checks,
    }


def assess_remote_execution_preflight_stub(
    dispatch_request_path: Path,
    *,
    workplace_profile: dict | None = None,
    actor: str,
) -> dict:
    return _assess_remote_execution_preflight_stub_v2(
        dispatch_request_path,
        workplace_profile=workplace_profile,
        actor=actor,
    )

__all__ = ["assess_remote_execution_preflight_stub", "create_remote_execution_work_order_dry_run"]
