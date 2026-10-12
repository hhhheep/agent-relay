# Token-savings validation

Use this procedure only when measuring or claiming savings. Ordinary guarded commands do not need to load it.

## Offline correctness and byte proxy

Run the fixed replay corpus against `token_guard.py` and then `stage_ledger.py`:

```text
<python> <skill-dir>/scripts/replay_benchmark.py run \
  --root <workspace> \
  --manifest <replay-corpus.json> \
  --token-guard <skill-dir>/scripts/token_guard.py \
  --stage-ledger <skill-dir>/scripts/stage_ledger.py \
  --output <offline-result.json>
```

The offline gate requires byte-exact guarded raw streams, verified hashes, matching exit classification and UTF-8 status, and recovery of every answer-key literal from model-visible structured content. `byte_reduction_percent` is a transport proxy only. When `provider_token_gate.claim` is `not_measured`, `actual_token_savings_proven` must remain false.

## Provider-measured A/B

Run baseline and guarded tasks from fresh contexts with the same model, reasoning setting, prompt, permissions, corpus, and answer key.

Extract each fresh Codex task's usage without exposing its conversation text:

```text
<python> <skill-dir>/scripts/codex_usage.py summarize --session <rollout.jsonl> --output <summary.json>
```

For a bounded pilot with fixed expected and actual answer JSON files, evaluate correctness, provider usage, and cache comparability together:

```text
<python> <skill-dir>/scripts/codex_usage.py evaluate \
  --case-id <case> --model <model> --reasoning-effort <effort> \
  --expected-answer <expected.json> \
  --baseline-answer <baseline-answer.json> \
  --guarded-answer <guarded-answer.json> \
  --baseline-usage <baseline-usage.json> \
  --guarded-usage <guarded-usage.json> \
  --min-new-input-reduction 0.4 \
  --output <evaluation.json>
```

By default, `evaluate` rejects the token claim when cached input differs by more than 5% between arms. It still reports correctness, new-input reduction, total-input reduction, and the cache delta so a confounded run remains useful diagnostic evidence.

When both arms have the same number of provider responses, the evaluator also reports a `last_response_comparison`. This isolates the response after tool output and is useful for diagnosing whether bounded evidence reduced model context. It is not a substitute for the complete-task acceptance gate.

Record baseline and guarded rollout paths in an `ab-session-pairs/v1` manifest, then assemble the comparison:

```text
<python> <skill-dir>/scripts/codex_usage.py assemble --manifest <pairs.json> --output <provider-usage.json>
```

The extractor reads `token_usage_record` events. It defines new input as `input_tokens - cached_input_tokens` and preserves both original values. The resulting provider usage has this shape:

```json
{
  "schema_version": "research-token-guard/provider-usage/v1",
  "cases": {
    "case-id": {
      "baseline": {
        "new_input_tokens": 1000,
        "cached_input_tokens": 200
      },
      "guarded": {
        "new_input_tokens": 550,
        "cached_input_tokens": 100
      }
    }
  }
}
```

Pass that file to `replay_benchmark.py run --provider-usage <usage.json>`. The case set must exactly match the corpus. New and cached input remain separate.

The token gate passes only after the correctness gate passes and provider-reported new input tokens decrease by at least 30% in aggregate and 40% at the case median. Do not infer cost without the provider's documented accounting.

## Invalid comparisons

Reject the comparison if either arm performs less required work, uses a different answer key, starts with materially different context, changes model or reasoning, silently retries, omits a failed request, lacks per-case provider usage, or has materially imbalanced cached input. A cheaper wrong decision is a failed run.
