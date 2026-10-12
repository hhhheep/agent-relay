import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import assert from "node:assert/strict";
import {
  EvidencePacketSchema,
  assertAllowedPath,
  atomicWriteText,
  safeProviderStatus,
  sha256File
} from "../lib/core.mjs";
import { createServer } from "../server.mjs";
import { providerConfig, providerStatus, structuredRequestBody } from "../lib/provider.mjs";
import {
  assertRemotePath,
  buildRemoteInvocation,
  remoteConfig,
  safeRemoteStatus
} from "../lib/remote.mjs";

test("validates a minimal evidence packet", () => {
  const packet = EvidencePacketSchema.parse({
    schema_version: "1.0",
    task_id: "T1",
    title: "Title",
    question: "Question",
    report_type: "status",
    execution_status: "done",
    scientific_status: "limited",
    claims: [{ claim_id: "OBS-1", kind: "observation", text: "Observed", source: "x.json" }]
  });
  assert.equal(packet.claims.length, 1);
});

test("provider status never returns a key", () => {
  const status = safeProviderStatus({
    DASHSCOPE_API_KEY: "secret-value",
    BAILIAN_BASE_URL: "https://ws-test.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
  });
  assert.equal(status.configured, true);
  assert.equal(status.provider, "bailian");
  assert.equal(status.model, "deepseek-v4-flash-0731");
  assert.equal(status.fixed_profile, true);
  assert.equal(JSON.stringify(status).includes("secret-value"), false);
});

test("provider profile is fixed to Bailian-hosted DeepSeek V4-Flash-0731", () => {
  const config = providerConfig({
    DASHSCOPE_API_KEY: "secret-value",
    BAILIAN_BASE_URL: "https://ws-test.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1/",
    DEEPSEEK_MODEL: "unexpected-model"
  });
  assert.equal(config.provider, "bailian");
  assert.equal(config.model, "deepseek-v4-flash-0731");
  assert.equal(config.baseURL, "https://ws-test.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1");
  assert.equal(config.enableThinking, false);
  assert.equal(providerConfig({
    DASHSCOPE_API_KEY: "secret-value",
    BAILIAN_BASE_URL: "https://dashscope.aliyuncs.com/compatible-mode/v1"
  }).baseURL, "https://dashscope.aliyuncs.com/compatible-mode/v1");
  assert.throws(() => providerConfig({
    DASHSCOPE_API_KEY: "secret-value",
    BAILIAN_BASE_URL: "https://api.deepseek.com"
  }));
});

test("structured JSON requests explicitly disable thinking", () => {
  const config = providerConfig({
    DASHSCOPE_API_KEY: "secret-value",
    BAILIAN_BASE_URL: "https://dashscope.aliyuncs.com/compatible-mode/v1"
  });
  const body = structuredRequestBody({ system: "system", user: "user", config });
  assert.equal(body.model, "deepseek-v4-flash-0731");
  assert.equal(body.enable_thinking, false);
  assert.equal("reasoning_effort" in body, false);
  assert.deepEqual(body.response_format, { type: "json_object" });
});

test("provider can load only Bailian settings from a bounded env file", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "rrw-env-"));
  const envFile = path.join(root, ".env");
  fs.writeFileSync(
    envFile,
    "OTHER_KEY=ignored\nDASHSCOPE_API_KEY=file-secret\nBAILIAN_BASE_URL=https://ws-test.cn-beijing.maas.aliyuncs.com/compatible-mode/v1\n",
    "utf8"
  );
  const env = { REPORT_WRITER_ENV_FILE: envFile };
  const config = providerConfig(env);
  const status = providerStatus(env);
  assert.equal(config.apiKey, "file-secret");
  assert.equal(config.baseURL, "https://ws-test.cn-beijing.maas.aliyuncs.com/compatible-mode/v1");
  assert.equal(status.configured, true);
  assert.equal(status.base_url_valid, true);
  assert.equal(status.credential_source, "env_file");
  assert.equal(JSON.stringify(status).includes("file-secret"), false);
});

test("path guard and atomic write stay inside allowed roots", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "rrw-"));
  process.env.REPORT_WRITER_ALLOWED_ROOTS = root;
  const target = assertAllowedPath(path.join(root, "out", "report.md"));
  atomicWriteText(target, "hello\n");
  assert.equal(fs.readFileSync(target, "utf8"), "hello\n");
  assert.equal(sha256File(target).length, 64);
  assert.throws(() => assertAllowedPath(path.resolve(root, "..", "escape.md")));
});

test("constructs the MCP server with installed SDK packages", () => {
  const server = createServer();
  assert.ok(server);
});

test("remote configuration is secret-safe and path-scoped", () => {
  const env = {
    REPORT_WRITER_REMOTE_ALIAS: "EXAMPLE_WORKER_A",
    REPORT_WRITER_REMOTE_SCRIPT: "/ssd2/work/tools/report_writer.py",
    REPORT_WRITER_REMOTE_ROOTS: "/ssd2/work",
    REPORT_WRITER_REMOTE_TIMEOUT_MS: "600000",
    DASHSCOPE_API_KEY: "remote-secret"
  };
  const config = remoteConfig(env);
  assert.equal(assertRemotePath("/ssd2/work/docs/report.md", config), "/ssd2/work/docs/report.md");
  assert.throws(() => assertRemotePath("/ssd2/other/report.md", config));
  assert.equal(JSON.stringify(safeRemoteStatus(env)).includes("remote-secret"), false);
});

test("SSH invocation contains no provider credential", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "rrw-ssh-"));
  const sshConfig = path.join(root, "ssh config");
  fs.writeFileSync(sshConfig, "Host example\n", "utf8");
  const config = remoteConfig({
    REPORT_WRITER_REMOTE_ALIAS: "EXAMPLE_WORKER_A",
    REPORT_WRITER_REMOTE_SCRIPT: "/ssd2/work/tools/report_writer.py",
    REPORT_WRITER_REMOTE_ROOTS: "/ssd2/work",
    REPORT_WRITER_SSH_CONFIG: sshConfig
  });
  const invocation = buildRemoteInvocation(config);
  assert.equal(invocation.command, "ssh");
  assert.equal(invocation.args.join(" ").includes("secret"), false);
  assert.deepEqual(invocation.args.slice(0, 2), ["-F", sshConfig]);
  assert.deepEqual(invocation.args.slice(-2), ["python3", "/ssd2/work/tools/report_writer.py"]);
});
