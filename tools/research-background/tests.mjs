import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { Engine, readConfig, sha, atomic, acquireLock } from './service.mjs';

const home = fileURLToPath(new URL('.',import.meta.url));
const runs = path.join(home,'test-runs'); fs.mkdirSync(runs,{recursive:true});
const fixture = (options = {}) => {
  const base = fs.mkdtempSync(path.join(runs,'case-'));
  fs.mkdirSync(path.join(base,'vault','Sources'),{recursive:true});
  const c = {...readConfig(path.join(home,'config.json')),vault:path.join(base,'vault'),state:path.join(base,'state'),
    output:'Generated/Summaries',exclude_roots:[],stable_seconds:1,retry_seconds:1,...options};
  let clock=Date.now()+10000, calls=0;
  const good = sources=>JSON.stringify({documents:sources.map(source=>{
    const index = source.lines.findIndex(line=>line.trim()),quote=source.lines[index];
    const item={text:'來源報告的合成觀察；仍待審閱。',line_start:index+1,line_end:index+1,quote};
    return {source:source.source,sha256:source.sha256,summary:[item],observations:[item],limitations:[],questions:[{...item,text:'PROPOSED：合成研究問題？'}]};
  })});
  const generate = async ({sources,receipt,response,atomic})=>{
    calls++; const content=good(sources);
    atomic(receipt,JSON.stringify({status:'received_unparsed',usage:{total_tokens:12},finish_reason:'stop'}));
    atomic(response,JSON.stringify({content})); return content;
  };
  const engine = new Engine(c,{generate,now:()=>clock});
  engine.baseline();
  const write=(source,text='合成來源：觀測值 3；沒有獨立驗證。\n')=>{
    atomic(path.join(c.vault,source),text);return engine.read(source);
  };
  const scanReady=()=>{engine.scan();clock+=1100;engine.scan();};
  return {base,c,engine,write,scanReady,advance:ms=>clock+=ms,calls:()=>calls,good,generate,now:()=>clock};
};

test('baseline never uploads old material; new file waits for stability; same SHA deduplicates; update makes version',async()=>{
  const f=fixture();f.write('Sources/a.md');f.engine.scan();assert.deepEqual(f.engine.status().counts,{});
  assert.equal(await f.engine.runBatch(),0);f.advance(1100);f.engine.scan();
  await f.engine.runBatch();assert.equal(f.calls(),1);assert.equal(f.engine.status().counts.success,1);
  f.scanReady();await f.engine.runBatch();assert.equal(f.calls(),1);
  f.write('Sources/a.md','更新：觀測值 4，仍是合成來源。\n');f.scanReady();await f.engine.runBatch();
  assert.equal(f.calls(),2);assert.equal(f.engine.status().counts.success,2);
  const jobs=f.engine.db.prepare('SELECT * FROM jobs').all();
  for(const job of jobs){assert(f.engine.complete(job));
    const artifact=JSON.parse(fs.readFileSync(path.join(f.c.vault,job.output,'summary.json')));
    assert.equal(artifact.sha256,job.sha);assert(artifact.summary[0].line_start>=1);
  }f.engine.close();
});

test('writing resets stable interval; submission rechecks a changed file',async()=>{
  const f=fixture();f.write('Sources/a.md');f.engine.scan();f.advance(900);
  f.write('Sources/a.md','還在寫入\n');f.engine.scan();f.advance(900);f.engine.scan();
  assert.equal(await f.engine.runBatch(),0);f.advance(1100);f.engine.scan();
  f.write('Sources/a.md','排隊後又修改\n');assert.equal(await f.engine.runBatch(),0);
  assert.equal(f.calls(),0);assert.equal(f.engine.status().counts.failed,1);f.engine.close();
});

