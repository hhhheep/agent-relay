"""Optional real CLI, synthetic loopback model, no user credentials or paid API."""
import json
import os
from pathlib import Path
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

from tests.test_worker_session_pool import pool
from tests.test_harness_codex_worker import packet
from solution_agent.harness import codex_worker as worker
from solution_agent.harness.worker_sessions import WorkerAdmissionError


@pytest.mark.skipif(not os.environ.get("HARNESS_TEST_CODEX"), reason="requires explicitly supplied local Codex CLI; never calls paid provider")
def test_real_cli_worker_pool_resumes_exact_thread_with_prior_history(pool, monkeypatch):
    binary = Path(os.environ["HARNESS_TEST_CODEX"]).resolve()
    assert binary.is_file()
    pool.profile.codex_path = str(binary)
    home = Path(os.environ["CODEX_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    requests = []
    current = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass

        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"data":[]}')

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            requests.append(body)
            p = current["packet"]
            output = Path(p["output"])
            output.mkdir(parents=True, exist_ok=True)
            (output / "result.yaml").write_text(yaml.safe_dump({"task_id": p["task_id"], "worker": "codex",
                "status": "success", "summary_short": "synthetic result", "outputs": {"worker_notes": "worker_notes.md"}}), encoding="utf-8")
            for name in ("worker_notes.md", "stdout.log", "stderr.log"):
                (output / name).write_text("synthetic evidence", encoding="utf-8")
            index = len(requests)
            events = [
                {"type": "response.created", "response": {"id": "resp-" + str(index)}},
                {"type": "response.output_item.done", "item": {"type": "message", "role": "assistant",
                    "id": "msg-" + str(index), "content": [{"type": "output_text", "text": "POOL_ANSWER_" + str(index)}]}},
                {"type": "response.completed", "response": {"id": "resp-" + str(index),
                    "usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25,
                              "input_tokens_details": None, "output_tokens_details": None}}},
            ]
            data = ''.join('data: ' + json.dumps(e) + '\n\n' for e in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    config = ('model="gpt-6.1-sol"\nmodel_provider="fixture"\n'
              '[model_providers.fixture]\nname="Local fixture"\nwire_api="responses"\n'
              'requires_openai_auth=false\nsupports_websockets=false\nrequest_max_retries=0\n'
              'stream_max_retries=0\nstream_idle_timeout_ms=5000\n'
              'base_url="http://127.0.0.1:' + str(server.server_port) + '/v1"\n')
    (home / 'config.toml').write_text(config, encoding='utf-8')
    real_run = subprocess.run

    def run_cli(argv, **kwargs):
        p, request = current["packet"], current["request"]
        directory = pool.jobs / request["command_id"]
        keep = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "COMSPEC",
                "SYSTEMDRIVE", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "CODEX_HOME",
                "SOLUTION_AGENT_TERMINAL_NESTING"}
        kwargs["env"] = {k: v for k, v in kwargs["env"].items() if k.upper() in keep}
        with (directory / "stdout.bin").open('wb') as out, (directory / "stderr.bin").open('wb') as err:
            return real_run(argv, **kwargs, stdout=out, stderr=err, timeout=40)

    try:
        identities = []
        for task, unit, marker in (("TASK", "first", "POOL_FIRST_A"), ("TASK-B", "first", "POOL_FIRST_B"),
                                    ("TASK", "second", "POOL_SECOND_A")):
            p = pool.make(task, unit); p["text"] = marker
            request = pool.prepare(p)
            if len(identities) == 2:
                wrong = pool.assigned(request)
                wrong["session_assignment"]["thread_id"] = identities[1]
                monkeypatch.setenv("SOLUTION_AGENT_COMMAND_ID", request["command_id"])
                monkeypatch.setenv("SOLUTION_AGENT_COMMAND_DIR", str(pool.jobs / request["command_id"]))
                before = len(requests)
                with pytest.raises(WorkerAdmissionError, match="dispatch_mismatch"):
                    worker.run_worker(wrong)
                assert len(requests) == before
            current.update(packet=p, request=request)
            receipt = pool.complete(p, request, codex_run=run_cli)
            identities.append(receipt["worker_session"]["thread_id"])
        assert identities[0] == identities[2] and identities[0] != identities[1]
        final_request = json.dumps(requests[-1])
        assert "POOL_FIRST_A" in final_request and "POOL_ANSWER_1" in final_request and "POOL_SECOND_A" in final_request
        assert "POOL_FIRST_B" not in final_request and "POOL_ANSWER_2" not in final_request
    finally:
        server.shutdown(); server.server_close()


