import { DraftResponseSchema } from './core.mjs';
import { createStructured } from '../../../packages/report-provider/index.mjs';
// Compatibility exports preserve the established Writer helper entrypoint.
export { providerStatus, providerConfig, structuredRequestBody } from '../../../packages/report-provider/index.mjs';

export async function draftWithProvider({ evidence, rules }) {
  const system = [
    "You write concise research reports grounded only in the supplied evidence packet.",
    "Separate measured observations, interpretations, limitations, and adopted decisions.",
    "Do not convert missing artifacts, interrupted execution, or inconclusive results into negative scientific findings.",
    "Every quantitative or consequential statement must be traceable to a supplied claim_id or artifact path.",
    "Do not include claims listed in forbidden_claims except to state explicitly that they are not established.",
    "Avoid repeating the same limitation or unsupported claim across multiple sections unless the supplied rules require it.",
    rules
  ].filter(Boolean).join("\n\n");
  const user = `Produce a JSON object with report_markdown, claims_used, warnings, and unsupported_claims.\n\nEVIDENCE PACKET:\n${JSON.stringify(evidence)}`;
  return createStructured({ schema: DraftResponseSchema, system, user });
}
