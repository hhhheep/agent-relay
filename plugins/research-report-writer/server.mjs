import path from "node:path";
import { pathToFileURL } from "node:url";
import { McpServer } from "@modelcontextprotocol/server";
import { serveStdio } from "@modelcontextprotocol/server/stdio";
import { z } from "zod/v4";
import {
  assertAllowedPath,
  atomicWriteText,
  loadEvidencePacket,
  readLimitedText,
  sha256File
} from "./lib/core.mjs";
import { draftWithProvider, providerStatus } from "./lib/provider.mjs";
import {
  draftRemote,
  safeRemoteStatus,
  validateRemoteEvidence
} from "./lib/remote.mjs";

function textResult(payload, isError = false) {
  return {
    content: [{ type: "text", text: JSON.stringify(payload, null, 2) }],
    structuredContent: payload,
    isError
  };
}

function provenancePath(outputPath, suffix) {
  return `${outputPath}.${suffix}.json`;
}

export function createServer() {
  const server = new McpServer(
    { name: "research-report-writer", version: "0.1.0" },
    { instructions: "Validate evidence first, then draft with the fixed Bailian-hosted DeepSeek V4-Flash-0731 profile. Codex performs the final evidence review. Never pass API keys as tool arguments." }
  );

  server.registerTool(
    "provider_status",
    {
      description: "Check whether the fixed Bailian-hosted DeepSeek V4-Flash-0731 profile has an API key and valid workspace Base URL; returns only secret-safe booleans and model metadata.",
      inputSchema: z.object({})
    },
    async () => textResult(providerStatus())
  );

  server.registerTool(
    "remote_writer_status",
    {
      description: "Use this to inspect secret-safe SSH remote-writer configuration. API keys are never returned and are transported only through SSH stdin at execution time.",
      inputSchema: z.object({})
    },
    async () => textResult({ provider: providerStatus(), remote: safeRemoteStatus() })
  );

  server.registerTool(
    "validate_evidence_packet",
    {
      description: "Use this before report generation to validate a compact evidence JSON file, unique claim IDs, size limit, and artifact references without a network request.",
      inputSchema: z.object({ evidence_path: z.string().min(1) })
    },
    async ({ evidence_path }) => {
      try {
        const loaded = loadEvidencePacket(evidence_path);
        const missingArtifacts = loaded.packet.artifacts
          .map((artifact) => artifact.path)
          .filter((artifactPath) => {
            try { assertAllowedPath(artifactPath, { mustExist: true }); return false; }
            catch { return true; }
          });
        return textResult({
          valid: true,
          evidence_path: loaded.resolved,
          evidence_sha256: loaded.sha256,
          claim_count: loaded.packet.claims.length,
          artifact_count: loaded.packet.artifacts.length,
          missing_artifacts: missingArtifacts
        });
      } catch (error) {
        return textResult({ valid: false, error: error.message }, true);
      }
    }
  );

  server.registerTool(
    "draft_research_report",
    {
      description: "Use this when a validated local evidence packet should be sent to Bailian-hosted DeepSeek V4-Flash-0731 to write a Markdown report directly to local disk.",
      inputSchema: z.object({
        evidence_path: z.string().min(1),
        rules_path: z.string().min(1).optional(),
        output_path: z.string().min(1),
        overwrite: z.boolean().default(false)
      })
    },
    async ({ evidence_path, rules_path, output_path, overwrite }) => {
      try {
        const evidence = loadEvidencePacket(evidence_path);
        const output = assertAllowedPath(output_path);
        if (!overwrite && await import("node:fs").then(({ default: fs }) => fs.existsSync(output))) {
          throw new Error(`Output already exists: ${output}`);
        }
        const rules = rules_path ? readLimitedText(rules_path).text : "";
        const generated = await draftWithProvider({ evidence: evidence.packet, rules });
        atomicWriteText(output, `${generated.parsed.report_markdown.trim()}\n`);
        const sidecar = provenancePath(output, "writer");
        const metadata = {
          schema_version: "1.0",
          created_at: new Date().toISOString(),
          evidence_path: evidence.resolved,
          evidence_sha256: evidence.sha256,
          rules_path: rules_path ? path.resolve(rules_path) : null,
          rules_sha256: rules_path ? sha256File(path.resolve(rules_path)) : null,
          output_path: output,
          output_sha256: sha256File(output),
          claims_used: generated.parsed.claims_used,
          warnings: generated.parsed.warnings,
          unsupported_claims: generated.parsed.unsupported_claims,
          provider: generated.provenance
        };
        atomicWriteText(sidecar, `${JSON.stringify(metadata, null, 2)}\n`);
        return textResult({
          written: true,
          output_path: output,
          output_sha256: metadata.output_sha256,
          sidecar_path: sidecar,
          claims_used_count: metadata.claims_used.length,
          warning_count: metadata.warnings.length,
          unsupported_claim_count: metadata.unsupported_claims.length,
          provider: generated.provenance
        });
      } catch (error) {
        return textResult({ written: false, error: error.message }, true);
      }
    }
  );

  server.registerTool(
    "validate_remote_evidence_packet",
    {
      description: "Validate an evidence packet already stored on the configured SSH workplace. No API key or report content is transferred.",
      inputSchema: z.object({ remote_evidence_path: z.string().min(1) })
    },
    async ({ remote_evidence_path }) => {
      try {
        return textResult(await validateRemoteEvidence({ evidencePath: remote_evidence_path }));
      } catch (error) {
        return textResult({ valid: false, error: error.message }, true);
      }
    }
  );

  server.registerTool(
    "draft_remote_research_report",
    {
      description: "Keep evidence and Markdown on the configured SSH workplace. The local Bailian Model Studio key is sent ephemerally through encrypted SSH stdin, never as an argument or remote file.",
      inputSchema: z.object({
        remote_evidence_path: z.string().min(1),
        remote_rules_path: z.string().min(1).optional(),
        remote_output_path: z.string().min(1),
        overwrite: z.boolean().default(false)
      })
    },
    async ({ remote_evidence_path, remote_rules_path, remote_output_path, overwrite }) => {
      try {
        const result = await draftRemote({
          evidencePath: remote_evidence_path,
          rulesPath: remote_rules_path,
          outputPath: remote_output_path,
          overwrite
        });
        return textResult(result);
      } catch (error) {
        return textResult({ written: false, error: error.message }, true);
      }
    }
  );

  return server;
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  void serveStdio(createServer);
}
