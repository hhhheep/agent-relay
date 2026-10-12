import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { providerConfig, providerStatus, structuredRequestBody, numericUsage, REPORT_PRESET } from '../index.mjs';
import { draftWithProvider } from '../../../plugins/research-report-writer/lib/provider.mjs';
import { loadProvider } from '../../../tools/research-background/provider.mjs';

test('environment wins over bounded env-file without leaking values', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'provider-test-'));
  try {
    const envFile = path.join(root, 'credentials.env');
    fs.writeFileSync(envFile, 'DASHSCOPE_API_KEY=file-value\nBAILIAN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1\nDEEPSEEK_MODEL=ignored\n');
    const env = { REPORT_WRITER_ENV_FILE: envFile, DASHSCOPE_API_KEY:'environment-value',
      BAILIAN_BASE_URL:'https://dashscope-intl.aliyuncs.com/compatible-mode/v1' };
    assert.equal(providerConfig(env).apiKey, 'environment-value');
    assert.equal(providerConfig(env).baseURL, env.BAILIAN_BASE_URL);
    assert.equal(providerStatus(env).credential_source, 'environment');
    assert(!JSON.stringify(providerStatus(env)).includes('environment-value'));
    assert.equal(structuredRequestBody({system:'s',user:'u',config:providerConfig(env)}).max_tokens, 8192);
    assert.equal(REPORT_PRESET.id, 'bailian-report/v1');
    assert.deepEqual(numericUsage({total_tokens:4,key:'hidden',nested:{count:2,bad:Infinity},arr:[1]}),
      {total_tokens:4,nested:{count:2}});
  } finally { fs.rmSync(root, {recursive:true,force:true}); }
});

test('Writer and Background use one preset with real SDK requests and mock transport', async () => {
  const previousFetch = globalThis.fetch;
  const previousEnv = { ...process.env };
  const bodies = [], receipts = new Map();
  process.env.DASHSCOPE_API_KEY = 'synthetic-provider-key';
  process.env.BAILIAN_BASE_URL = 'https://dashscope.aliyuncs.com/compatible-mode/v1';
  delete process.env.REPORT_WRITER_ENV_FILE;
  globalThis.fetch = async (_url, options) => {
    bodies.push(JSON.parse(options.body));
    const content = bodies.length === 1 ? JSON.stringify({report_markdown:'Grounded',claims_used:['C1'],warnings:[],unsupported_claims:[]}) : '{"documents":[]}';
    return new Response(JSON.stringify({id:'mock-response',object:'chat.completion',model:REPORT_PRESET.model,
      choices:[{index:0,message:{role:'assistant',content},finish_reason:'stop'}],
      usage:{total_tokens:8,prompt_tokens:5,completion_tokens:3,unsafe:'synthetic-provider-key'}}),
      {status:200,headers:{'content-type':'application/json'}});
  };
  try {
    const draft = await draftWithProvider({evidence:{claims:[{claim_id:'C1'}]},rules:'Synthetic rule'});
    assert.equal(draft.parsed.report_markdown, 'Grounded');
    assert.deepEqual(draft.parsed.claims_used, ['C1']);
    const background = await loadProvider({credential_env_file:undefined});
    const content = await background.generate({sources:[{source:'Sources/a.md',lines:['Observation']}],
      rules:'JSON rules',receipt:'receipt',response:'response',atomic:(key,value)=>receipts.set(key,JSON.parse(value))});
    assert.equal(content, '{"documents":[]}');
    assert.deepEqual(bodies.map(b=>b.max_tokens), [8192,4096]);
    assert.deepEqual(bodies.map(b=>b.model), [REPORT_PRESET.model,REPORT_PRESET.model]);
    assert(bodies.every(b=>b.enable_thinking===false && b.temperature===0.2));
    assert.deepEqual(bodies[1].response_format,{type:'json_object'});
    assert.equal(receipts.get('receipt').usage.total_tokens, 8);
    assert(!JSON.stringify([...receipts.values()]).includes('synthetic-provider-key'));
    // Existing explicit helper override works without making it the default authority.
    const override = await loadProvider({provider_module:fileURLToPath(new URL('../../../plugins/research-report-writer/lib/provider.mjs',import.meta.url))});
    assert.equal(override.status.model, REPORT_PRESET.model);
  } finally {
    globalThis.fetch = previousFetch;
    for (const key of Object.keys(process.env)) if (!(key in previousEnv)) delete process.env[key];
    Object.assign(process.env, previousEnv);
  }
});
