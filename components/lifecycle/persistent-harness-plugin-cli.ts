import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import {StdioServerTransport} from '@modelcontextprotocol/sdk/server/stdio.js';
import {createPersistentReader,createDesktopPersistentReader} from './persistent-harness-reader.ts';
import {persistentRemotePort,validateIpc} from './persistent-harness-ipc.ts';

const c=JSON.parse(readFileSync(process.argv[2],'utf8'));validateIpc(c);
if(c.adminToken!==undefined)throw Error('plugin_configuration_must_not_contain_admin_token');
const sha=(path:string)=>createHash('sha256').update(readFileSync(path)).digest('hex');
if(c.frontendSha256!==sha(fileURLToPath(import.meta.url))||c.nodeSha256!==sha(process.execPath))throw Error('persistent_frontend_runtime_mismatch');
const port=persistentRemotePort(c);
try{
 const reader=process.env.CODEX_THREAD_ID?await createPersistentReader(port,process.env):await createDesktopPersistentReader(port,process.env);
 let closing=false;const close=async()=>{if(closing)return;closing=true;await reader.close();await port.close();};
 process.stdin.once('end',()=>{void close();});process.once('SIGINT',()=>{void close();});process.once('SIGTERM',()=>{void close();});
 await reader.server.connect(new StdioServerTransport());
}catch(error){await port.close();throw error;}
