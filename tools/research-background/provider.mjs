import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

// Reuse installed provider identity, endpoint validation and credential injection.
// Deliberately do not use draftWithProvider: its schema is report prose and it
// parses before returning usage. No upstream files or model defaults are changed.
export async function loadProvider(config) {
  const installed = await import(pathToFileURL(config.provider_module).href);
  const env = { ...process.env, REPORT_WRITER_ENV_FILE: config.credential_env_file };
  const status = installed.providerStatus(env);
  if (!status.configured) throw new Error('PROVIDER_NOT_CONFIGURED');
  const providerConfig = installed.providerConfig(env); // secret remains in memory
  const require = createRequire(config.provider_module);
  const OpenAI = require('openai').default;
  const client = new OpenAI({ apiKey: providerConfig.apiKey, baseURL: providerConfig.baseURL,
    maxRetries: 0, timeout: 120000 }); // queue owns finite retry policy
  return {
    status,
    async generate({ sources, rules, receipt, response, atomic, signal }) {
      const started = new Date().toISOString();
      const meta = { started_at: started, provider: providerConfig.provider,
        configured_model: providerConfig.model, status: 'requesting', usage: null,
        finish_reason: null, error: null };
      atomic(receipt, JSON.stringify(meta, null, 2));
      let completion;
      try {
        completion = await client.chat.completions.create({
          ...installed.structuredRequestBody({ system: rules,
            user: JSON.stringify({ sources:sources.map(source=>({...source,
              lines:source.lines.map((text,index)=>({line:index+1,text}))})) }), config: providerConfig }), max_tokens: 4096
        }, { signal });
      } catch (err) {
        // Do not log SDK messages, request headers, URLs or credential strings.
        atomic(receipt, JSON.stringify({ ...meta, status: 'api_error',
          finished_at: new Date().toISOString(), error: {
            code: signal?.aborted ? 'INTERRUPTED' : 'API_REQUEST_FAILED',
            http_status: Number.isInteger(err.status) ? err.status : null,
            type: ['APIError', 'APIConnectionError', 'APIConnectionTimeoutError',
              'RateLimitError', 'AuthenticationError', 'PermissionDeniedError',
              'BadRequestError', 'AbortError', 'Error', 'TypeError', 'ReferenceError'].includes(err.name) ? err.name : 'ProviderError',
            network_code: ['EACCES','EPERM','ENOTFOUND','ECONNREFUSED','ECONNRESET',
              'ETIMEDOUT','UND_ERR_CONNECT_TIMEOUT','UNABLE_TO_VERIFY_LEAF_SIGNATURE']
                .find(code => [err.code,err.cause?.code,err.cause?.cause?.code].includes(code)) ?? null,
            diagnostic: /structuredRequestBody.*not a function/.test(err.message ?? '') ? 'INSTALLED_REQUEST_HELPER_UNAVAILABLE' :
              /fetch failed|Connection error/i.test(err.message ?? '') ? 'CONNECTION_FAILED' :
              /aborted/i.test(err.message ?? '') ? 'ABORTED' : null
          } }, null, 2));
        throw new Error(signal?.aborted ? 'INTERRUPTED' : 'API_REQUEST_FAILED');
      }
      const content = completion.choices?.[0]?.message?.content ?? '';
      const numericOnly = value => {
        if (typeof value === 'number' && Number.isFinite(value)) return value;
        if (value && typeof value === 'object' && !Array.isArray(value))
          return Object.fromEntries(Object.entries(value).filter(([k]) => /^[a-z_]+$/.test(k))
            .map(([k,v]) => [k,numericOnly(v)]).filter(([,v]) => v !== undefined));
      };
      // Save secret-safe usage and finish reason BEFORE parsing generated content.
      atomic(receipt, JSON.stringify({ ...meta, status: 'received_unparsed',
        finished_at: new Date().toISOString(), returned_model: completion.model ?? null,
        response_id: completion.id ?? null, usage: numericOnly(completion.usage) ?? null,
        finish_reason: completion.choices?.[0]?.finish_reason ?? null }, null, 2));
      atomic(response, JSON.stringify({ content }));
      return content;
    }
  };
}