test('matching complete delivery has priority; one incorrect SHA holds entire batch; incomplete manifest holds ordinary scan',async()=>{
  const f=fixture();const a=f.write('Sources/a.md'),b=f.write('Sources/b.md');
  const file=path.join(f.c.vault,'Sources/batch.delivery.json');
  const manifest={schema:'research-delivery/v1',status:'complete',files:[{path:a.source,sha256:a.sha256},{path:b.source,sha256:'0'.repeat(64)}]};
  atomic(file,JSON.stringify(manifest));f.scanReady();assert.equal(await f.engine.runBatch(),0);
  assert(f.engine.status().warnings.some(w=>w.code==='DELIVERY_SHA_MISMATCH'));
  manifest.files[1].sha256=b.sha256;manifest.status='copying';atomic(file,JSON.stringify(manifest));
  f.scanReady();assert.equal(await f.engine.runBatch(),0);
  manifest.status='complete';atomic(file,JSON.stringify(manifest));f.engine.scan();
  assert.equal(await f.engine.runBatch(),2);assert.equal(f.calls(),1);
  assert.equal(f.engine.status().counts.success,2);f.engine.close();
});

test('baseline old manifests do not trigger old deliveries',async()=>{
  const f=fixture();f.engine.close();fs.rmSync(path.join(f.c.state,'jobs.sqlite'));
  const e=new Engine(f.c,{generate:f.generate});const doc=f.write('Sources/a.md');
  atomic(path.join(f.c.vault,'Sources/a.delivery.json'),JSON.stringify({schema:'research-delivery/v1',status:'complete',files:[{path:doc.source,sha256:doc.sha256}]}));
  e.baseline();e.scan();assert.equal(await e.runBatch(),0);assert.equal(f.calls(),0);e.close();
});

test('API failures persist and retry with bounded exponential backoff',async()=>{
  const f=fixture();f.write('Sources/a.md');f.scanReady();let calls=0;
  f.engine.generate=async({receipt,atomic})=>{calls++;atomic(receipt,JSON.stringify({status:'api_error',usage:null,finish_reason:null,error:{code:'API_REQUEST_FAILED'}}));throw new Error('API_REQUEST_FAILED');};
  await f.engine.runBatch();assert.equal(calls,1);await f.engine.runBatch();assert.equal(calls,1);
  f.advance(1000);await f.engine.runBatch();assert.equal(calls,2);
  f.advance(1000);await f.engine.runBatch();assert.equal(calls,2);
  f.advance(1000);await f.engine.runBatch();assert.equal(calls,3);
  f.advance(99999);await f.engine.runBatch();assert.equal(calls,3);
  const job=f.engine.db.prepare('SELECT * FROM jobs').get();assert.equal(job.status,'failed');assert.equal(job.retry_at,null);
  f.engine.close();const e=new Engine(f.c);assert.equal(e.status().counts.failed,1);e.close();
});

test('JSON parse and locator validation errors keep usage/finish reason before finite retry',async()=>{
  const f=fixture();f.write('Sources/a.md');f.scanReady();
  f.engine.generate=async({receipt,response,atomic})=>{
    atomic(receipt,JSON.stringify({usage:{total_tokens:99},finish_reason:'length',status:'received_unparsed'}));
    atomic(response,JSON.stringify({content:'{broken'}));return '{broken';
  };
  await f.engine.runBatch();const job=f.engine.db.prepare('SELECT * FROM jobs').get();
  const meta=JSON.parse(fs.readFileSync(path.join(f.c.state,job.attempt_dir,'provider.json')));
  assert.equal(meta.usage.total_tokens,99);assert.equal(meta.finish_reason,'length');assert.equal(meta.status,'parse_error');
  f.advance(1000);
  f.engine.generate=async({sources,receipt,response,atomic})=>{
    const value=JSON.parse(f.good(sources));value.documents[0].summary[0].line_start=999;
    const content=JSON.stringify(value);atomic(receipt,JSON.stringify({usage:{total_tokens:55},status:'received_unparsed'}));
    atomic(response,JSON.stringify({content}));return content;
  };
  await f.engine.runBatch();assert.equal(f.engine.status().counts.failed,1);assert.equal(f.engine.status().counts.success,undefined);
  f.engine.close();
});

test('own output nested under Sources, binary, temporary, canvas and obsidian content never trigger',async()=>{
  const f=fixture({output:'Sources/generated'});f.write('Sources/a.md');
  atomic(path.join(f.c.vault,'Sources/a.part.md'),'partial');
  atomic(path.join(f.c.vault,'Sources/.obsidian/x.md'),'private');
  atomic(path.join(f.c.vault,'Sources/x.canvas'),'{}');
  atomic(path.join(f.c.vault,'Sources/binary.txt'),Buffer.from([0,1,2]));
  f.scanReady();await f.engine.runBatch();assert.equal(f.calls(),1);
  f.scanReady();await f.engine.runBatch();assert.equal(f.calls(),1);f.engine.close();
});

