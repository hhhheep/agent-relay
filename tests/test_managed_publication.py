import asyncio
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest
from mcp import Client

from tests.test_harness_artifact_apply import setup_runtime
from solution_agent.harness.artifact_apply import ArtifactApplyEngine, WriteConflict
from solution_agent.harness.artifact_runtime import ArtifactAccessError
from solution_agent.harness.task_capsule import CapsuleConflict, WriterLeaseRef
from solution_agent.harness import mcp_server as server


def store(artifacts, task, body):
    return artifacts.put_bytes(task, io.BytesIO(json.dumps(body).encode()),
        media_type='application/json', source_kind='generated', source_ref='synthetic handoff').artifact_id


def fixture(root, *, sections=False):
    root = root.resolve()
    task, capsules, artifacts, engine = setup_runtime(root)
    target, draft = root/'target.md', root/'draft.md'
    before = '# Report\nkeep\n<!-- SYNC:status:START -->\nold\n<!-- SYNC:status:END -->\n'
    target.write_bytes(before.encode())
    draft.write_bytes(before.replace('\nold\n','\nnew\n').encode())
    declaration = dict(schema_version='managed-markdown/v1',task_id=task.task_id,
        direction='draft_to_authority',destination_kind='local_file',draft_path='draft.md',destination_path='target.md',
        draft_owner='writer',review_owner='manager',publish_owner='manager',
        scope='sections' if sections else 'full_document',sections=['managed:status'] if sections else [],
        accepted_destination_sha256=hashlib.sha256(target.read_bytes()).hexdigest())
    packet = dict(schema_version='1.0',kind='local_file',commit_required=True,commit_protocol='local_compare_and_swap',
        draft_path=str(draft),draft_sha256=hashlib.sha256(draft.read_bytes()).hexdigest(),destination_path=str(target),
        destination_record_id=None,expected_destination_sha256=declaration['accepted_destination_sha256'],
        reason='update verified status',writer_must_not_commit=True)
    return task,capsules,artifacts,engine,target,draft,declaration,packet


def review(artifacts, task, plan, **changes):
    body=dict(schema_version='managed-markdown-review/v1',plan_id=plan.plan_id,diff_artifact_id=plan.diff_artifact_id,
        candidate_sha256=plan.candidate_sha256,declaration_sha256=artifacts.resolve(task,plan.input_artifact_ids[2]).sha256,
        review_owner='manager',publish_owner='manager',decision='approved',evidence_refs=['synthetic validation'])
    body.update(changes)
    return store(artifacts,task,body)


def test_publication_mcp_round_trip_and_read_back_drift(tmp_path, monkeypatch):
    task,capsules,artifacts,engine,target,draft,decl,packet=fixture(tmp_path)
    monkeypatch.setattr(server,'_solution_root',lambda:tmp_path.resolve())
    monkeypatch.setattr(server,'_require_activation',lambda:None)
    monkeypatch.setattr(server,'_with_server',lambda value:value)
    packet_id=store(artifacts,task,packet)
    async def run():
        async with Client(server.mcp) as client:
            planned=await client.call_tool('artifact_apply',dict(task_id=task.task_id,mode='publication',
                request={'declaration':decl,'packet_artifact_id':packet_id}))
            data=planned.structured_content
            assert data['ok'],data
            assert data['publication']['status']=='draft_ready'
            assert data['writer_lease_id'] is None
            assert not capsules.load(task.task_id).writer_leases
            from solution_agent.harness.artifact_apply import ApplyPlan
            plan=ApplyPlan.model_validate(data['plan'])
            evidence=review(artifacts,task,plan)
            reviewed=(await client.call_tool('artifact_apply',dict(task_id=task.task_id,mode='publication',
                plan_id=plan.plan_id,request={'review':engine._json_artifact(task,evidence),'publish_owner':'manager'}))).structured_content
            assert reviewed['ok'],reviewed
            assert reviewed['publication']['status']=='reviewed'
            assert not capsules.load(task.task_id).writer_leases
            evidence=reviewed['publication']['review_artifact_id']
            request=dict(task_id=task.task_id,mode='publication',plan_id=plan.plan_id,commit=True,
                request={'review_artifact_id':evidence,'publish_owner':'manager'})
            published=(await client.call_tool('artifact_apply',request)).structured_content
            assert published['ok'],published
            assert published['publication']['status']=='verified'
            assert target.read_bytes()==draft.read_bytes()
            replay=(await client.call_tool('artifact_apply',request)).structured_content
            assert replay['receipt']==published['receipt']
            target.write_bytes(b'external update')
            stale=(await client.call_tool('artifact_apply',request)).structured_content
            assert stale['ok'] is False and stale['error']['code']=='WRITE_CONFLICT'
            assert target.read_bytes()==b'external update'
            assert (engine._plan_dir(task.task_id,plan.plan_id)/'receipt.json').is_file()
    asyncio.run(run())


