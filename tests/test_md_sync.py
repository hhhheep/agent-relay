from pathlib import Path
import json
import subprocess
import sys
import pytest
from solution_agent.harness.task_md_sync import plan_sync,accept_sync


def test_actual_receiver_process_unicode_update_retry_and_conflict(tmp_path):
    source=tmp_path/'local'; source.mkdir(); remote=tmp_path/'remote with spaces'; remote.mkdir()
    (source/'使用.md').write_text('版本一\n`$(not-a-command)`',encoding='utf-8')
    def build(): return plan_sync(source,['使用.md'],remote_python=sys.executable,remote_workplace=str(remote))
    first=build()
    def run(plan):
        result=subprocess.run(plan['argv'],capture_output=True,check=True)
        return accept_sync(plan,{'state':'succeeded','exit_code':0,'stdout':{'preview':result.stdout.decode()}})
    receipt=run(first); assert run(first)==receipt
    (source/'使用.md').write_text('版本二',encoding='utf-8'); second=build(); new=run(second)
    assert receipt['version']!=new['version']
    assert (Path(receipt['directory'])/'使用.md').read_text(encoding='utf-8').startswith('版本一')
    (Path(new['directory'])/'使用.md').write_text('remote edit')
    assert subprocess.run(second['argv'],capture_output=True).returncode!=0


def test_no_acceptance_on_disconnection_or_wrong_receipt(tmp_path):
    (tmp_path/'a.md').write_text('content')
    plan=plan_sync(tmp_path,['a.md'],remote_python=sys.executable,remote_workplace=str(tmp_path))
    with pytest.raises(ValueError,match='not completed'): accept_sync(plan,{'state':'running'})
    with pytest.raises(ValueError,match='mismatch'): accept_sync(plan,{'state':'succeeded','exit_code':0,'stdout':{'preview':'{"ok":true,"version":"wrong"}'}})
    with pytest.raises(ValueError,match='absolute Python'): plan_sync(tmp_path,['a.md'],remote_python='python',remote_workplace=str(tmp_path))


def test_oversize_requires_explicit_artifact_transport(tmp_path):
    (tmp_path/'a.md').write_text('a'*48000)
    with pytest.raises(ValueError,match='artifact transport'): plan_sync(tmp_path,['a.md'],remote_python=sys.executable,remote_workplace=str(tmp_path))


def test_receiver_preserves_declared_alias_and_rejects_other_destination(tmp_path):
    (tmp_path/'a.md').write_text('verified content')
    (tmp_path/'alias').mkdir()
    workplace=tmp_path/'alias'/'..'
    plan=plan_sync(tmp_path,['a.md'],remote_python=sys.executable,remote_workplace=str(workplace))
    result=subprocess.run(plan['argv'],capture_output=True,check=True)
    receipt=json.loads(result.stdout)
    command={'state':'succeeded','exit_code':0,'stdout':{'preview':json.dumps(receipt)}}
    assert accept_sync(plan,command)==receipt
    assert Path(receipt['directory']).resolve().parent==tmp_path.resolve()/'.solution-agent'/'md-versions'
    assert (Path(receipt['directory'])/'a.md').read_text()=='verified content'
    receipt['directory']=str(tmp_path/'elsewhere'/plan['version'])
    command['stdout']['preview']=json.dumps(receipt)
    with pytest.raises(ValueError,match='destination mismatch'):
        accept_sync(plan,command)
