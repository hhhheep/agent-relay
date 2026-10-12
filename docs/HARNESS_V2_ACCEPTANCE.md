# Harness V2 Acceptance Record

## Scope note added 2026-09-28

This record is the verified baseline for the pre-event-driven candidate.  It
does not implement or satisfy:

- `HARNESS_V2_EVENT_DRIVEN_WAIT_TASKBOOK.md`;
- `HARNESS_V2_TOKEN_EFFICIENCY_AND_CORRECTNESS_TASKBOOK.md`.

Its component-level token A/B results remain valid for their declared cases,
but they are not proof of zero model calls during WAIT, full local/remote usage
aggregation, or the 14-unit weekly operating target.  A new release acceptance
record is required after those taskbooks are implemented.

## Result

The integration candidate is packaged and safe to deploy from a new Codex task.
It has not been applied to the original local checkout or the Example workplace.

## Verified behavior

- The MCP surface exposes exactly `stage_execute`, `stage_status`, and
  `stage_collect`.
- A real no-API MCP smoke launched a subprocess, produced two artifacts,
  collected `metrics.json`, and completed as `succeeded`.
- Context selection is bounded and writes a selection manifest. Required
  evidence raises instead of being silently discarded.
- The repo map ranks symbols and dependencies against the current task instead
  of sending a fixed filename list.
- Provider usage separates input, cache read/write, new input, output, total,
  and optional observed cost.
- A repeated production validation fingerprint stops after one corrective
  retry. A materially changed failure may continue.
- Required Token Guard receipts and raw stream hashes are verified during
  collection; invalid evidence fails closed.
- The report-writer stage fixes provider/model to Bailian-hosted
  `deepseek-v4-flash-0731`, accepts credentials only from the environment, writes
  one report plus a claim sidecar, rejects unknown claim IDs, and persists
  provider usage without prompt bodies.
- The overlay installer dry-run against the original dirty checkout reported
  zero conflicts and never modified it.

## Regression record

Full-suite result after integration:

- 1508 passed
- 25 failed
- 1 skipped
- 5 subtests passed

The 25 failure identities exactly match both the pre-change baseline and the
post-real-A/B final run; added regressions are zero. The final guarded receipt
is:

```text
.token-guard/runs/20260926T101025Z-d3de57f7/receipt.json
stdout sha256: 34c49c73628f13cde2f6c9a8ed35d074ce7610b991da505154e05ddc8de91f3b
stderr sha256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
```

Most baseline failures exercise existing deep Windows paths and older
continuation fixtures. They are not hidden or reclassified as passes.

## Token-savings claim status

The paired A/B gate has passed a controlled live `gpt-6-luna`/low synthetic
benchmark. Three baseline/candidate outcomes were identical and evidence
verified. Cache-stratum matching selected the first pair whose cached-token
delta was at most 15%; selection ignored savings magnitude and preserved all
excluded attempts.

Provider-reported selected totals:

- baseline total input: 285586; candidate total input: 44393
- baseline cached input: 42240; candidate cached input: 40192
- baseline output: 166; candidate output: 177
- non-cached input plus output: 243512 -> 4378, a 98.202142% reduction
- total input plus output: 285752 -> 44570, an 84.402559% reduction
- maximum paired cached-token delta: 7.2727%

The authoritative report is:

```text
benchmarks/harness_token_ab/runs/20260926T093350Z/ab_result_balanced.json
```

An earlier pilot produced no usage because the child process was blocked from
network access by the shell sandbox; it is excluded rather than counted as a
slow or zero-token run.

The production-provider real-task gate also passed. It used the fixed Bailian
Singapore endpoint and `deepseek-v4.1-flash` against three historical research
tasks whose evidence hashes match their existing writer sidecars. The
no-network disclosure preflight fixed the scope to 24,182 candidate bytes and
126,776 baseline bytes and rejected OpenAI-style, assignment-style, Bearer,
PEM, AWS, Alibaba Cloud, GitHub, Hugging Face, and Slack credential patterns.

All six selected provider calls returned the expected canonical outcome. The
three baseline/candidate fingerprints were identical, all evidence checks
passed, both sides reported zero cached input, and the gate reported no
violations. Provider-reported input plus output changed from 38,834 to 10,040:
28,794 fewer tokens, or 74.146367%. Prompt bodies were not persisted.

The authoritative real-task artifacts are:

```text
benchmarks/real_task_ab/preflight_manifest_bailian.json
preflight sha256: fed91a4ef93432e35d251e531038ecd5fa7585f72aba58fd129731e9bed28d36
benchmarks/real_task_ab/runs_bailian/20260926T100805Z/real_task_ab_bailian_result.json
result sha256: 247c2a96e0a771b374a7b0ff6f5e5ef2fa62f29c42b812d9212bfe7097e06bab
```

The local-only Bailian A/B safety tests passed 3/3. They cover wrong-case
rejection, allowlisted credential loading, provider-usage enforcement, and the
invariant that saved response records contain no prompt body.

`evaluate_paired_savings` continues to reject changed outcomes, unverified
evidence, provider/model mismatch, cache imbalance, or non-positive savings.