def test_reviewed_plan_waits_for_existing_writer_and_survives_restart(tmp_path):
    task,capsules,artifacts,engine,target,draft,decl,packet=fixture(tmp_path)
    leased=capsules.acquire_writer_lease(task.task_id,WriterLeaseRef(lease_id='existing-writer',target=str(target),
        base_sha256=decl['accepted_destination_sha256'],owner_work_unit='original-unit',
        expires_at=datetime.now(timezone.utc)+timedelta(minutes=5)),expected_version=task.version)
    plan=engine.plan_publication(leased,tmp_path,decl,store(artifacts,task,packet))
    evidence=review(artifacts,task,plan)
    reviewed=engine.review_publication(task,plan.plan_id,engine._json_artifact(task,evidence),'manager')
    assert reviewed['status']=='reviewed_waiting_for_writer'
    evidence=reviewed['review_artifact_id']
    with pytest.raises(CapsuleConflict): engine.commit_publication(task,plan.plan_id,evidence,'manager')
    assert engine.inspect_publication(task,plan.plan_id)['status']=='reviewed_waiting_for_writer'
    assert capsules.load(task.task_id).writer_leases==leased.writer_leases
    assert b'old' in target.read_bytes()
    assert (engine._plan_dir(task.task_id,plan.plan_id)/'publication_review.json').is_file()
    capsules.release_writer_lease(task.task_id,'existing-writer',expected_version=leased.version)
    restarted=ArtifactApplyEngine(tmp_path/'state',artifacts,capsules)
    result=restarted.commit_publication(task,plan.plan_id,evidence,'manager')
    assert result['status']=='verified'
    assert not capsules.load(task.task_id).writer_leases


@pytest.mark.parametrize('field,value',[('decision','rejected'),('candidate_sha256','0'*64),
    ('diff_artifact_id','foreign'),('review_owner','writer'),('publish_owner','writer'),
    ('declaration_sha256','0'*64),('plan_id','foreign'),('evidence_refs',[])])
def test_review_must_bind_exact_plan_and_declared_owners(tmp_path,field,value):
    task,capsules,artifacts,engine,target,draft,decl,packet=fixture(tmp_path)
    plan=engine.plan_publication(task,tmp_path,decl,store(artifacts,task,packet))
    with pytest.raises((PermissionError,ValueError)):
        engine.commit_publication(task,plan.plan_id,review(artifacts,task,plan,**{field:value}),'manager')
    assert b'old' in target.read_bytes()
    assert not capsules.load(task.task_id).writer_leases


def test_sections_preserve_unrelated_bytes_and_pin_draft(tmp_path):
    task,capsules,artifacts,engine,target,draft,decl,packet=fixture(tmp_path,sections=True)
    plan=engine.plan_publication(task,tmp_path,decl,store(artifacts,task,packet))
    draft.write_bytes(b'new unreviewed draft')
    with pytest.raises(PermissionError): engine.commit(task,plan.plan_id,'anything')
    result=engine.commit_publication(task,plan.plan_id,review(artifacts,task,plan),'manager')
    assert result['status']=='verified' and b'keep\n' in target.read_bytes() and b'\nnew\n' in target.read_bytes()


def test_section_escape_and_stale_base_retain_draft_without_leases(tmp_path):
    task,capsules,artifacts,engine,target,draft,decl,packet=fixture(tmp_path,sections=True)
    draft.write_bytes(draft.read_bytes().replace(b'keep',b'unreviewed change'))
    packet['draft_sha256']=hashlib.sha256(draft.read_bytes()).hexdigest()
    with pytest.raises(PermissionError,match='outside declared sections'):
        engine.plan_publication(task,tmp_path,decl,store(artifacts,task,packet))
    decl['scope']='full_document';decl['sections']=[]
    target.write_bytes(b'newer authority')
    with pytest.raises(WriteConflict): engine.plan_publication(task,tmp_path,decl,store(artifacts,task,packet))
    assert draft.is_file() and not capsules.load(task.task_id).writer_leases


@pytest.mark.parametrize('mutation',[{'kind':'researchflow','commit_protocol':'researchflow_get_sha_put_with_409_reconcile'},
    {'destination_path':'other.md'},{'draft_sha256':'0'*64},{'writer_must_not_commit':False}])
def test_packet_cannot_redirect_or_silently_bypass_destination_owner(tmp_path,mutation):
    task,capsules,artifacts,engine,target,draft,decl,packet=fixture(tmp_path)
    packet.update(mutation)
    with pytest.raises(ValueError): engine.plan_publication(task,tmp_path,decl,store(artifacts,task,packet))
    assert b'old' in target.read_bytes() and not capsules.load(task.task_id).writer_leases


