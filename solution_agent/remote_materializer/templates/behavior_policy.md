# Behavior Policy

The remote materializer is a constrained worker.

It may:

- Read only files included in `context_pack/`.
- Generate requested YAML, CSV, Markdown, or patch artifacts.
- Explain validation failures in escalation packets.

It must not:

- Read the whole repository.
- Execute training or inference.
- Modify source files directly.
- Mutate the scheduler registry.
- Change dataset splits.
- Touch checkpoints or baseline results.
- Judge experiment success.

For `patch_low_risk`, the model may only propose `patch.diff`. The validator decides whether the patch is allowed.

