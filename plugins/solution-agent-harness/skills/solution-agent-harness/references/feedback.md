# Sparse Harness feedback

Feedback is optional maintenance data, not a completion ritual. Use it only
when an observed Harness condition requires action, or when the user explicitly
asks for a Harness audit or feedback report.

Allowed classes are `defect`, `environment`, `safety-stop`, and `decision`.
Research results, provider answers, and task-domain failures are not Harness
defects. Do not speculate from an unverified symptom and do not repeat an
unchanged issue.

The canonical feedback file has `## Open issues` and `## Issues`. Preserve one
stable `HF-NNN` issue per underlying condition and append comments instead of
rewriting history. Keep evidence to the smallest exact fact; link receipts or
hashes instead of copying logs. Never include credentials or raw provider
payloads.

Before creating an issue, read the existing file and reuse the matching ID.
After editing, run:

```text
python <skill-dir>/scripts/lint_feedback.py <feedback-file>
```

When no actionable system issue exists, omit feedback entirely.