def test_actual_writer_packet_is_consumed_without_a_second_format(tmp_path,monkeypatch):
    from tests.test_harness_report_writer import writer, _evidence
    task,capsules,artifacts,engine,target,draft,decl,packet=fixture(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('SOLUTION_AGENT_USAGE_RUN_DIR',str(tmp_path/'usage'))
    evidence=tmp_path/'evidence.json';_evidence(evidence)
    def provider(*args,**kwargs):
        return ({'report_markdown':'# Reviewed\nSynthetic result [c1].','claims_used':['c1'],
                 'warnings':[],'unsupported_claims':[]},
                {'model':'deepseek-v4-flash-0731','usage':{'prompt_tokens':10,'completion_tokens':5}})
    result=writer.run(evidence_path=evidence,output_dir=tmp_path/'out',destination_kind='local_file',
        destination_path=target,expected_destination_sha256=decl['accepted_destination_sha256'],
        update_reason='review existing evidence',provider_call=provider)
    assert b'old' in target.read_bytes()
    decl['draft_path']='out/report.md'
    packet_id=artifacts.put_path(task,Path(result['publication_packet_path'])).artifact_id
    plan=engine.plan_publication(task,tmp_path,decl,packet_id)
    assert engine.commit_publication(task,plan.plan_id,review(artifacts,task,plan),'manager')['status']=='verified'
    assert target.read_bytes()==Path(result['report_path']).read_bytes()


@pytest.mark.parametrize('changes',[{'task_id':'another-task'},{'draft_path':'../draft.md'},
    {'destination_path':'*.md'},{'publish_owner':' '},{'accepted_destination_sha256':None}])
def test_declaration_rejects_wrong_task_path_owner_or_alignment(tmp_path,changes):
    task,capsules,artifacts,engine,target,draft,decl,packet=fixture(tmp_path)
    decl.update(changes)
    with pytest.raises((PermissionError,ValueError)):
        engine.plan_publication(task,tmp_path,decl,store(artifacts,task,packet))
    assert b'old' in target.read_bytes() and not capsules.load(task.task_id).writer_leases


def test_new_document_requires_explicit_null_base_and_scope_is_enforced(tmp_path):
    task,capsules,artifacts,engine,target,draft,decl,packet=fixture(tmp_path)
    target.unlink();decl['accepted_destination_sha256']=packet['expected_destination_sha256']=None
    plan=engine.plan_publication(task,tmp_path,decl,store(artifacts,task,packet))
    evidence=review(artifacts,task,plan)
    with pytest.raises(PermissionError): engine.commit_publication(task,plan.plan_id,evidence,'writer')
    assert not target.exists()
    assert engine.commit_publication(task,plan.plan_id,evidence,'manager')['status']=='verified'
    restricted=capsules.compare_and_swap(task.task_id,capsules.load(task.task_id).version,
        lambda c:c.model_copy(update={'scope':c.scope.model_copy(update={'writable_paths':[str(tmp_path/'other')]})}))
    with pytest.raises(PermissionError): engine.commit_publication(restricted,plan.plan_id,evidence,'manager')


def test_crash_after_receipt_recovers_only_original_plan_and_lease(tmp_path,monkeypatch):
    task,capsules,artifacts,engine,target,draft,decl,packet=fixture(tmp_path)
    plan=engine.plan_publication(task,tmp_path,decl,store(artifacts,task,packet))
    evidence=review(artifacts,task,plan)
    release=capsules.release_writer_lease
    monkeypatch.setattr(capsules,'release_writer_lease',lambda *a,**k:(_ for _ in ()).throw(CapsuleConflict('simulated crash')))
    with pytest.raises(CapsuleConflict): engine.commit_publication(task,plan.plan_id,evidence,'manager')
    assert target.read_bytes()==draft.read_bytes()
    assert len(capsules.load(task.task_id).writer_leases)==1
    monkeypatch.setattr(capsules,'release_writer_lease',release)
    result=engine.commit_publication(task,plan.plan_id,evidence,'manager')
    assert result['status']=='verified' and not capsules.load(task.task_id).writer_leases
    assert len([a for a in capsules.load(task.task_id).working_artifacts if a.role=='applied_target'])==1


def test_reviewed_plan_stale_target_and_expiry_do_not_acquire_lease(tmp_path):
    from solution_agent.harness.artifact_apply import ApplyError
    task,capsules,artifacts,engine,target,draft,decl,packet=fixture(tmp_path)
    plan=engine.plan_publication(task,tmp_path,decl,store(artifacts,task,packet))
    evidence=review(artifacts,task,plan)
    target.write_bytes(b'concurrent authority')
    with pytest.raises(WriteConflict): engine.commit_publication(task,plan.plan_id,evidence,'manager')
    assert not capsules.load(task.task_id).writer_leases
    path=engine._plan_dir(task.task_id,plan.plan_id)/'plan.json'
    path.write_text(plan.model_copy(update={'expires_at':datetime.now(timezone.utc)-timedelta(seconds=1)}).model_dump_json())
    with pytest.raises(ApplyError,match='expired'): engine.commit_publication(task,plan.plan_id,evidence,'manager')
    assert not capsules.load(task.task_id).writer_leases


def test_concurrent_retries_share_one_receipt_without_review_rebinding(tmp_path):
    task,capsules,artifacts,engine,target,draft,decl,packet=fixture(tmp_path)
    plan=engine.plan_publication(task,tmp_path,decl,store(artifacts,task,packet))
    assert engine.inspect_publication(task,plan.plan_id)['status']=='draft_ready'
    evidence=review(artifacts,task,plan)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:engine.commit_publication(task,plan.plan_id,evidence,'manager'),range(4)))
    assert all(r['receipt']==results[0]['receipt'] for r in results)
    assert engine.inspect_publication(task,plan.plan_id)['status']=='verified'
    with pytest.raises(PermissionError,match='rebinding'):
        engine.commit_publication(task,plan.plan_id,review(artifacts,task,plan,evidence_refs=['different review']),'manager')


