import {build} from 'esbuild';
import {mkdirSync,readFileSync,writeFileSync,copyFileSync,existsSync,readdirSync} from 'node:fs';
import {resolve,dirname,relative,join} from 'node:path';import {fileURLToPath} from 'node:url';import {createHash} from 'node:crypto';
const root=dirname(fileURLToPath(import.meta.url)),workspace=resolve(root,'../..'),sourceRoot=workspace;
const out=resolve(root,'packages','router-'+new Date().toISOString().replace(/[:.]/g,'-'));mkdirSync(out,{recursive:true});
const sha=p=>createHash('sha256').update(readFileSync(p)).digest('hex');
const result=await build({absWorkingDir:root,entryPoints:['manager-cli.ts','mailbox-cli.ts','publish-cli.ts','session-lifecycle-cli.ts','controller-cli.ts','router-api.ts'],outdir:out,outExtension:{'.js':'.mjs'},bundle:true,platform:'node',target:'node24',format:'esm',metafile:true,
 banner:{js:"import { createRequire as __createRequire } from 'node:module'; const require = __createRequire(import.meta.url);"}});
const sources={},packages=new Map();
for(const name of Object.keys(result.metafile.inputs)){
 const path=resolve(root,name);sources[relative(workspace,path).replaceAll('\\','/')]=sha(path);
 if(!path.includes('node_modules')&&!path.includes(join('.deps','node-packages')))continue;
 let directory=dirname(path);
 while(directory!==dirname(directory)&&(!existsSync(join(directory,'package.json'))||!JSON.parse(readFileSync(join(directory,'package.json'),'utf8')).name))directory=dirname(directory);
 const info=JSON.parse(readFileSync(join(directory,'package.json'),'utf8'));
 if(packages.has(info.name+'@'+info.version))continue;
 const licenses=readdirSync(directory).filter(n=>/^(license|copying)(\.|$)/i.test(n));
 if(!licenses.length)throw Error('dependency_license_missing:'+info.name);
 const dest=join(out,'licenses',info.name.replaceAll('/','_')+'-'+info.version);mkdirSync(dest,{recursive:true});
 for(const name of licenses)copyFileSync(join(directory,name),join(dest,name));
 packages.set(info.name+'@'+info.version,{name:info.name,version:info.version,license:info.license,licenses:licenses.map(n=>relative(out,join(dest,n)).replaceAll('\\','/'))});
}
for(const name of ['PAPERCLIP-LICENSE','provenance.json']){mkdirSync(join(out,'licenses','paperclip'),{recursive:true});copyFileSync(resolve(root,'../goal-routing-integration/vendor',name),join(out,'licenses','paperclip',name));}
const python=['mailbox-rpc.py','resume-rpc.py','md-plan-rpc.py','worker-md-allowlist.json',...['help_queue','help_channel','help_resume','verification_runner','publication_registry','md_snapshot','task_md_sync'].map(n=>'solution_agent/harness/'+n+'.py'),...JSON.parse(readFileSync(join(root,'worker-md-allowlist.json'),'utf8')).files];
for(const name of python){const target=join(out,name);mkdirSync(dirname(target),{recursive:true});const input=join(name.startsWith('solution_agent/')||name.startsWith('plugins/')?sourceRoot:root,name);copyFileSync(input,target);sources[relative(workspace,input).replaceAll('\\','/')]=sha(input);}
for(const name of ['session-lifecycle-hook.cmd']){copyFileSync(join(root,name),join(out,name));sources[relative(workspace,join(root,name)).replaceAll('\\','/')]=sha(join(root,name));}
// This helper-only distribution is a separate directory, never a runtime overlay.
// Full Harness __init__ eagerly imports unrelated runtime components.
for(const name of ['solution_agent/__init__.py','solution_agent/harness/__init__.py'])writeFileSync(join(out,name),'"""Isolated routing helper namespace; never overlay onto a Harness runtime."""\n');
writeFileSync(join(out,'package.json'),JSON.stringify({name:'harness-routing-candidate',version:'0.0.0-preview',private:true,type:'module',engines:{node:'>=24'}},null,2));
writeFileSync(join(out,'SCOPE.md'),'Candidate routing components only. No credentials, runtime state, XATS server, native App tools server, or Harness runtime included. Requires declared installed services. Generated Python namespace initializers intentionally expose helpers only. Install in a separate directory; NEVER overlay these files onto a Harness runtime. Not approved for formal deployment.\n');
const files={};const walk=dir=>{for(const e of readdirSync(dir,{withFileTypes:true})){const path=join(dir,e.name);if(e.isDirectory())walk(path);else files[relative(out,path).replaceAll('\\','/')]=sha(path);}};walk(out);
writeFileSync(join(out,'manifest.json'),JSON.stringify({scope:'isolated routing component package; full deployment acceptance pending',sources,dependencies:[...packages.values()],files},null,2));
writeFileSync(join(root,'router-package-latest.json'),JSON.stringify({directory:out,manifest_sha256:sha(join(out,'manifest.json'))},null,2));console.log(JSON.stringify({directory:out,files:Object.keys(files).length,dependencies:packages.size}));
