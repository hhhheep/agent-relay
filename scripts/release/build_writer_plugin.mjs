// Generated plugin closure: shared provider source remains the only authority.
import { build } from '../../components/lifecycle/node_modules/esbuild/lib/main.js';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import crypto from 'node:crypto';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../..');
const source=path.join(root,'plugins/research-report-writer');
const out=path.join(root,'dist/p1-candidate/research-report-writer');
if(path.relative(root,out)!==path.join('dist','p1-candidate','research-report-writer'))throw Error('unsafe_generated_output');
if(fs.existsSync(out)&&fs.realpathSync(out)!==out)throw Error('generated_output_link_not_allowed');
fs.rmSync(out,{recursive:true,force:true});fs.mkdirSync(out,{recursive:true});
const result=await build({absWorkingDir:root,
  entryPoints:['plugins/research-report-writer/server.mjs','plugins/research-report-writer/lib/provider.mjs','plugins/research-report-writer/lib/core.mjs'],
  outbase:'plugins/research-report-writer',outdir:out,bundle:true,splitting:true,platform:'node',target:'node24',format:'esm',metafile:true,
  entryNames:'[dir]/[name]',chunkNames:'chunks/[name]-[hash]',outExtension:{'.js':'.mjs'},
  banner:{js:"import {createRequire as __createRequire} from 'node:module';const require=__createRequire(import.meta.url);"}});
// Copy only Git candidates. Never copy node_modules, local profiles or receipts.
import {execFileSync} from 'node:child_process';
const candidates=execFileSync('git',['ls-files','--cached','--others','--exclude-standard','-z'],{cwd:root}).toString().split('\0').filter(Boolean);
for(const name of candidates){
  if(!name.startsWith('plugins/research-report-writer/'))continue;
  const relative=name.slice('plugins/research-report-writer/'.length);
  if(!(relative.startsWith('.codex-plugin/')||relative.startsWith('skills/')||relative.startsWith('remote/')||relative.startsWith('examples/')||relative==='.mcp.json'))continue;
  const target=path.join(out,relative);fs.mkdirSync(path.dirname(target),{recursive:true});fs.copyFileSync(path.join(root,name),target);
}
fs.writeFileSync(path.join(out,'package.json'),JSON.stringify({name:'research-report-writer',version:'0.1.0',private:true,type:'module',engines:{node:'>=24'}},null,2)+'\n');
const sha=file=>crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const sources={},licenses=new Set();
for(const name of Object.keys(result.metafile.inputs)){
  const input=path.resolve(root,name);sources[name.replaceAll('\\','/')]=sha(input);
  if(!input.includes('node_modules')&&!input.includes(path.join('.deps','node-packages')))continue;
  let dir=path.dirname(input);
  while(dir!==path.dirname(dir)&&!fs.existsSync(path.join(dir,'package.json')))dir=path.dirname(dir);
  const info=JSON.parse(fs.readFileSync(path.join(dir,'package.json'),'utf8'));
  if(!info.name||licenses.has(info.name+'@'+info.version))continue;
  const files=fs.readdirSync(dir).filter(n=>/^(license|copying)(\.|$)/i.test(n));
  if(!files.length)throw Error('dependency_license_missing:'+info.name);
  const target=path.join(out,'licenses',info.name.replaceAll('/','_')+'-'+info.version);fs.mkdirSync(target,{recursive:true});
  for(const name of files)fs.copyFileSync(path.join(dir,name),path.join(target,name));
  licenses.add(info.name+'@'+info.version);
}
const files={};function walk(dir){for(const e of fs.readdirSync(dir,{withFileTypes:true})){const file=path.join(dir,e.name);if(e.isDirectory())walk(file);else files[path.relative(out,file).replaceAll('\\','/')]=sha(file);}}walk(out);
fs.writeFileSync(path.join(out,'build-manifest.json'),JSON.stringify({status:'CANDIDATE',sources,dependencies:[...licenses],files},null,2)+'\n');
console.log(JSON.stringify({plugin:'research-report-writer',files:Object.keys(files).length,dependencies:licenses.size}));
