# GPT-6 skill scope consolidation receipt

Date: 2026-09-28

## Applied design rules

This change follows OpenAI's current guidance: the name and description are the
primary selection signals; descriptions should be short and goal-specific;
conditional detail belongs in references; overlapping or over-broad skills
cause unnecessary loading; routing changes need positive and negative eval
prompts.

Official references:

- https://developers.openai.com/blog/rethinking-skills-and-prompts-for-gpt-6-astra
- https://developers.openai.com/plugins/concepts/skills
- https://developers.openai.com/blog/eval-skills

## Consolidation

- `solution-agent-feedback` is no longer a separate skill. Its sparse feedback
  procedure and linter now live under `solution-agent-harness` and are read only
  for an actionable Harness issue or an explicit feedback request.
- Example 91/93/FAN routing now lives in the Harness reference. The former
  user-level `example-remote-codex` skill remains only as an explicit legacy
  diagnostic and has `allow_implicit_invocation: false`.
- `research-token-guard` remains separate because it applies to local noisy
  commands outside Harness. Its trigger excludes ordinary small commands and
  normal file reads.
- `research-report-writer` remains separate because it invokes an external
  report model. Its trigger excludes ordinary Markdown editing, planning,
  scientific decisions, and auditing.
- `remote-artifact-packager` remains separate because its Windows/SSH quoting
  and archive verification are specialized. It now triggers only for explicit
  package, archive, snapshot, or download outcomes.

## WAIT interaction

The Harness skill now states one control-plane rule: a
`WAITING_EXTERNAL` disposition with model continuation suppressed ends the
model turn. A Goal loop, model heartbeat, or repeated model-driven
`stage_status` call is not a fallback.

## Validation

- All five changed source or installed skills passed the official
  `quick_validate.py` validator.
- Harness, Token Guard, and Report Writer personal plugin sources passed the
  official plugin validator.
- Harness packaging, feedback lint, MCP, WAIT, and routing focused suite: 22 passed.
- Broader affected-area regression: 158 passed, 1459 deselected.
- The cached Harness plugin contains exactly one `SKILL.md`.
- `evals/skill-routing.prompts.csv` contains positive, implicit, adjacent, and
  negative controls for later behavioral evaluation in a fresh task.

Installed cache versions:

- `solution-agent-harness`: `0.5.0+codex.20260928150832`
- `research-token-guard`: `0.2.0+codex.20260928150832`
- `research-report-writer`: `0.1.0+codex.20260928150832`

## Known Codex CLI state mismatch

`codex plugin add` reported success and created the versioned cache roots above,
but `codex plugin list --available --json` still reports the three personal
plugins as `installed=false`. User config still enables all three and the current
task loads their prior versions. A new task is required to verify which cached
versions the App actually advertises. Do not delete old caches; they remain
rollback evidence.
