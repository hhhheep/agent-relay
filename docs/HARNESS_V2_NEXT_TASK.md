# Prompt for the Next Implementation and Deployment Task

Use the `solution-agent-harness`, `research-token-guard`,
`research-report-writer`, `plugin-creator`, and `example-remote-codex` skills as
their scopes become relevant. Work from:

```text
C:\Users\example-user\Desktop\example-research\tools\solution-agent-harness
```

The existing packaged candidate is now a baseline, not the release to deploy as
the completed token-efficiency upgrade. First implement both required
taskbooks:

```text
docs/HARNESS_V2_EVENT_DRIVEN_WAIT_TASKBOOK.md
docs/HARNESS_V2_TOKEN_EFFICIENCY_AND_CORRECTNESS_TASKBOOK.md
```

Preserve the existing solution-agent registry, scheduler, seven-tool MCP
surface, terminal workflow, ResearchFlow authority, Token Guard evidence, and
report-writer boundary. Start with the failing acceptance tests from both
taskbooks. Implement observation-only usage ledgers and manifests before
changing routing, then add durable event-driven WAIT, mandatory bounded
context/output, separate request/tool/retry budgets, and idempotent local/remote
aggregation. Do not use an LLM condensation as the only surviving copy of
authority, task rules, acceptance gates, or scientific evidence.

Run the focused and complete local regression suites. Preserve all baseline
failure identities and require zero added regressions. Produce T1-T13 receipts
and the WAIT A1-A5 receipts. Do not claim weekly savings from the earlier
synthetic or report-only A/B tests.

Only after the implementation gates pass, build a new complete release archive
and record its SHA-256. Then follow `docs/HARNESS_V2_DEPLOYMENT.md` and
`docs/HARNESS_V2_REMOTE_DEPLOYMENT_REQUIREMENTS.md`. Verify the new bundle hash
and run the overlay installer in dry-run mode against the local solution-agent
checkout. Stop on any conflict; do not force overwrite or discard unrelated
dirty work. If clean, apply with backups, install requirements into the
checkout-local venv, register the personal plugin through the plugin-creator
workflow, and open a new task to verify that exactly seven Harness tools are
live. Run the no-API smoke before any provider call.

Deploy the same newly verified bundle to the configured Example 91 workplace using
the solution-agent remote lifecycle, not a long ad-hoc SSH command. Repeat the
dry-run, backup, plugin load, and no-API smoke remotely. Pass the Bailian key
only through the configured environment. Finally run one small real report and
the bounded shadow/paired cases required by the new taskbooks. Do not claim
token or allowance savings unless provider usage, outcome equivalence, evidence
verification, cache balance, WAIT zero-call evidence, and uncontaminated
account-window gates all pass. If exact Codex allowance observation is not
available, mark that gate inconclusive instead of estimating it.