test('completed artifact before status commit recovers without new API request; saved response recovers missing artifacts',async()=>{
  const f=fixture();f.write('Sources/a.md');f.scanReady();await f.engine.runBatch();
  const job=f.engine.db.prepare('SELECT * FROM jobs').get();
  f.engine.db.prepare("UPDATE jobs SET status='processing' WHERE id=?").run(job.id);
  f.engine.close();const e=new Engine(f.c,{generate:f.generate});e.recover();assert.equal(e.status().counts.success,1);
  assert.equal(await e.runBatch(),0);assert.equal(f.calls(),1);
  fs.unlinkSync(path.join(f.c.vault,job.output,'complete.json'));
  e.db.prepare("UPDATE jobs SET status='processing' WHERE id=?").run(job.id);e.recover();
  assert(e.complete(job));assert.equal(f.calls(),1);e.close();
});

test('UTF-8 BOM and CRLF raw version hashes are preserved',async()=>{
  const f=fixture();f.write('Sources/a.md','\uFEFF合成來源\r\n觀測值 3\r\n');f.scanReady();await f.engine.runBatch();
  const job=f.engine.db.prepare('SELECT * FROM jobs').get();
  assert.equal(sha(fs.readFileSync(path.join(f.c.vault,job.output,'source.txt'))),job.sha);f.engine.close();
});

test('backfill is explicit, bounded and confined to configured sources',async()=>{
  const f=fixture();f.write('Sources/a.md');assert.throws(()=>f.engine.backfill(['Sources/a.md'],0));
  assert.throws(()=>f.engine.backfill(['../secret.md'],1));
  f.engine.backfill(['Sources/a.md'],1);await f.engine.runBatch();assert.equal(f.calls(),1);f.engine.close();
});

test('unchanged oversized baseline stays quiet; new oversized version asks for review without truncation or API',async()=>{
  const f=fixture({max_source_bytes:32});
  const source='Sources/large.md',file=path.join(f.c.vault,source),raw='長文件測試'.repeat(100);
  atomic(file,raw);f.engine.db.prepare('INSERT INTO baseline VALUES (?,?)').run(source,sha(Buffer.from(raw)));
  f.engine.scan();assert.equal(f.engine.status().warnings.length,0);
  atomic(file,raw+'更新');f.engine.scan();assert(f.engine.status().warnings.some(w=>w.code==='SOURCE_TOO_LARGE'));
  assert.equal(await f.engine.runBatch(),0);assert.equal(f.calls(),0);f.engine.close();
});

test('SQLite singleton forbids second worker and releases after abrupt process kill; interrupted work resumes',async()=>{
  const f=fixture();f.write('Sources/a.md');f.scanReady();f.engine.close();
  const config=path.join(f.base,'config.json');atomic(config,JSON.stringify(f.c));
  const child=spawn(process.execPath,['--disable-warning=ExperimentalWarning',path.join(home,'worker-fixture.mjs'),config],{windowsHide:true,stdio:['ignore','pipe','pipe']});
  await new Promise((resolve,reject)=>{
    let output='';child.stdout.on('data',data=>{output+=data;if(output.includes('PROCESSING'))resolve();});
    child.on('error',reject);child.on('exit',code=>reject(new Error('child exited '+code)));
    setTimeout(()=>reject(new Error('child readiness timeout')),10000).unref();
  });
  assert.throws(()=>acquireLock(f.c.state),/ALREADY_RUNNING/);
  const exit=new Promise(resolve=>child.once('exit',resolve));child.kill('SIGKILL');await exit;
  const release=acquireLock(f.c.state),e=new Engine(f.c,{generate:f.generate,now:f.now});
  assert.equal(e.status().counts.processing,1);e.recover();assert.equal(e.status().counts.pending,1);
  await e.runBatch();assert.equal(e.status().counts.success,1);
  assert.equal(e.db.prepare('SELECT attempts FROM jobs').get().attempts,2);e.close();release();
});
