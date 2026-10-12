import {build} from '../../components/lifecycle/node_modules/esbuild/lib/main.js';
import {mkdirSync,writeFileSync} from 'node:fs';
import {resolve,dirname} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';
const root=resolve(dirname(fileURLToPath(import.meta.url)),'../..');
const output=resolve(root,'.verification/build');mkdirSync(output,{recursive:true});
const vendorRequire=createRequire(resolve(root,'components/xats-routing-lab/upstream/package.json'));
const native=vendorRequire('better-sqlite3');
const db=new native(':memory:');db.exec('CREATE TABLE smoke(value INTEGER); INSERT INTO smoke VALUES(1)');
if(db.prepare('SELECT value FROM smoke').get().value!==1)throw Error('native_sqlite_failed');db.close();
const result=await build({absWorkingDir:root,entryPoints:['components/lifecycle/production-xats-service-20261008.ts'],
 outfile:resolve(output,'xats-service.mjs'),bundle:true,platform:'node',target:'node24',format:'esm',metafile:true,
 plugins:[{name:'project-native-sqlite',setup(b){b.onResolve({filter:/^better-sqlite3$/},()=>({path:pathToFileURL(vendorRequire.resolve('better-sqlite3')).href,external:true}));}}],
 banner:{js:"import {createRequire as __createRequire} from 'node:module';const require=__createRequire(import.meta.url);const __filename=import.meta.filename;const __dirname=import.meta.dirname;"}});
writeFileSync(resolve(output,'xats-metafile.json'),JSON.stringify(result.metafile,null,2));
console.log(JSON.stringify({native_sqlite:'passed_in_memory',service:'built_without_starting',inputs:Object.keys(result.metafile.inputs).length,output}));
