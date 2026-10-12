# Upstream Design Sources

Harness V2 uses upstream projects as design references and keeps one existing
solution-agent lifecycle. It does not add another autonomous scheduler.

## Adopted now

- [MCP Python SDK v2](https://github.com/modelcontextprotocol/python-sdk): the
  only added runtime dependency. It provides the typed seven-tool MCP surface
  and in-process client tests. The tested version is 2.2.0; the requirement is
  bounded to `mcp>=2.2,<3`.
- [Aider repository map](https://github.com/Aider-AI/aider/blob/main/aider/website/docs/repomap.md):
  inspired the static symbol/dependency ranking under a fixed map budget. This
  implementation is local code; no Aider source or automatic Git behavior is
  embedded.
- [SWE-agent](https://github.com/SWE-agent/SWE-agent): inspired the narrow
  agent-computer interface, bounded views, compact validation feedback, and
  stop-on-no-progress behavior. SWE-agent itself is not installed.
- [Inspect AI](https://github.com/UKGovernmentBEIS/inspect_ai): inspired paired
  samples and explicit token/time/cost gates. Production execution remains in
  solution-agent; `harness.ab_eval` is the independent savings gate.
- [OpenInference semantic conventions](https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md):
  informed names for input, output, cache-read, cache-write, total token, and
  cost observations. Prompt and response bodies are not traced by default.

## Selected for the token-efficiency taskbook

These are design references for
`HARNESS_V2_TOKEN_EFFICIENCY_AND_CORRECTNESS_TASKBOOK.md`.  They do not add
runtime dependencies.

- [Pydantic AI usage limits](https://github.com/pydantic/pydantic-ai/blob/main/docs/agent.md#usage-limits):
  informed separate request, tool-call, cumulative-token, per-request-context,
  and cost limits.  Harness distinguishes preflight limits from post-response
  limits so an already-billed overage is never described as prevented.
- [AutoGen bounded model context](https://github.com/microsoft/autogen/blob/main/python/packages/autogen-agentchat/src/autogen_agentchat/agents/_assistant_agent.py):
  reinforced explicit message-buffer and token-limited context interfaces.
  Harness keeps its own deterministic context gateway and does not add AutoGen.
- [SWE-agent history processors](https://github.com/SWE-agent/SWE-agent/blob/main/sweagent/agent/history_processors.py):
  informed explicit keep/remove evidence classes and cache-aware history
  reduction.  Recency alone is not an evidence-retention policy.
- [OpenHands condenser contract](https://github.com/OpenHands/software-agent-sdk/blob/main/openhands-sdk/openhands/sdk/context/condenser/base.py)
  and [rule-loss issue 4544](https://github.com/OpenHands/software-agent-sdk/issues/4544):
  informed immutable source views, condensation metadata, and a regression gate
  proving that active rules remain deterministically recoverable.  LLM summaries
  cannot own Harness authority or scientific evidence.
- [MCP tools specification](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/server/tools.mdx):
  informed deterministic tool ordering and schema manifests for prompt-cache
  stability.  Harness keeps a stable seven-tool surface rather than per-call
  dynamic tool churn.
- [LiteLLM spend tracking](https://github.com/BerriAI/litellm-docs/blob/main/docs/proxy/cost_tracking.md):
  informed requested/served model, provider, attempt, cache, and cost-anomaly
  fields.  Harness does not add a LiteLLM proxy or database; provider responses
  and the append-only run ledger remain authoritative.

## Deliberately not adopted

- No replacement agent loop, Git auto-commit, or second remote scheduler.
- No model-generated shell command surface. Execution remains registry based.
- No semantic compression of required evidence. Raw streams remain exact and
  hash-addressed through Token Guard.
- No token-savings claim based on character estimates. Only provider-reported
  usage can pass the A/B gate.
- No LLM-generated condensation as the only surviving copy of task authority,
  skills, run specifications, acceptance gates, or scientific protocols.
- No proxy/database dependency solely for token accounting.
- No claim that Harness reduced the Codex host's unrelated plugin tools unless
  a supported host profile proves the inventory change.

## Upgrade policy

Upgrade one upstream boundary at a time. Re-run the seven-tool MCP smoke test,
execution-port tests, report-writer contract tests, and paired-savings gates
before changing a tested dependency range.
