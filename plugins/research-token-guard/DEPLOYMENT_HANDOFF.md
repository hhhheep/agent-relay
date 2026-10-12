# Research Token Guard remote-workflow deployment handoff

> **Deployment hold:** v0.2 now has one clean provider-measured case with correctness parity and 44.7253% lower new input, but the representative real-record pair was cache-confounded and was correctly rejected by the evaluator. Read `V0.2_TOKEN_SAVINGS_DESIGN.md`. Do not use the old prompt below for a final production rollout until a broader representative replay passes the corpus gates. A bounded remote pilot may be deployed in a new page when the user requests it.

This was the v0.1 deployment handoff. It is retained as deployment context, but is superseded by the active v0.2 token-savings goal. The output guard and personal skill already exist on remote 91; the solution-agent workflow integration described here has not been deployed yet.

## v0.2 bounded remote-pilot prompt for a new page

```text
Use the ncku remote workplace, research-token-guard, skill-creator, plugin-creator, and remote-artifact-packager guidance as applicable.

Deploy Research Token Guard v0.2.0 as a bounded pilot to EXAMPLE_WORKER_A for /srv/example-workspaces/fan-workspace. The canonical local source is:
C:\Users\example-user\Desktop\example-research\tools\research-token-guard

Inspect the remote tool and personal skill first, compare versions and hashes, and update in place without deleting research artifacts. Read the exact cachebuster from the local manifest; the expected base version is 0.2.0. Update both the workspace tool and /home/agent-user/.codex/skills/research-token-guard as needed.

Integrate the atomic stage contract into the solution-agent / remote workplace flow: one objective and answer key per stage; exact command, declared inputs, budgets, and literal evidence queries in the stage spec; one model call to stage_ledger.py execute; verified raw receipts and event chain; no cache reuse after any declared-input mutation; status/change for routine views; exact evidence only for decisions; process-side waiting; and a short terminal handoff.

Do not enable a global command hook, proxy model/API traffic, expose API keys, change the workplace model, start GPU training, or run paid external-model calls. Keep the rollout labeled pilot: the clean local GPT-6 Luna/high case saved 44.7253% new input with identical answers and equal cached input, while the real-record pair was rejected as cache-confounded.

Before declaring the pilot installed, run the full unit suite, Python compilation, skill/plugin validation where available, the offline replay, and one deterministic atomic execute smoke test. Open a fresh remote GPT-6 task to confirm the skill is discovered and that the task performs one atomic command rather than CLI discovery. Report exact changed files, hashes, installed version, test counts, smoke receipt hashes, and any solution-agent integration that remains manual. Do not claim production token savings from bytes or from a cache-imbalanced A/B.
```

## Known current state

- Remote SSH alias: `EXAMPLE_WORKER_A`
- Remote project: `/srv/example-workspaces/fan-workspace`
- Remote Codex observed: `0.155.1`
- Existing workspace tool: `/srv/example-workspaces/fan-workspace/tools/research-token-guard`
- Existing personal skill: `/home/agent-user/.codex/skills/research-token-guard`
- Global command hook: intentionally disabled
- External compression model: none

Do not assume the files are unchanged. Verify the current hashes and versions before updating them.

## Historical v0.1 new-page prompt (held)

```text
Use the ncku remote workplace, research-token-guard, skill-creator, plugin-creator, and remote-artifact-packager guidance as applicable.

Deploy the completed Research Token Guard remote workflow to EXAMPLE_WORKER_A for /srv/example-workspaces/fan-workspace. The command-output tool and personal skill may already exist, so inspect and update safely rather than blindly reinstalling. The canonical local source is:
C:\Users\example-user\Desktop\example-research\tools\research-token-guard

Integrate the bounded stage workflow into the actual solution-agent / remote workplace task contract. Require one objective and acceptance gate per Codex task, guarded execution for noisy commands, exact verification before scientific or repair decisions, process-side waiting instead of model polling, a validated stage_receipt.json, a handoff under about 2,000 tokens, and a fresh Codex task for the next stage.

Do not enable a global command hook, do not proxy model/API traffic, do not delete existing research artifacts, and do not run new GPU or paid API work for deployment validation. Preserve raw stdout/stderr and hashes. Use saved logs or deterministic smoke outputs for A/B validation.

Before declaring success, run all plugin tests locally and remotely, validate the plugin and skill, validate a realistic stage receipt with --check-artifacts and --verify-token-guard, and open a fresh GPT-6 Luna read-only task that demonstrates correct skill usage. Report exact changed files, hashes, test counts, and any remaining manual step.
```

## Deployment acceptance

- The existing tool is updated only after hash comparison and tests.
- The orchestrator produces a stage receipt conforming to `research-stage-receipt/v1`.
- `validate_stage_receipt.py` passes with artifact and Token Guard verification enabled.
- No short-interval model polling is introduced.
- No global hook or API proxy is enabled.
- A new GPT-6 task loads the skill and follows the stage contract.
- Full raw evidence remains on the remote workplace; only bounded receipts and short handoffs cross stages.
