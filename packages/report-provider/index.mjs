import fs from "node:fs";
import path from "node:path";
import preset from './profiles/report-v1.json' with { type: 'json' };
export const REPORT_PRESET = Object.freeze({...preset, response_format:Object.freeze({...preset.response_format})});
export async function createProviderClient(config, options = {}) {
  const { default: OpenAI } = await import('openai');
  return new OpenAI({ apiKey: config.apiKey, baseURL: config.baseURL, ...options });
}
export function numericUsage(value) {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (value && typeof value === 'object' && !Array.isArray(value))
    return Object.fromEntries(Object.entries(value).filter(([k]) => /^[a-z_]+$/.test(k))
      .map(([k,v]) => [k,numericUsage(v)]).filter(([,v]) => v !== undefined));
}
export function safeProviderStatus(env = process.env) {
  const hasApiKey = Boolean(env.DASHSCOPE_API_KEY);
  const hasBaseURL = Boolean(env.BAILIAN_BASE_URL);
  return {
    provider: "bailian",
    provider_name: "bailian_openai_compatible",
    configured: hasApiKey && hasBaseURL,
    has_api_key: hasApiKey,
    has_base_url: hasBaseURL,
    has_model: true,
    model: REPORT_PRESET.model,
    model_family: REPORT_PRESET.model_family,
    api_style: "openai_compatible_chat",
    thinking_mode: "disabled",
    fixed_profile: true
  };
}

function readBailianSettingsFromEnvFile(env) {
  const rawPath = (env.REPORT_WRITER_ENV_FILE ?? "").trim();
  if (!rawPath) return {};
  const envPath = path.resolve(rawPath);
  const stat = fs.statSync(envPath);
  if (!stat.isFile() || stat.size > 64 * 1024) {
    throw new Error("REPORT_WRITER_ENV_FILE must be a file no larger than 64 KiB");
  }
  const selected = {};
  for (const rawLine of fs.readFileSync(envPath, "utf8").split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    const separator = line.indexOf("=");
    const key = line.slice(0, separator).trim();
    if (!new Set(["DASHSCOPE_API_KEY", "BAILIAN_BASE_URL"]).has(key)) continue;
    const value = line.slice(separator + 1).trim().replace(/^(['"])(.*)\1$/, "$2");
    if (value) selected[key] = value;
  }
  return selected;
}

function resolvedProviderEnv(env = process.env) {
  const fromFile = readBailianSettingsFromEnvFile(env);
  return {
    ...env,
    DASHSCOPE_API_KEY: env.DASHSCOPE_API_KEY || fromFile.DASHSCOPE_API_KEY,
    BAILIAN_BASE_URL: env.BAILIAN_BASE_URL || fromFile.BAILIAN_BASE_URL
  };
}

function validateBailianBaseURL(raw) {
  let parsed;
  try {
    parsed = new URL(raw);
  } catch {
    throw new Error("BAILIAN_BASE_URL is not a valid URL");
  }
  const host = parsed.hostname.toLowerCase();
  const sharedHosts = new Set([
    "dashscope.aliyuncs.com",
    "dashscope-intl.aliyuncs.com",
    "dashscope-us.aliyuncs.com",
    "cn-hongkong.dashscope.aliyuncs.com"
  ]);
  if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error("BAILIAN_BASE_URL must be credential-free HTTPS without query or fragment");
  }
  if (!host.endsWith(".maas.aliyuncs.com") && !sharedHosts.has(host)) {
    throw new Error("BAILIAN_BASE_URL must use an official Bailian workspace or shared host");
  }
  const normalizedPath = parsed.pathname.replace(/\/+$/, "");
  if (normalizedPath !== "/compatible-mode/v1") {
    throw new Error("BAILIAN_BASE_URL must end with /compatible-mode/v1");
  }
  return `${parsed.origin}${normalizedPath}`;
}

export function providerStatus(env = process.env) {
  const resolved = resolvedProviderEnv(env);
  let baseURLValid = false;
  let configurationError = null;
  if (resolved.BAILIAN_BASE_URL) {
    try {
      validateBailianBaseURL(resolved.BAILIAN_BASE_URL);
      baseURLValid = true;
    } catch (error) {
      configurationError = error.message;
    }
  }
  const base = safeProviderStatus(resolved);
  return {
    ...base,
    configured: base.has_api_key && baseURLValid,
    base_url_valid: baseURLValid,
    configuration_error: configurationError,
    credential_source: env.DASHSCOPE_API_KEY
      ? "environment"
      : resolved.DASHSCOPE_API_KEY
        ? "env_file"
        : "missing"
  };
}

export function providerConfig(env = process.env) {
  const resolved = resolvedProviderEnv(env);
  const status = providerStatus(env);
  if (!status.configured) {
    throw new Error(`Report writer provider is not fully configured: ${JSON.stringify(status)}`);
  }
  return {
    provider: "bailian",
    apiKey: resolved.DASHSCOPE_API_KEY,
    baseURL: validateBailianBaseURL(resolved.BAILIAN_BASE_URL),
    model: REPORT_PRESET.model,
    enableThinking: REPORT_PRESET.enable_thinking
  };
}

function jsonObjectFormat() {
  return { ...REPORT_PRESET.response_format };
}

export function structuredRequestBody({ system, user, config }) {
  return {
    model: config.model,
    messages: [
      { role: "system", content: `${system}\nReturn JSON only.` },
      { role: "user", content: user }
    ],
    response_format: jsonObjectFormat(),
    enable_thinking: config.enableThinking,
    temperature: REPORT_PRESET.temperature,
    max_tokens: REPORT_PRESET.max_tokens
  };
}

export async function createStructured({ schema, system, user, config = providerConfig() }) {
  const client = await createProviderClient(config);
  const completion = await client.chat.completions.create(
    structuredRequestBody({ system, user, config })
  );
  const content = completion.choices?.[0]?.message?.content;
  if (!content) throw new Error("Provider returned empty content");
  let raw;
  try {
    raw = JSON.parse(content);
  } catch (error) {
    throw new Error(`Provider returned invalid JSON: ${error.message}`);
  }
  const parsed = schema.parse(raw);
  return {
    parsed,
    provenance: {
      provider: config.provider,
      model: config.model,
      response_id: completion.id ?? null,
      usage: completion.usage ?? null
    }
  };
}
