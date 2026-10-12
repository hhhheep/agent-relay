import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { providerConfig } from "./provider.mjs";

const SAFE_REMOTE_TOKEN = /^\/[A-Za-z0-9._\/-]+$/;

export function remoteConfig(env = process.env) {
  const alias = (env.REPORT_WRITER_REMOTE_ALIAS ?? "").trim();
  const script = (env.REPORT_WRITER_REMOTE_SCRIPT ?? "").trim();
  const roots = (env.REPORT_WRITER_REMOTE_ROOTS ?? "")
    .split(";")
    .map((item) => item.trim())
    .filter(Boolean)
    .map((item) => path.posix.resolve(item));
  const timeoutMs = Number.parseInt(env.REPORT_WRITER_REMOTE_TIMEOUT_MS ?? "600000", 10);
  const sshConfigRaw = (env.REPORT_WRITER_SSH_CONFIG ?? "").trim();
  const sshConfig = sshConfigRaw ? path.resolve(sshConfigRaw) : null;
  if (!alias || !/^[A-Za-z0-9._-]+$/.test(alias)) {
    throw new Error("REPORT_WRITER_REMOTE_ALIAS is missing or invalid");
  }
  if (!SAFE_REMOTE_TOKEN.test(script)) {
    throw new Error("REPORT_WRITER_REMOTE_SCRIPT is missing or invalid");
  }
  if (roots.length === 0 || roots.some((root) => !SAFE_REMOTE_TOKEN.test(root))) {
    throw new Error("REPORT_WRITER_REMOTE_ROOTS is missing or invalid");
  }
  if (!Number.isFinite(timeoutMs) || timeoutMs < 1000 || timeoutMs > 1800000) {
    throw new Error("REPORT_WRITER_REMOTE_TIMEOUT_MS must be between 1000 and 1800000");
  }
  if (sshConfig && (!fs.existsSync(sshConfig) || !fs.statSync(sshConfig).isFile())) {
    throw new Error("REPORT_WRITER_SSH_CONFIG does not point to a readable file");
  }
  return { alias, script, roots, timeoutMs, sshConfig };
}

export function safeRemoteStatus(env = process.env) {
  try {
    const config = remoteConfig(env);
    return {
      configured: true,
      ssh_alias: config.alias,
      remote_script: config.script,
      remote_roots: config.roots,
      timeout_ms: config.timeoutMs,
      ssh_configured: Boolean(config.sshConfig),
      credential_transport: "ssh_stdin_ephemeral"
    };
  } catch (error) {
    return {
      configured: false,
      error: error.message,
      credential_transport: "ssh_stdin_ephemeral"
    };
  }
}

export function assertRemotePath(remotePath, config = remoteConfig()) {
  if (typeof remotePath !== "string" || !SAFE_REMOTE_TOKEN.test(remotePath)) {
    throw new Error(`Remote path is not an allowed absolute POSIX path: ${remotePath}`);
  }
  const resolved = path.posix.resolve(remotePath);
  const inside = config.roots.some((root) => resolved === root || resolved.startsWith(`${root}/`));
  if (!inside) throw new Error(`Remote path is outside REPORT_WRITER_REMOTE_ROOTS: ${resolved}`);
  return resolved;
}

export function buildRemoteInvocation(config = remoteConfig()) {
  const sshConfigArgs = config.sshConfig ? ["-F", config.sshConfig] : [];
  return {
    command: "ssh",
    args: [
      ...sshConfigArgs,
      "-o", "BatchMode=yes",
      "-o", "ConnectTimeout=10",
      config.alias,
      "python3",
      config.script
    ]
  };
}

function redact(text, secret) {
  if (!secret) return text;
  return String(text).split(secret).join("[REDACTED]");
}

export function runRemoteEnvelope(envelope, { env = process.env, spawnImpl = spawn } = {}) {
  const config = remoteConfig(env);
  const invocation = buildRemoteInvocation(config);
  const serialized = `${JSON.stringify({ ...envelope, allowed_roots: config.roots })}\n`;
  const secret = envelope.provider?.api_key ?? "";
  return new Promise((resolve, reject) => {
    const child = spawnImpl(invocation.command, invocation.args, {
      shell: false,
      windowsHide: true,
      stdio: ["pipe", "pipe", "pipe"]
    });
    let stdout = "";
    let stderr = "";
    const maxOutput = 1024 * 1024;
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error(`Remote writer timed out after ${config.timeoutMs} ms`));
    }, config.timeoutMs);
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
      if (stdout.length > maxOutput) child.kill();
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
      if (stderr.length > maxOutput) child.kill();
    });
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(new Error(redact(error.message, secret)));
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      if (stdout.length > maxOutput || stderr.length > maxOutput) {
        reject(new Error("Remote writer output exceeded 1 MiB"));
        return;
      }
      if (code !== 0) {
        reject(new Error(redact(stderr.trim() || `Remote writer exited with code ${code}`, secret)));
        return;
      }
      try {
        resolve(JSON.parse(stdout));
      } catch (error) {
        reject(new Error(`Remote writer returned invalid JSON: ${redact(error.message, secret)}`));
      }
    });
    child.stdin.end(serialized);
  });
}

function remoteProvider(env = process.env) {
  const config = providerConfig(env);
  return {
    provider: config.provider,
    api_key: config.apiKey,
    base_url: config.baseURL,
    model: config.model
  };
}

export async function validateRemoteEvidence({ evidencePath, env = process.env }) {
  const config = remoteConfig(env);
  return runRemoteEnvelope({
    mode: "validate",
    evidence_path: assertRemotePath(evidencePath, config)
  }, { env });
}

export async function draftRemote({ evidencePath, rulesPath, outputPath, overwrite, env = process.env }) {
  const config = remoteConfig(env);
  return runRemoteEnvelope({
    mode: "draft",
    evidence_path: assertRemotePath(evidencePath, config),
    rules_path: rulesPath ? assertRemotePath(rulesPath, config) : null,
    output_path: assertRemotePath(outputPath, config),
    overwrite: Boolean(overwrite),
    provider: remoteProvider(env)
  }, { env });
}
