from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import pytest
from solution_agent.harness.publication_registry import install,lookup,key
from solution_agent.harness import codex_worker as worker
from tests.test_harness_codex_worker import packet


def test_concurrent_publish_and_conflicting_rebind(tmp_path):
    path=tmp_path/'registry.sqlite'
    values=[dict(task_id='task',work_unit_id='unit'+str(i),worker_id='worker'+str(i),origin_chat_id='chat'+str(i%2)) for i in range(16)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        receipts=list(executor.map(lambda b:install(path,b),values))
    assert all(r['installed'] for r in receipts)
    for b in values: assert lookup(path,key(b['task_id'],b['work_unit_id']))['binding']==b
    assert install(path,values[0])==receipts[0]
    with pytest.raises(ValueError,match='rebinding'): install(path,{**values[0],'origin_chat_id':'wrong'})


def test_worker_packet_loads_registered_binding_without_changing_unregistered_task(packet):
    root=Path(packet['root']);binding=dict(task_id=packet['task_id'],work_unit_id=packet['work_unit_id'],worker_id='worker',origin_chat_id='origin')
    install(root/'config'/'help-publications.sqlite',binding)
    routed=worker.worker_packet(root,packet['profile_id'],packet['task_id'],packet['work_unit_id'],packet['text'],packet['phase'])
    assert routed['help_route']['binding']==binding
    other=worker.worker_packet(root,packet['profile_id'],packet['task_id'],'unregistered',packet['text'],packet['phase'])
    assert 'help_route' not in other


def test_worker_packet_includes_registered_markdown_and_rejects_changed_files(packet):
    from solution_agent.harness.md_snapshot import build_bundle,materialize
    root=Path(packet['root']);cwd=Path(packet['cwd'])
    (cwd/'instructions.md').write_text('pinned worker instructions')
    bundle=build_bundle(cwd,['instructions.md'])
    directory=materialize(bundle,cwd/'.solution-agent'/'md-versions')
    binding=dict(task_id=packet['task_id'],work_unit_id=packet['work_unit_id'],worker_id='worker',origin_chat_id='origin')
    install(root/'config'/'help-publications.sqlite',binding,markdown={k:bundle[k] for k in ('version','manifest')},workplace=cwd)
    def build(): return worker.worker_packet(root,packet['profile_id'],packet['task_id'],packet['work_unit_id'],packet['text'],packet['phase'])
    assert build()['markdown_snapshot']['version']==bundle['version']
    (directory/'instructions.md').write_text('changed')
    with pytest.raises(ValueError,match='hash'): build()


def test_markdown_registration_requires_existing_verified_snapshot_and_pins_version(tmp_path):
    from solution_agent.harness.md_snapshot import build_bundle,materialize
    from solution_agent.harness.worker_help import markdown_config
    source=tmp_path/'source';source.mkdir();(source/'guide.md').write_text('version one')
    workplace=tmp_path/'workplace';workplace.mkdir();runtime=tmp_path/'runtime'
    path=runtime/'config'/'help-publications.sqlite'
    binding=dict(task_id='task',work_unit_id='unit',worker_id='worker',origin_chat_id='chat')
    bundle=build_bundle(source,['guide.md']);snapshot={k:bundle[k] for k in ('version','manifest')}
    with pytest.raises(ValueError,match='snapshot'): install(path,binding,markdown=snapshot,workplace=workplace)
    assert not path.exists()
    directory=materialize(bundle,workplace/'.solution-agent'/'md-versions')
    receipt=install(path,binding,markdown=snapshot,workplace=workplace)
    assert receipt['markdown']==snapshot
    assert markdown_config(runtime,key('task','unit'),workplace)=={**snapshot,'directory':str(directory)}
    (source/'guide.md').write_text('version two');next_bundle=build_bundle(source,['guide.md'])
    materialize(next_bundle,workplace/'.solution-agent'/'md-versions')
    with pytest.raises(ValueError,match='rebinding'):
        install(path,binding,markdown={k:next_bundle[k] for k in ('version','manifest')},workplace=workplace)
    # The same task is pinned even when a newer snapshot exists.
    assert markdown_config(runtime,key('task','unit'),workplace)['version']==snapshot['version']
    (directory/'guide.md').write_text('tampered after admission')
    with pytest.raises(ValueError,match='hash'): markdown_config(runtime,key('task','unit'),workplace)
