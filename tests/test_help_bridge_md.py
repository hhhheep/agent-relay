from __future__ import annotations
import json
import pytest
from solution_agent.harness.help_bridge import transfer,deliver_reply
from solution_agent.harness.md_snapshot import build_bundle,materialize,verify_snapshot
from tests.test_worker_help_queue import queue,binding
from tests.test_harness_codex_worker import packet,write_result
from tests.test_worker_help_queue import enable
from solution_agent.harness import codex_worker as worker,worker_help as help
from pathlib import Path
from types import SimpleNamespace


def test_local_offline_then_lost_acceptance_does_not_duplicate(tmp_path):
    path=tmp_path/'q.sqlite'; q=queue(path); q.ask('q','problem','check'); q.close()
    def offline(e): raise ConnectionError('App closed')
    assert transfer(path,binding(),offline)['state']=='waiting_manager'
    accepted={}; lose=[True]
    def receive(e):
        accepted.setdefault(e['delivery_id'],e)
        if lose[0]: lose[0]=False; raise TimeoutError('receipt lost')
        return {'delivery_id':e['delivery_id'],'origin_chat_id':'chat','durably_received':True}
    assert transfer(path,binding(),receive)['state']=='waiting_manager'
    assert transfer(path,binding(),receive)['delivered']==1
    assert len(accepted)==1
    assert transfer(path,binding(),offline)['delivered']==0
    delivery=next(iter(accepted))
    deliver_reply(path,binding(),{'binding':binding(),'delivery_id':delivery,'answer':'proposal'})
    q=queue(path); assert q.status('q')['answer']=='proposal'; q.close()


def test_bridge_rejects_wrong_origin_and_false_ack(tmp_path):
    path=tmp_path/'q.sqlite'; q=queue(path); q.ask('q','problem','check'); q.close()
    with pytest.raises(ValueError,match='untrusted'): transfer(path,binding(chat='wrong'),lambda e: {})
    with pytest.raises(ValueError,match='invalid manager'): transfer(path,binding(),lambda e: {'durably_received':True})
    q=queue(path); assert q.pending()[0]['state']=='pending'; q.close()


def test_md_versions_repeat_and_corruption(tmp_path):
    source=tmp_path/'source'; source.mkdir(); (source/'SKILL.md').write_text('v1')
    root=tmp_path/'remote'
    first=build_bundle(source,['SKILL.md']); old=materialize(first,root)
    assert materialize(first,root)==old
    (source/'SKILL.md').write_text('v2'); second=build_bundle(source,['SKILL.md'])
    new=materialize(second,root)
    assert old!=new and (old/'SKILL.md').read_text()=='v1'
    (new/'SKILL.md').write_text('divergent remote edit')
    with pytest.raises(ValueError,match='hash mismatch'): materialize(second,root)
    assert (new/'SKILL.md').read_text()=='divergent remote edit'


@pytest.mark.parametrize('name',['../escape.md','/absolute.md','a\\b.md','a.py','C:/x.md','a/../b.md'])
def test_md_path_rejection(tmp_path,name):
    with pytest.raises(ValueError): build_bundle(tmp_path,[name])


def test_md_failed_transfer_cannot_publish(tmp_path):
    (tmp_path/'a.md').write_text('a'); (tmp_path/'b.md').write_text('b')
    bundle=build_bundle(tmp_path,['a.md','b.md']); bundle['files'].pop('b.md')
    with pytest.raises(ValueError): materialize(bundle,tmp_path/'remote')
    assert not (tmp_path/'remote').exists()


@pytest.mark.parametrize('route_on,md_on',[(False,False),(True,False),(False,True),(True,True)])
def test_worker_four_feature_combinations(packet,monkeypatch,tmp_path,route_on,md_on):
    if route_on: packet=enable(packet)
    if md_on:
        source=tmp_path/'docs'; source.mkdir(); (source/'usage.md').write_text('Read task inputs first.')
        bundle=build_bundle(source,['usage.md'])
        materialize(bundle,Path(packet['cwd'])/'.solution-agent'/'md-versions')
        root=Path(packet['root']); (root/'config').mkdir(exist_ok=True)
        key=worker._digest({'task_id':packet['task_id'],'work_unit_id':packet['work_unit_id']})
        (root/'config'/'experimental-worker-markdown.json').write_text(json.dumps({'enabled':True,'bindings':{key:{'manifest':bundle['manifest'],'version':bundle['version']}}}))
        packet=worker.worker_packet(root,packet['profile_id'],packet['task_id'],packet['work_unit_id'],packet['text'],packet['phase'])
    seen=[]
    def fake_watch(argv,prompt,cwd,env,route): seen.append(prompt); write_result(packet); return 0
    def fake_run(argv,**kwargs): seen.append(kwargs['input'].decode()); write_result(packet); return SimpleNamespace(returncode=0)
    monkeypatch.setattr(help,'run_with_watch',fake_watch)
    monkeypatch.setattr(worker.subprocess,'run',fake_run)
    assert worker.run_worker(packet)==0
    assert ('--request-id' in seen[0])==route_on
    assert ('usage.md' in seen[0])==md_on
    receipt=json.loads((Path(packet['control'])/'receipt.json').read_text())
    assert ('markdown_snapshot' in receipt)==md_on


def test_sync_interrupted_write_keeps_previous_snapshot(tmp_path,monkeypatch):
    (tmp_path/'a.md').write_text('v1'); root=tmp_path/'remote'
    first=build_bundle(tmp_path,['a.md']); old=materialize(first,root)
    (tmp_path/'a.md').write_text('v2'); second=build_bundle(tmp_path,['a.md'])
    original=Path.rename
    def interrupted(self,target):
        if self.name.startswith('.staging-'): raise OSError('connection/process interrupted before publication')
        return original(self,target)
    monkeypatch.setattr(Path,'rename',interrupted)
    with pytest.raises(OSError): materialize(second,root)
    verify_snapshot(old,first['manifest'])
    assert not (root/second['version']).exists()