def test_draft_read_scope_and_required_alignment_cannot_be_inferred(tmp_path):
    task,capsules,artifacts,engine,target,draft,decl,packet=fixture(tmp_path)
    packet_id=store(artifacts,task,packet)
    missing={k:v for k,v in decl.items() if k!='accepted_destination_sha256'}
    with pytest.raises(ValueError): engine.plan_publication(task,tmp_path,missing,packet_id)
    restricted=task.model_copy(update={'scope':task.scope.model_copy(update={'readable_paths':[str(target)]})})
    with pytest.raises(ArtifactAccessError): engine.plan_publication(restricted,tmp_path,decl,packet_id)
    assert b'old' in target.read_bytes() and not capsules.load(task.task_id).writer_leases


def test_existing_writer_uppercase_expected_sha_is_compatible(tmp_path):
    task,capsules,artifacts,engine,target,draft,decl,packet=fixture(tmp_path)
    packet['expected_destination_sha256']=packet['expected_destination_sha256'].upper()
    plan=engine.plan_publication(task,tmp_path,decl,store(artifacts,task,packet))
    assert engine.commit_publication(task,plan.plan_id,review(artifacts,task,plan),'manager')['status']=='verified'


def test_replace_without_receipt_recovers_same_publication_after_restart(tmp_path,monkeypatch):
    task,capsules,artifacts,engine,target,draft,decl,packet=fixture(tmp_path)
    plan=engine.plan_publication(task,tmp_path,decl,store(artifacts,task,packet))
    evidence=review(artifacts,task,plan)
    put=artifacts.put_path
    def interrupted(*args,**kwargs):
        raise RuntimeError('simulated interruption after atomic replace')
    monkeypatch.setattr(artifacts,'put_path',interrupted)
    with pytest.raises(RuntimeError,match='simulated interruption'):
        engine.commit_publication(task,plan.plan_id,evidence,'manager')
    assert target.read_bytes()==draft.read_bytes()
    assert not (engine._plan_dir(task.task_id,plan.plan_id)/'receipt.json').exists()
    monkeypatch.setattr(artifacts,'put_path',put)
    restarted=ArtifactApplyEngine(tmp_path/'state',artifacts,capsules)
    result=restarted.commit_publication(task,plan.plan_id,evidence,'manager')
    assert result['receipt']['recovered_after_replace'] is True
    assert result['status']=='verified' and not capsules.load(task.task_id).writer_leases


def test_receipt_without_its_review_link_cannot_be_reported_verified(tmp_path):
    from solution_agent.harness.artifact_apply import ApplyError
    task,capsules,artifacts,engine,target,draft,decl,packet=fixture(tmp_path)
    plan=engine.plan_publication(task,tmp_path,decl,store(artifacts,task,packet))
    engine.commit_publication(task,plan.plan_id,review(artifacts,task,plan),'manager')
    directory=engine._plan_dir(task.task_id,plan.plan_id)
    (directory/'publication_review.json').unlink()
    with pytest.raises(ApplyError,match='review evidence is missing'):
        engine.inspect_publication(task,plan.plan_id)
    assert (directory/'receipt.json').is_file() and target.read_bytes()==draft.read_bytes()
