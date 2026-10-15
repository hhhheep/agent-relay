// Real stdio handshake/list/status against the generated standalone plugin.
import {Client} from '../../components/lifecycle/node_modules/@modelcontextprotocol/sdk/dist/esm/client/index.js';
import {StdioClientTransport} from '../../components/lifecycle/node_modules/@modelcontextprotocol/sdk/dist/esm/client/stdio.js';
import path from 'node:path';
import fs from 'node:fs';
import {fileURLToPath} from 'node:url';
import assert from 'node:assert/strict';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../..');
const pluginRoot=path.resolve(process.argv[2] ?? path.join(root,'dist/p1-candidate/research-report-writer'));
const cwd=path.join(root,'.verification','writer-cwd');fs.mkdirSync(cwd,{recursive:true});
const transport=new StdioClientTransport({command:process.execPath,args:[path.join(pluginRoot,'server.mjs')],cwd,
 env:{PATH:process.env.PATH ?? '',SystemRoot:process.env.SystemRoot ?? ''},stderr:'pipe'});
const client=new Client({name:'p1-standalone-check',version:'0.1.0'});
try {
 await client.connect(transport);
 const tools=await client.listTools();
 assert.deepEqual(tools.tools.map(t=>t.name).sort(),['provider_status','remote_writer_status','validate_evidence_packet','draft_research_report','validate_remote_evidence_packet','draft_remote_research_report'].sort());
 const status=await client.callTool({name:'provider_status',arguments:{}});
 assert.equal(status.isError,false);assert.equal(status.structuredContent.configured,false);
 console.log(JSON.stringify({standalone_plugin:true,arbitrary_cwd:true,tools:tools.tools.length,configured:false}));
} finally {await client.close();await transport.close();}
