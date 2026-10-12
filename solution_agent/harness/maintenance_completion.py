"""Explicit maintenance-only completion using the existing TaskCapsule CAS API."""
from __future__ import annotations


def finish_owned_maintenance(capsules, receipt, *, task_id, work_unit_id):
    # The caller supplies the original runner's recovered receipt, not a model
    # assertion that maintenance finished. Lost/unknown never permits cleanup.
    if (receipt.get('ok') is not True or receipt.get('task_id') != task_id
            or receipt.get('work_unit_id') != work_unit_id
            or not receipt.get('command_id') or receipt.get('access_mode') != 'writer'
            or receipt.get('state') not in {'succeeded', 'failed', 'cancelled'}):
        raise ValueError('owned terminal maintenance receipt required')
    current = capsules.load(task_id)
    if current.active_work_unit is None:
        return {'finished': True, 'already_finished': True, 'capsule_version': current.version,
                'task_id': task_id, 'work_unit_id': work_unit_id, 'command_id': receipt['command_id']}
    if current.active_work_unit != work_unit_id or current.writer_leases:
        raise ValueError('maintenance ownership or active writer conflict')
    finished = capsules.finish_work_unit(task_id, work_unit_id, expected_version=current.version)
    return {'finished': True, 'already_finished': False, 'capsule_version': finished.version,
            'task_id': task_id, 'work_unit_id': work_unit_id, 'command_id': receipt['command_id']}
