"""Design fault/concurrency checks; these do not exercise production launch."""
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import pytest
from design.admission_model import AdmissionModel


def accept(model, **changes):
    args=dict(submission_id='publication', workplace='immutable-workplace-id',
              task='task', unit='unit', request={'text':'run','phase':'prepare_code'})
    args.update(changes)
    return model.accept(**args)


def test_lost_accept_response_and_restart_recovers_original_command(tmp_path):
    path=tmp_path/'admissions.sqlite';model=AdmissionModel(path)
    original=accept(model);model.close()
    reopened=AdmissionModel(path)
    try:
        assert accept(reopened)==original
        assert original['state']=='accepted' and original['permit'] is None
    finally:reopened.close()


@pytest.mark.parametrize('change', [{'request':{'text':'different'}}, {'workplace':'other'}, {'task':'other'}, {'unit':'other'}])
def test_reusing_identity_cannot_change_request_or_binding(tmp_path, change):
    model=AdmissionModel(tmp_path/'admissions.sqlite')
    try:
        original=accept(model)
        with pytest.raises(ValueError, match='identity_conflict'):accept(model, **change)
        assert model.inspect('publication')==original
    finally:model.close()


def test_new_token_cannot_bypass_work_unit_uniqueness(tmp_path):
    model=AdmissionModel(tmp_path/'admissions.sqlite')
    try:
        accept(model)
        with pytest.raises(sqlite3.IntegrityError):accept(model, submission_id='new-token')
        assert model.db.execute('SELECT count(*) FROM admissions').fetchone()[0]==1
    finally:model.close()


def test_concurrent_retries_share_command_and_only_one_supervisor_can_enter(tmp_path):
    path=tmp_path/'admissions.sqlite';seed=AdmissionModel(path);seed.close()
    def retry(_):
        model=AdmissionModel(path)
        try:
            row=accept(model);model.lease_confirmed('publication')
            return row['command_id'],model.consume_permit('publication')
        finally:model.close()
    with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(retry,range(32)))
    assert len({r[0] for r in results})==1
    assert sum(r[1] is not None for r in results)==1


@pytest.mark.parametrize('child_may_have_run', [False, True])
def test_crash_after_gate_never_becomes_automatic_relaunch(tmp_path, child_may_have_run):
    path=tmp_path/'admissions.sqlite';model=AdmissionModel(path)
    accept(model);model.lease_confirmed('publication');permit=model.consume_permit('publication')
    # Both crash windows have identical durable evidence. The model cannot
    # infer external side effects from a missing running/terminal receipt.
    effects=['external-effect'] if child_may_have_run else []
    model.close();reopened=AdmissionModel(path)
    try:
        assert reopened.attention('publication')['state']=='needs_attention'
        accept(reopened);reopened.lease_confirmed('publication')
        assert reopened.consume_permit('publication') is None
        assert len(effects)==int(child_may_have_run) and permit
    finally:reopened.close()


def test_terminal_receipt_replay_is_stable_and_owner_checked(tmp_path):
    model=AdmissionModel(tmp_path/'admissions.sqlite')
    try:
        accept(model);model.lease_confirmed('publication');permit=model.consume_permit('publication')
        receipt={'exit_code':0,'artifacts':{'result':'digest'}}
        original=model.terminal('publication',permit,receipt)
        assert model.terminal('publication',permit,receipt)==original
        with pytest.raises(ValueError,match='owner_conflict'):model.terminal('publication','wrong',receipt)
        with pytest.raises(ValueError,match='receipt_conflict'):model.terminal('publication',permit,{'exit_code':1})
        assert model.consume_permit('publication') is None
    finally:model.close()
