import { REPORT_PRESET } from "../../../packages/report-provider/index.mjs";
export { safeProviderStatus } from "../../../packages/report-provider/index.mjs";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { z } from "zod/v4";

export const ClaimSchema = z.object({
  claim_id: z.string().min(1),
  kind: z.enum(["observation", "interpretation", "limitation", "decision"]),
  text: z.string().min(1),
  source: z.string().min(1),
  value: z.unknown().optional(),
  scope: z.string().optional()
});

export const EvidencePacketSchema = z.object({
  schema_version: z.literal("1.0"),
  task_id: z.string().min(1),
  title: z.string().min(1),
  question: z.string().min(1),
  report_type: z.enum(["status", "experiment", "audit", "synthesis"]),
  execution_status: z.string().min(1),
  scientific_status: z.string().min(1),
  claims: z.array(ClaimSchema).min(1),
  forbidden_claims: z.array(z.string()).default([]),
  required_sections: z.array(z.string()).default([]),
  artifacts: z.array(z.object({
    path: z.string().min(1),
    sha256: z.string().regex(/^[a-fA-F0-9]{64}$/).optional(),
    role: z.string().optional()
  })).default([]),
  next_action: z.string().optional(),
  notes: z.array(z.string()).default([])
});

export const DraftResponseSchema = z.object({
  report_markdown: z.string().min(1),
  claims_used: z.array(z.string()),
  warnings: z.array(z.string()),
  unsupported_claims: z.array(z.string())
});

export const REPORT_WRITER_MODEL = REPORT_PRESET.model;
export const REPORT_WRITER_MODEL_FAMILY = REPORT_PRESET.model_family;

export function sha256Buffer(buffer) {
  return crypto.createHash("sha256").update(buffer).digest("hex");
}

export function sha256File(filePath) {
  return sha256Buffer(fs.readFileSync(filePath));
}

export function atomicWriteText(filePath, text) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const tempPath = `${filePath}.${process.pid}.${crypto.randomUUID()}.tmp`;
  fs.writeFileSync(tempPath, text, { encoding: "utf8", flag: "wx" });
  fs.renameSync(tempPath, filePath);
}

export function getAllowedRoots() {
  const raw = process.env.REPORT_WRITER_ALLOWED_ROOTS ?? "";
  return raw.split(path.delimiter).map((item) => item.trim()).filter(Boolean)
    .map((item) => path.resolve(item));
}

export function assertAllowedPath(filePath, { mustExist = false } = {}) {
  const roots = getAllowedRoots();
  if (roots.length === 0) {
    throw new Error("REPORT_WRITER_ALLOWED_ROOTS is not configured");
  }
  const resolved = path.resolve(filePath);
  const inside = roots.some((root) => resolved === root || resolved.startsWith(`${root}${path.sep}`));
  if (!inside) throw new Error(`Path is outside REPORT_WRITER_ALLOWED_ROOTS: ${resolved}`);
  if (mustExist && !fs.existsSync(resolved)) throw new Error(`File does not exist: ${resolved}`);
  return resolved;
}

export function readLimitedText(filePath) {
  const resolved = assertAllowedPath(filePath, { mustExist: true });
  const maxBytes = Number.parseInt(process.env.REPORT_WRITER_MAX_INPUT_BYTES ?? "524288", 10);
  const stat = fs.statSync(resolved);
  if (!stat.isFile()) throw new Error(`Expected a file: ${resolved}`);
  if (stat.size > maxBytes) {
    throw new Error(`Input exceeds REPORT_WRITER_MAX_INPUT_BYTES (${stat.size} > ${maxBytes})`);
  }
  return { resolved, text: fs.readFileSync(resolved, "utf8"), size: stat.size };
}

export function loadEvidencePacket(filePath) {
  const loaded = readLimitedText(filePath);
  let parsed;
  try {
    parsed = JSON.parse(loaded.text);
  } catch (error) {
    throw new Error(`Evidence packet is not valid JSON: ${error.message}`);
  }
  const packet = EvidencePacketSchema.parse(parsed);
  const ids = packet.claims.map((claim) => claim.claim_id);
  if (new Set(ids).size !== ids.length) throw new Error("claim_id values must be unique");
  return { ...loaded, packet, sha256: sha256Buffer(Buffer.from(loaded.text, "utf8")) };
}
