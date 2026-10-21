import {readFileSync,realpathSync,existsSync,statSync} from 'node:fs';
import {createHash} from 'node:crypto';
import {isAbsolute,resolve} from 'node:path';
import type {PersistentHostConfig} from './persistent-harness-ipc.ts';
import {persistentWindowsShell} from './persistent-harness-launch.ts';

export type RuntimePins={schema:'persistent-runtime-pins/v1',frontendPath:string,files:{path:string,sha256:string}[],environmentSha256:string};
const sha=(v:string|Buffer)=>createHash('sha256').update(v).digest('hex');
const canonical=(p:string)=>realpathSync.native(p);
const environmentKeys=['PATH','Path','PYTHONHOME','PYTHONPATH','PYTHONSAFEPATH','PYTHONNOUSERSITE','NODE_OPTIONS','NODE_PATH','LOCALAPPDATA','XDG_STATE_HOME','USERPROFILE','HOME','SystemRoot','COMSPEC','TEMP','TMP'];
export function persistentHostEnvironment(c:PersistentHostConfig,env:NodeJS.ProcessEnv):NodeJS.ProcessEnv{
 const result={...env},key=(v:string)=>process.platform==='win32'?v.toUpperCase():v;
 const allowed=new Set(environmentKeys.map(key));
 if(c.environment!==undefined&&(!c.environment||typeof c.environment!=='object'||Array.isArray(c.environment)||Object.entries(c.environment).some(([name,value])=>!allowed.has(key(name))||(value!==null&&(typeof value!=='string'||value.length>32768)))))throw Error('persistent_host_environment_invalid');
 const seen=new Set<string>();
 for(const [name,value] of Object.entries(c.environment??{})){
  if(seen.has(key(name)))throw Error('persistent_host_environment_invalid');seen.add(key(name));
  for(const original of Object.keys(result))if(key(original)===key(name))delete result[original];
  if(value!==null)result[name]=value;
 }
 return result;
}
const environmentDigest=(c:PersistentHostConfig,env:NodeJS.ProcessEnv)=>{
 env=persistentHostEnvironment(c,env);
 const declared=JSON.parse(readFileSync(c.installedBackendConfig,'utf8')).mcpServers?.solution_agent_harness;
 // Python's Windows environment mapping uppercases names; Windows treats them as
 // case-insensitive. Hash the effective values, not parent/child spelling differences.
 const key=(v:string)=>process.platform==='win32'?v.toUpperCase():v;
 const effective=new Map<string,string|undefined>();
 for(const entries of [env,declared?.env??{}])for(const [name,value] of Object.entries(entries))effective.set(key(name),value as string|undefined);
 return sha(JSON.stringify([...new Set([...environmentKeys,...Object.keys(declared?.env??{})].map(key))].sort().map(k=>[k,effective.get(k)??null])));
};
function requiredFiles(c:PersistentHostConfig,hostEntry:string,guardian:string,node:string,frontend:string){
 const declared=JSON.parse(readFileSync(c.installedBackendConfig,'utf8')).mcpServers?.solution_agent_harness;
 if(!declared||!isAbsolute(declared.command)||!Array.isArray(declared.args)||!isAbsolute(declared.cwd)||!isAbsolute(c.guardianPython??declared.command))throw Error('persistent_absolute_launch_paths_required');
 // Source-mode imports can change independently. Sealed manual hosts require the bundled entry.
 if(!hostEntry.endsWith('.mjs')||!frontend.endsWith('.mjs'))throw Error('persistent_bundled_runtime_required');
 const files=[hostEntry,guardian,node,frontend,c.installedBackendConfig,c.guardianPython??declared.command,declared.command,c.owner.appServerPath,c.owner.workplaceIdentityPath];
 if(process.platform==='win32')files.push(persistentWindowsShell());
 for(const arg of declared.args)if(typeof arg==='string'&&existsSync(resolve(declared.cwd,arg))&&statSync(resolve(declared.cwd,arg)).isFile())files.push(resolve(declared.cwd,arg));
 return [...new Set(files.map(canonical))].sort();
}
// Explicit preparation only: never seal or rewrite pins as part of start/attach/retry.
export function sealPersistentHost(c:PersistentHostConfig,paths:{hostEntry:string,guardian:string,node:string,frontend:string},env:NodeJS.ProcessEnv=process.env):RuntimePins{
 return {schema:'persistent-runtime-pins/v1',frontendPath:canonical(paths.frontend),files:requiredFiles(c,paths.hostEntry,paths.guardian,paths.node,paths.frontend).map(path=>({path,sha256:sha(readFileSync(path))})),environmentSha256:environmentDigest(c,env)};
}
export function verifyPersistentRuntime(c:PersistentHostConfig,paths:{hostEntry:string,guardian:string,node:string},env:NodeJS.ProcessEnv=process.env){
 const pins=c.runtimePins;
 if(pins?.schema!=='persistent-runtime-pins/v1'||!Array.isArray(pins.files)||pins.files.length>4096||!pins.frontendPath||pins.environmentSha256!==environmentDigest(c,env))throw Error('persistent_runtime_pins_required_or_environment_changed');
 const files=new Map<string,string>();
 for(const file of pins.files){
  if(!isAbsolute(file.path)||!/^[a-f0-9]{64}$/.test(file.sha256))throw Error('persistent_runtime_pin_invalid');
  const path=canonical(file.path);if(files.has(path)||sha(readFileSync(path))!==file.sha256)throw Error('persistent_runtime_file_changed');files.set(path,file.sha256);
 }
 for(const path of requiredFiles(c,paths.hostEntry,paths.guardian,paths.node,pins.frontendPath))if(!files.has(path))throw Error('persistent_runtime_pin_missing');
 // Includes tokens only as digest input, never prints them. Active controller binding is separately CAS-protected.
 return sha(JSON.stringify({config:c,files:[...files].sort(([a],[b])=>a.localeCompare(b)),node:process.version,platform:process.platform,arch:process.arch}));
}
