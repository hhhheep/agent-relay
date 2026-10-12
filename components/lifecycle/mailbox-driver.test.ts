import test from 'node:test';import assert from 'node:assert/strict';
import {mkdtempSync,rmSync,readFileSync} from 'node:fs';import {tmpdir} from 'node:os';import {join,resolve} from 'node:path';import {randomBytes} from 'node:crypto';import {execFileSync} from 'node:child_process';
import {startServer} from '../xats-routing-lab/upstream/src/daemon/server.ts';
import {connectStableInbox} from './stable-inbox.ts';import {runMailbox} from './mailbox-driver.ts';

test('real Python mailbox and XATS driver deliver question and retain received answer across restart',async()=>{
 const root=mkdtempSync(join(tmpdir(),'mailbox-driver-')),token=randomBytes(32).toString('hex');
 const server=await startServer({dbPath:join(root,'xats.sqlite'),host:'127.0.0.1',port:0,token});let manager:any;
 const url=`http://127.0.0.1:${server.port}/mcp`,python=resolve(process.platform === 'win32' ? '../../.venv/Scripts/python.exe' : '../../.venv/bin/python');
 const binding={task_id:'task',work_unit_id:'unit',worker_id:'worker',origin_chat_id:'publisher-chat'},mailbox=join(root,'mailbox.sqlite');
 const run=(program:string)=>JSON.parse(execFileSync(python,['-c',program,mailbox,JSON.stringify(binding)],{encoding:'utf8',windowsHide:true}));
 try{
  const delivery=run("import sys,json;from solution_agent.harness.help_queue import HelpQueue;q=HelpQueue(sys.argv[1]);q.bind(json.loads(sys.argv[2]));print(json.dumps(q.ask('question','blocked','actual check')));q.close()");
  manager=await connectStableInbox(join(root,'manager.sqlite'),{url,token,name:'manager',team:'mailbox-test'});
  const config={stateRoot:join(root,'worker'),url,name:'worker',team:'mailbox-test',managerSource:manager.agentId,python,rpcScript:resolve('mailbox-rpc.py'),mailbox,binding};
  const env={HARNESS_XATS_TOKEN:token},signal=new AbortController().signal;
  await runMailbox(config,env,signal,true);
  const statuses=await Promise.allSettled([
   runMailbox({...config,stateRoot:join(root,'bad'),name:'bad',rpcScript:join(root,'missing.py')},env,signal,true),
   runMailbox(config,env,signal,true)
  ]);
  assert.equal(statuses[0].status,'rejected');assert.equal(statuses[1].status,'fulfilled');
  const bad=JSON.parse(readFileSync(join(root,'bad/status.json'),'utf8'));
  assert.equal(bad.failures,1);assert.equal(bad.errorCode,'mailbox_operation_failed');
  const received=(await manager.get(0)).messages;assert.equal(received.length,1);
  const envelope=JSON.parse(received[0].body);assert.equal(envelope.delivery_id,delivery);assert.deepEqual(envelope.binding,binding);
  const workerId=received[0].from_agent_id;
  await manager.send(workerId,JSON.stringify({kind:'help_accepted',durably_received:true,delivery_id:delivery,origin_chat_id:binding.origin_chat_id}));
  const answer=JSON.stringify({kind:'awaiting_verification',request_id:delivery});
  await manager.send(workerId,JSON.stringify({binding,delivery_id:delivery,answer}));
  await runMailbox(config,env,signal,true);await runMailbox(config,env,signal,true);
  const status=run("import sys,json;from solution_agent.harness.help_queue import HelpQueue;q=HelpQueue(sys.argv[1]);print(json.dumps(q.status('question')));q.close()");
  assert.equal(status.state,'answered');assert.equal(status.answer,answer);
  const final=(await manager.get(0)).messages;assert.equal(final.length,2);
  const saved=JSON.parse(final[1].body);assert.equal(saved.kind,'help_answer_saved');assert.equal(saved.delivery_id,delivery);
  await manager.send(workerId,JSON.stringify({...saved,kind:'help_answer_confirmed'}));
  await runMailbox(config,env,signal,true);
  assert.equal(run("import sys,json,sqlite3;c=sqlite3.connect(sys.argv[1]);print(json.dumps(c.execute('SELECT confirmed FROM channel_saved_answers').fetchone()[0]));c.close()"),1);
 }finally{await manager?.close();await server.app.close();rmSync(root,{recursive:true,force:true});}
});
