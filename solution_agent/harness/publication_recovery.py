"""Read-only, profile-bound absence inspection for legacy prepared publications."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from solution_agent.harness.codex_worker import worker_packet
from solution_agent.harness.task_capsule import TaskCapsuleStore
from solution_agent.harness.remote_terminal import ProfileTerminalManager, load_terminal_profile


class RecoveryEvidenceError(ValueError):
    """An unresolved command must be reconciled before proving no execution."""
    def __init__(self, directory, reason):
        self.command_id = directory.name
        self.evidence_path = str(directory)
        self.reason = reason
        super().__init__(f'recovery blocked: command {self.command_id}: {reason}; '
                         f'evidence={self.evidence_path}; preserve command and reconcile history')


def _read_evidence(directory, filename):
    path = directory / filename
    if path.is_symlink():
        raise RecoveryEvidenceError(directory, f'linked {filename}')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise RecoveryEvidenceError(directory, f'missing or unreadable {filename}') from error
    if not isinstance(value, dict):
        raise RecoveryEvidenceError(directory, f'invalid {filename} object')
    if value.get('command_id', directory.name) != directory.name:
        raise RecoveryEvidenceError(directory, f'conflicting command identity in {filename}')
    return value


def _inspect_command(directories, binding):
    """Reconcile storage generations by command ID, never by status alone.

    A surviving task-bound request can identify a foreign command even when
    its newer status directory is only a lost tombstone. Unknown identities
    remain blockers; status is not a substitute for a request.
    """
    requests, statuses, warnings = [], [], []
    for directory in directories:
        for filename, values in (('request.json', requests), ('status.json', statuses)):
            path = directory / filename
            if not path.exists() and not path.is_symlink():
                warnings.append({'path': str(path), 'reason': 'missing'})
                continue
            values.append(_read_evidence(directory, filename))
    if not requests:
        raise RecoveryEvidenceError(directories[0], 'no authoritative request in either storage generation')
    # Missing identity in one generation may not be replaced by guesswork.
    identity_keys = ('task_id', 'work_unit_id', 'access_mode', 'command_sha256')
    for key in identity_keys:
        values = {json.dumps(r[key], sort_keys=True) for r in requests if key in r}
        if len(values) > 1:
            raise RecoveryEvidenceError(directories[0], f'conflicting {key} across storage generations')
    request = {}
    for item in requests:
        request.update(item)
    if request.get('access_mode') == 'read_only':
        return None, warnings
    task = request.get('task_id')
    if not isinstance(task, str) or not task.strip():
        raise RecoveryEvidenceError(directories[0], 'request lacks authoritative task identity')
    for status in statuses:
        for key in identity_keys:
            # Lost tombstones may lack identity or explicitly set it to null.
            if status.get(key) is not None and key in request and status[key] != request[key]:
                raise RecoveryEvidenceError(directories[0], f'conflicting {key} in status evidence')
    if task != binding['task_id']:
        return None, warnings
    unit = request.get('work_unit_id')
    if not isinstance(unit, str) or not unit.strip():
        raise RecoveryEvidenceError(directories[0], 'legacy task job lacks work-unit identity')
    if unit == binding['work_unit_id']:
        return {'command_id': directories[0].name,
                'states': sorted({str(s.get('state', 'unknown')) for s in statuses}) or ['unknown']}, warnings
    return None, warnings


def inspect_unstarted(root, profile_name, binding, text, phase):
    root = Path(root).resolve(strict=True)
    packet = worker_packet(root, profile_name, binding['task_id'], binding['work_unit_id'], text, phase)
    before = TaskCapsuleStore(root / 'state').load(binding['task_id'])
    if before.active_work_unit is not None or before.writer_leases:
        raise ValueError('active work or writer lease prevents absence proof')
    if Path(packet['control']).exists() or Path(packet['output']).exists():
        raise ValueError('worker control or output already exists; recover original command')
    manager = ProfileTerminalManager(load_terminal_profile(root))
    matches, warnings, commands = [], [], {}
    roots = [manager.base / 'jobs', manager.base / 'main' / 'commands']
    for jobs in roots:
        if jobs.is_symlink():
            raise ValueError('linked job root')
        if not jobs.exists():
            continue
        entries = list(jobs.iterdir())
        if len(entries) > 20000:
            raise ValueError('inspection bound exceeded')
        for directory in entries:
            if directory.name.startswith('.'):
                continue
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError('unrecognized job evidence')
            commands.setdefault(directory.name, []).append(directory)
    blockers = []
    for command_id in sorted(commands):
        try:
            match, notices = _inspect_command(commands[command_id], binding)
            warnings.extend(notices)
            if match is not None:
                matches.append(match)
        except RecoveryEvidenceError as error:
            blockers.append(error)
    if blockers:
        error = blockers[0]
        error.blockers = [{'command_id': e.command_id, 'reason': e.reason,
                           'evidence_path': e.evidence_path} for e in blockers]
        raise error
    after = TaskCapsuleStore(root / 'state').load(binding['task_id'])
    if after.version != before.version or after.writer_leases or after.active_work_unit is not None:
        raise ValueError('capsule changed during inspection')
    if Path(packet['control']).exists() or Path(packet['output']).exists() or matches:
        raise ValueError('execution evidence exists; recover original command')
    return {'schema': 'harness-unstarted/v1', 'binding': binding,
            'worker_task_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
            'worker_phase': phase, 'capsule_version': after.version, 'active_work_unit': None,
            'writer_leases': 0, 'worker_control_exists': False, 'worker_output_exists': False,
            'matching_jobs': matches, 'inspected_roots': [str(p) for p in roots],
            'history_warnings': warnings, 'inspected_command_count': len(commands)}
