"""Explicit optional installed-CLI fixture, loopback only, isolated credential-free home."""
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import yaml


def complete(root, pool, packet, request, action):
    binary = Path(action['codex_binary']).resolve()
    assert binary.is_file()
    home = Path(os.environ['CODEX_HOME'])
    home.mkdir(parents=True, exist_ok=True)
    gate = root / 'complete-worker.signal'
    called = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b'{"data":[]}')
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            called.append(self.path)
            (root / 'actual-worker-waiting.json').write_text(json.dumps({'command_id':request['command_id'], 'pid':os.getpid()}))
            start = time.monotonic()
            while not gate.exists():
                if time.monotonic() - start > 600: raise TimeoutError('bounded Desktop acceptance wait expired')
                time.sleep(.1)
            output = Path(packet['output']); output.mkdir(parents=True, exist_ok=True)
            (output / 'result.yaml').write_text(yaml.safe_dump({'task_id':packet['task_id'],'worker':'codex','status':'success','summary_short':'Loopback Desktop handoff fixture','outputs':{'worker_notes':'worker_notes.md'}}),encoding='utf8')
            for name in ('worker_notes.md','stdout.log','stderr.log'):
                (output / name).write_text('Synthetic isolated Desktop handoff evidence',encoding='utf8')
            events=[{'type':'response.created','response':{'id':'fixture-response'}},
                {'type':'response.output_item.done','item':{'type':'message','role':'assistant','id':'fixture-message','content':[{'type':'output_text','text':'Synthetic worker completed'}]}},
                {'type':'response.completed','response':{'id':'fixture-response','usage':{'input_tokens':20,'output_tokens':5,'total_tokens':25,'input_tokens_details':None,'output_tokens_details':None}}}]
            data=''.join('data: '+json.dumps(e)+'\n\n' for e in events).encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    (home/'config.toml').write_text('model="gpt-6.1-sol"\nmodel_provider="fixture"\n[model_providers.fixture]\nname="Loopback fixture"\nwire_api="responses"\nrequires_openai_auth=false\nsupports_websockets=false\nrequest_max_retries=0\nstream_max_retries=0\nstream_idle_timeout_ms=620000\nbase_url="http://127.0.0.1:'+str(server.server_port)+'/v1"\n',encoding='utf8')
    real_run = subprocess.run
    def execute(argv, **kwargs):
        directory=pool.jobs/request['command_id']
        keep={'SYSTEMROOT','WINDIR','PATH','PATHEXT','TEMP','TMP','COMSPEC','SYSTEMDRIVE','USERPROFILE','APPDATA','LOCALAPPDATA','CODEX_HOME','SOLUTION_AGENT_TERMINAL_NESTING'}
        kwargs['env']={k:v for k,v in kwargs['env'].items() if k.upper() in keep}
        with (directory/'stdout.bin').open('wb') as out,(directory/'stderr.bin').open('wb') as err:
            return real_run(argv,**kwargs,stdout=out,stderr=err,timeout=650)
    try:
        receipt=pool.complete(packet,request,codex_run=execute,finish_unit=False)
        assert len(called)==1
        (root/'actual-worker-receipt.json').write_text(json.dumps({'command_id':request['command_id'],'worker_session':receipt['worker_session'],'loopback_requests':len(called)}))
        return receipt
    finally:
        server.shutdown();server.server_close()