@pytest.mark.skipif(not os.environ.get("HARNESS_TEST_CODEX"), reason="requires explicitly supplied local Codex CLI; never calls paid provider")
def test_real_cli_precompact_accepts_production_hook_output_and_stops_before_compaction(tmp_path):
    """Extra stdout keys make Codex ignore continue:false; test its real parser."""
    import shlex
    import shutil
    repo = Path(__file__).resolve().parents[1]
    binary = Path(os.environ['HARNESS_TEST_CODEX']).resolve()
    node = shutil.which('node')
    assert binary.is_file() and node
    home = tmp_path / 'isolated-home'; home.mkdir()
    callback = repo / 'tests/fixtures/precompact_cli_callback.mts'
    loader = repo / 'components/lifecycle/node_modules/tsx/dist/loader.mjs'
    assert loader.is_file(), 'install frozen Lifecycle test dependencies first'
    event_path = tmp_path / 'precompact-event.json'
    argv = [node, '--import', loader.as_uri(), str(callback), str(event_path)]
    command = ('& ' + ' '.join("'" + arg.replace("'", "''") + "'" for arg in argv)) if os.name == 'nt' else shlex.join(argv)
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"data":[]}')
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', 0))); requests.append(self.path)
            events = [
                {'type':'response.created','response':{'id':'fixture-response'}},
                {'type':'response.output_item.done','item':{'type':'message','role':'assistant','id':'fixture-message','content':[{'type':'output_text','text':'OK'}]}},
                {'type':'response.completed','response':{'id':'fixture-response','usage':{'input_tokens':20000,'output_tokens':5,'total_tokens':20005,'input_tokens_details':None,'output_tokens_details':None}}}]
            data = ''.join('data: '+json.dumps(e)+'\n\n' for e in events).encode()
            self.send_response(200); self.send_header('Content-Type','text/event-stream'); self.send_header('Content-Length',str(len(data))); self.end_headers(); self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    (home/'config.toml').write_text('model="gpt-6.1-sol"\nmodel_provider="fixture"\nmodel_auto_compact_token_limit=1\nmodel_auto_compact_token_limit_scope="total"\n[features]\nhooks=true\n[[hooks.PreCompact]]\nmatcher="auto"\n[[hooks.PreCompact.hooks]]\ntype="command"\ncommand='+json.dumps(command)+'\n[model_providers.fixture]\nname="Loopback only"\nwire_api="responses"\nrequires_openai_auth=false\nsupports_websockets=false\nrequest_max_retries=0\nstream_max_retries=0\nstream_idle_timeout_ms=3000\nbase_url="http://127.0.0.1:'+str(server.server_port)+'/v1"\n', encoding='utf8')
    keep = {'SYSTEMROOT','WINDIR','PATH','PATHEXT','TEMP','TMP','COMSPEC','SYSTEMDRIVE','USERPROFILE','APPDATA','LOCALAPPDATA'}
    env = {k:v for k,v in os.environ.items() if k.upper() in keep}; env['CODEX_HOME'] = str(home)
    def run(args):
        return subprocess.run([str(binary),'--dangerously-bypass-hook-trust','-a','never','exec',*args],input='Isolated hook contract fixture',text=True,capture_output=True,env=env,cwd=home,timeout=30)
    try:
        first = run(['-s','read-only','--skip-git-repo-check','--json','-'])
        assert first.returncode == 0, first.stderr
        thread = next(json.loads(line)['thread_id'] for line in first.stdout.splitlines() if json.loads(line).get('type')=='thread.started')
        second = run(['resume','--skip-git-repo-check','--json',thread,'-'])
        assert event_path.is_file(), second.stdout + second.stderr
        event = json.loads(event_path.read_text())
        assert event['session_id'] == thread and event['trigger'] == 'auto'
        assert second.returncode == 1 and len(requests) == 1, 'production hook must stop before any compaction request'
    finally:
        server.shutdown(); server.server_close()
